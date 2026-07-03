"""Public API-key job endpoints: submit an OCR job and poll its status and results."""

import os
import time
from datetime import timedelta
from typing import Literal
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import Settings
from app.deps import get_redis, get_settings, rate_limit_query, rate_limit_submit
from app.exceptions import BadRequest, Conflict, NotFound, NotReady, Unprocessable
from app.models.backend import Backend
from app.models.backend_domain import BackendDomain
from app.models.db import get_db
from app.models.domain import Domain
from app.models.job import Job, JobResult
from app.models.user import User
from app.schemas.job import (
    JobListResponse,
    JobResultEntry,
    JobResultResponse,
    JobStatus,
    JobSubmitResponse,
    render_external_url,
)
from app.services import config as config_service
from app.services import redis_jobs, storage

router = APIRouter()


@router.post(
    "/jobs",
    response_model=JobSubmitResponse,
    status_code=202,
    summary="Submit an OCR job",
    responses={
        400: {"description": "Unsupported extension or file too large"},
        401: {"description": "Missing or invalid X-API-Key header"},
        422: {"description": "Invalid UUID or unsupported fmt"},
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def submit_job(
    request: Request,
    image: UploadFile = File(...),
    uuid: UUID = Form(...),
    fmt: Literal["alto", "txt", "multi"] = Form("multi"),
    domain: str | None = Form(None),
    username: str = Depends(rate_limit_submit()),
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> JobSubmitResponse:
    """Submit an image for OCR and enqueue it for asynchronous processing.

    Requires a valid API key in the ``X-API-Key`` header. The upload is stored and the job
    is queued, returning ``202 Accepted`` with a job id immediately; poll the status and
    result endpoints to retrieve output once processing finishes.
    """
    # uuid and fmt are parsed and validated at the boundary (UUID / Literal), so the
    # body below trusts them.
    external_id = uuid

    # Validate extension
    filename = image.filename or "image"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in settings.allowed_extensions:
        raise BadRequest(f"Extension {ext} not allowed. Allowed: {settings.allowed_extensions}")

    # Validate file size
    image_bytes = await image.read()
    if len(image_bytes) > settings.max_upload_bytes:
        raise BadRequest(f"File too large. Max: {settings.max_upload_bytes} bytes")

    # Validate domain: if specified, at least one enabled backend must serve it.
    if domain:
        serves_domain = await db.scalar(
            select(func.count())
            .select_from(Domain)
            .join(BackendDomain, Domain.id == BackendDomain.domain_id)
            .join(Backend, BackendDomain.backend_id == Backend.id)
            .where(Domain.name == domain, Backend.enabled.is_(True))
        )
        if not serves_domain:
            raise Unprocessable(f"No backend serves domain '{domain}' — check available domains")

    # Upload to MinIO incoming
    incoming_client = request.app.state.incoming_client
    object_path = f"{username}/{external_id}{ext}"
    await storage.put_object(
        incoming_client,
        settings.minio_incoming_bucket,
        object_path,
        image_bytes,
        image.content_type or "application/octet-stream",
    )

    # Insert job in Postgres
    job = Job(
        username=username,
        external_id=external_id,
        status="queued",
        fmt=fmt,
        domain=domain,
        file_size_bytes=len(image_bytes),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Enqueue in Redis — include user priority so the submit worker routes to the right queue
    state_ttl = await config_service.get_state_ttl_seconds(db)
    user_row = await db.execute(select(User).where(User.username == username))
    user_priority = (user_row.scalar_one_or_none() or User()).priority or 0
    await redis_jobs.enqueue_job(
        r,
        str(job.id),
        {
            "username": username,
            "external_id": str(external_id),
            "fmt": fmt,
            "domain": domain or "",
            "ext": ext,
            "submitted_at": str(time.time()),
            "priority": str(user_priority),
        },
        state_ttl,
    )

    return JobSubmitResponse(job_id=job.id, external_id=external_id, status="queued")


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatus,
    summary="Get job status",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "Job not found for the authenticated user"},
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def get_job_status(
    job_id: UUID,
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
) -> JobStatus:
    """Return the current status of one of your jobs.

    Requires a valid API key in the ``X-API-Key`` header and only resolves jobs owned by the
    authenticated user. The status reflects the asynchronous lifecycle (``queued``,
    ``running``, ``done``, ``failed``); fetch the result endpoint once it is ``done``.
    """
    result = await db.execute(
        select(Job, User.external_url_template)
        .outerjoin(User, Job.username == User.username)
        .where(Job.id == job_id, Job.username == username)
    )
    row = result.first()
    if not row:
        raise NotFound("Job not found")
    job, url_template = row

    return JobStatus(
        job_id=job.id,
        external_id=job.external_id,
        status=job.status,
        fmt=job.fmt,
        domain=job.domain,
        submitted_at=job.submitted_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        external_url=render_external_url(url_template, job.external_id),
    )


@router.get(
    "/jobs/{job_id}/result",
    response_model=JobResultResponse,
    summary="Download job result",
    responses={
        202: {"description": "Job accepted but not finished yet; retry later"},
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "Job not found for the authenticated user"},
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def get_job_result(
    job_id: UUID,
    request: Request,
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobResultResponse:
    """Return presigned download URLs for a finished job's OCR output.

    Requires a valid API key in the ``X-API-Key`` header and only resolves jobs owned by the
    authenticated user. Because processing is asynchronous, the result may still be pending:
    a job that is not yet ``done`` responds with ``202``, while a failed job responds ``200``
    with ``status="failed"`` and the error (no results). Presigned URLs are refreshed
    automatically when expired.
    """
    result = await db.execute(select(Job).where(Job.id == job_id, Job.username == username))
    job = result.scalar_one_or_none()
    if not job:
        raise NotFound("Job not found")

    if job.status == "failed":
        return JobResultResponse(status="failed", error=job.error or "Job failed")
    if job.status != "done":
        raise NotReady("Job not completed yet")

    # Get results
    res = await db.execute(select(JobResult).where(JobResult.job_id == job_id))
    job_results = res.scalars().all()

    results_client = request.app.state.results_public_client
    entries = []
    now = utcnow()
    presigned_ttl = await config_service.get_presigned_ttl_minutes(db)
    for jr in job_results:
        # Refresh presigned URL if expired (stored as naive UTC)
        if not jr.presigned_url or (jr.presigned_until and jr.presigned_until < now):
            ext_map = {"alto": "xml", "txt": "txt"}
            file_ext = ext_map.get(jr.fmt, jr.fmt)
            obj_path = f"{username}/{job.external_id}.{file_ext}.zst"
            jr.presigned_url = await storage.presign_get(
                results_client,
                settings.minio_results_bucket,
                obj_path,
                presigned_ttl,
            )
            jr.presigned_until = now + timedelta(minutes=presigned_ttl)
            await db.commit()

        entries.append(JobResultEntry(fmt=jr.fmt, url=jr.presigned_url))

    return JobResultResponse(status="done", results=entries)


@router.get(
    "/jobs/{job_id}/result/{fmt}/download",
    summary="Download a finished job's stored OCR artifact",
    responses={
        202: {"description": "Job accepted but not finished yet; retry later"},
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "Job not found, or no result in the requested format"},
        409: {"description": "Job processing failed (no artifact to download)"},
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def download_job_result(
    job_id: UUID,
    fmt: Literal["alto", "txt"],
    request: Request,
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Stream a finished job's stored OCR artifact (zstd-compressed) through taas.

    Unlike ``/result`` — which returns presigned URLs signed for the *public* MinIO
    endpoint, for external clients — this proxies the object straight from internal
    storage. It exists for callers that share taas's network (e.g. the compat shim)
    and therefore cannot reach the public presign host. Requires a valid API key and
    only resolves jobs owned by the authenticated user.
    """
    result = await db.execute(select(Job).where(Job.id == job_id, Job.username == username))
    job = result.scalar_one_or_none()
    if not job:
        raise NotFound("Job not found")

    if job.status == "failed":
        raise Conflict(job.error or "Job failed")
    if job.status != "done":
        raise NotReady("Job not completed yet")

    res = await db.execute(
        select(JobResult).where(JobResult.job_id == job_id, JobResult.fmt == fmt)
    )
    if not res.scalar_one_or_none():
        raise NotFound(f"No {fmt} result available")

    ext_map = {"alto": "xml", "txt": "txt"}
    obj_path = f"{username}/{job.external_id}.{ext_map.get(fmt, fmt)}.zst"
    results_client = request.app.state.results_client
    data = await storage.get_object(results_client, settings.minio_results_bucket, obj_path)
    return Response(content=data, media_type="application/octet-stream")


@router.get(
    "/jobs",
    response_model=JobListResponse,
    summary="List your jobs",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def list_jobs(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
) -> JobListResponse:
    """List the authenticated user's jobs, newest first.

    Requires a valid API key in the ``X-API-Key`` header and returns only jobs owned by that
    user. Results can be filtered by ``status`` and are paginated with ``limit`` and
    ``offset``; ``total`` reports the full count matching the filter.
    """
    query = select(Job).where(Job.username == username)
    count_query = select(func.count()).select_from(Job).where(Job.username == username)

    if status:
        query = query.where(Job.status == status)
        count_query = count_query.where(Job.status == status)

    query = query.order_by(Job.submitted_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    jobs = result.scalars().all()

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # All jobs belong to the authenticated user, so resolve their URL template once.
    url_template = await db.scalar(
        select(User.external_url_template).where(User.username == username)
    )

    return JobListResponse(
        jobs=[
            JobStatus(
                job_id=j.id,
                external_id=j.external_id,
                status=j.status,
                fmt=j.fmt,
                domain=j.domain,
                submitted_at=j.submitted_at,
                started_at=j.started_at,
                finished_at=j.finished_at,
                error=j.error,
                external_url=render_external_url(url_template, j.external_id),
            )
            for j in jobs
        ],
        total=total or 0,
    )
