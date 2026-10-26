"""Public API-key job endpoints: submit an OCR job and poll its status and results."""

import time
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

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
from sqlalchemy import Integer, Text, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import Settings
from app.deps import (
    get_redis,
    get_settings,
    rate_limit_query,
    rate_limit_submit,
    require_principal,
)
from app.engines.outputs import ext_for
from app.exceptions import (
    BadRequest,
    Conflict,
    Forbidden,
    Gone,
    NotFound,
    NotReady,
    Unprocessable,
)
from app.models.db import get_db
from app.models.job import Job, JobResult
from app.models.user import User
from app.schemas.api.domain import DomainEntry, DomainListResponse
from app.schemas.api.job import (
    JobListResponse,
    JobResultEntry,
    JobResultResponse,
    JobStatusResponse,
    JobSubmitResponse,
)
from app.schemas.redis import JobState
from app.services import config as config_service
from app.services import redis_jobs, storage
from app.services.event_emitter import get_emitter
from app.services.events import JobEvent
from app.services.imageinfo import image_megapixels
from app.services.job_spec import (
    fmt_label,
    parse_external_id,
    parse_outputs,
    resolve_job_spec,
)
from app.services.scopes import Principal, check_scope, visible_outputs
from app.services.uploads import UploadTooLarge, read_limited
from app.telemetry import inject_trace_context
from app.vocabulary import DEFAULT_DOMAIN, Fmt, JobEventName, JobStatus

router = APIRouter()


@router.post(
    "/jobs",
    response_model=JobSubmitResponse,
    status_code=202,
    summary="Submit an OCR job",
    responses={
        400: {"description": "Unsupported extension, file too large, or malformed multipart body"},
        401: {"description": "Missing or invalid X-API-Key header"},
        403: {"description": "The API key isn't allowed this domain or these outputs"},
        422: {
            "description": "Empty image; invalid external_id or fmt; unknown domain; outputs "
            "the domain can't produce; or options that don't match the domain's options schema"
        },
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def submit_job(
    request: Request,
    image: UploadFile = File(...),
    external_id: str | None = Form(
        None,
        description="Your own label for this page (any string up to 255 characters, e.g. "
        "its PID); echoed back on its status and events. Optional, not unique.",
    ),
    fmt: Fmt = Form(Fmt.MULTI),
    domain: str | None = Form(None),
    outputs: str | None = Form(
        None,
        description="Comma-separated outputs, e.g. `alto,txt`. Overrides `fmt`; see "
        "GET /api/v1/domains for what each domain offers.",
    ),
    options: str | None = Form(
        None,
        description="Engine options as a JSON object, validated against the domain's "
        "`options_schema` (GET /api/v1/domains).",
    ),
    username: str = Depends(rate_limit_submit()),
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> JobSubmitResponse:
    """Submit an image for OCR and enqueue it for asynchronous processing.

    Requires a valid API key in the ``X-API-Key`` header. The upload is stored and the job
    is queued, returning ``202 Accepted`` with a job id immediately; poll the status and
    result endpoints to retrieve output once processing finishes.

    A job without a ``domain`` runs in the ``default`` domain. ``outputs`` (or, without
    it, ``fmt``) picks the results to produce, and ``options`` passes engine options; all
    three are checked against ``GET /api/v1/domains`` before the upload is accepted.
    """
    # external_id is the caller's label, echoed back in status and events — not unique,
    # never parsed. The storage key is job.id (the server-generated PK); we mint it here
    # so the upload can use it before the row is inserted.
    try:
        label = parse_external_id(external_id)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    job_id = uuid4()

    # Validate extension
    filename = image.filename or "image"
    ext = Path(filename).suffix.lower()
    if ext not in settings.allowed_extensions:
        raise BadRequest(f"Extension {ext} not allowed. Allowed: {settings.allowed_extensions}")

    # Validate file size: the domain's own limit (maps are big) or the global one.
    domain_limits = (await config_service.get_domain_limits(db)).get(domain or DEFAULT_DOMAIN)
    max_bytes = (
        domain_limits.max_upload_bytes
        if domain_limits is not None and domain_limits.max_upload_bytes is not None
        else settings.max_upload_bytes
    )
    try:
        image_bytes = await read_limited(image.read, max_bytes)
    except UploadTooLarge as exc:
        raise BadRequest(str(exc)) from exc
    if not image_bytes:
        raise Unprocessable("empty image")

    # Domain, outputs, options: validated against what the enabled backends serve (D7).
    try:
        requested = parse_outputs(outputs, fmt)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    # The key's scope first, so an out-of-scope domain isn't probed for existence.
    denied = check_scope(principal, domain or DEFAULT_DOMAIN, requested)
    if denied is not None:
        raise Forbidden(denied)
    spec = await resolve_job_spec(db, domain, requested, options)
    job_fmt = fmt_label(spec.outputs)
    megapixels = image_megapixels(image_bytes)

    # Upload to MinIO incoming
    incoming_client = request.app.state.incoming_client
    object_path = f"{username}/{job_id}{ext}"
    await storage.put_object(
        incoming_client,
        settings.minio_incoming_bucket,
        object_path,
        image_bytes,
        image.content_type or "application/octet-stream",
    )

    # Insert job in Postgres
    job = Job(
        id=job_id,
        username=username,
        external_id=label,
        status=JobStatus.QUEUED,
        fmt=job_fmt,
        domain=spec.domain,
        outputs=sorted(spec.outputs),
        options=spec.options,
        file_size_bytes=len(image_bytes),
        megapixels=megapixels,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Enqueue in Redis — include user priority so the submit worker routes to the right queue
    state_ttl = await config_service.get_state_ttl_seconds(db)
    user_row = await db.execute(select(User).where(User.username == username))
    user_priority = (user_row.scalar_one_or_none() or User()).priority or 0
    state = JobState(
        username=username,
        external_id=label,
        fmt=job_fmt,
        domain=spec.domain,
        outputs=spec.outputs,
        options=spec.options,
        ext=ext,
        megapixels=megapixels,
        submitted_at=time.time(),
        priority=user_priority,
        # The workers continue this request's trace from here (empty when off).
        trace=inject_trace_context(),
    )
    await redis_jobs.enqueue_job(r, str(job.id), state, state_ttl)

    get_emitter().emit(
        JobEvent.from_state(
            utcnow(),
            JobEventName.SUBMITTED,
            str(job.id),
            state,
            file_size_bytes=len(image_bytes),
        )
    )
    return JobSubmitResponse(job_id=job.id, external_id=label, status=JobStatus.QUEUED)


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
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
) -> JobStatusResponse:
    """Return the current status of one of your jobs.

    Requires a valid API key in the ``X-API-Key`` header and only resolves jobs owned by the
    authenticated user. The status reflects the asynchronous lifecycle (``queued``,
    ``running``, ``storing``, ``done``, ``failed``); ``storing`` means the engine has
    finished and its results await storage. Fetch results once it is ``done``.
    """
    job = await db.scalar(select(Job).where(Job.id == job_id, Job.username == username))
    if job is None:
        raise NotFound("Job not found")

    return JobStatusResponse(
        job_id=job.id,
        external_id=job.external_id,
        status=job.status,
        fmt=job.fmt,
        domain=job.domain,
        outputs=job.outputs,
        submitted_at=job.submitted_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
    )


@router.get(
    "/jobs/{job_id}/result",
    response_model=JobResultResponse,
    summary="Download job result",
    responses={
        202: {"description": "Job accepted but not finished yet; retry later"},
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "Job not found for the authenticated user"},
        410: {"description": "The results expired (max age or the storage cap)"},
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

    if job.status == JobStatus.FAILED:
        return JobResultResponse(status=JobStatus.FAILED, error=job.error or "Job failed")
    if job.status != JobStatus.DONE:
        raise NotReady("Job not completed yet")

    # Get results
    res = await db.execute(select(JobResult).where(JobResult.job_id == job_id))
    job_results = res.scalars().all()
    if job_results and all(jr.evicted_at is not None for jr in job_results):
        raise Gone("Results expired")

    results_client = request.app.state.results_public_client
    entries = []
    now = utcnow()
    presigned_ttl = (await config_service.get_runtime_config(db)).presigned_ttl_minutes
    for jr in job_results:
        # Refresh presigned URL if expired (stored as naive UTC)
        if not jr.presigned_url or (jr.presigned_until and jr.presigned_until < now):
            file_ext = ext_for(jr.fmt)
            obj_path = f"{username}/{job.id}.{file_ext}.zst"
            jr.presigned_url = await storage.presign_get(
                results_client,
                settings.minio_results_bucket,
                obj_path,
                presigned_ttl,
            )
            jr.presigned_until = now + timedelta(minutes=presigned_ttl)
            await db.commit()

        entries.append(JobResultEntry(fmt=jr.fmt, url=jr.presigned_url))

    return JobResultResponse(status=JobStatus.DONE, results=entries)


@router.get(
    "/jobs/{job_id}/result/{fmt}/download",
    summary="Download a finished job's stored OCR artifact",
    responses={
        202: {"description": "Job accepted but not finished yet; retry later"},
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "Job not found, or no result in the requested format"},
        410: {"description": "The result expired (max age or the storage cap)"},
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

    if job.status == JobStatus.FAILED:
        raise Conflict(job.error or "Job failed")
    if job.status != JobStatus.DONE:
        raise NotReady("Job not completed yet")

    res = await db.execute(
        select(JobResult).where(JobResult.job_id == job_id, JobResult.fmt == fmt)
    )
    stored = res.scalar_one_or_none()
    if not stored:
        raise NotFound(f"No {fmt} result available")
    if stored.evicted_at is not None:
        raise Gone(f"The {fmt} result expired")

    obj_path = f"{username}/{job.id}.{ext_for(fmt)}.zst"
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
    status: JobStatus | None = None,
    external_id: str | None = Query(
        None, max_length=255, description="Only jobs submitted with this external_id."
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
) -> JobListResponse:
    """List the authenticated user's jobs, newest first.

    Requires a valid API key in the ``X-API-Key`` header and returns only jobs owned by that
    user. Results can be filtered by ``status`` and ``external_id`` and are paginated with
    ``limit`` and ``offset``; ``total`` reports the full count matching the filter.
    """
    query = select(Job).where(Job.username == username)
    count_query = select(func.count()).select_from(Job).where(Job.username == username)

    if status:
        query = query.where(Job.status == status)
        count_query = count_query.where(Job.status == status)
    if external_id is not None:
        try:
            parse_external_id(external_id)
        except ValueError:
            # Submit refuses such a label, so no job has it. Answering without the query
            # also keeps a NUL away from Postgres, which rejects it (that was a 500).
            return JobListResponse(jobs=[], total=0)
        query = query.where(Job.external_id == external_id)
        count_query = count_query.where(Job.external_id == external_id)

    query = query.order_by(Job.submitted_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    jobs = result.scalars().all()

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    return JobListResponse(
        jobs=[
            JobStatusResponse(
                job_id=j.id,
                external_id=j.external_id,
                status=j.status,
                fmt=j.fmt,
                domain=j.domain,
                outputs=j.outputs,
                submitted_at=j.submitted_at,
                started_at=j.started_at,
                finished_at=j.finished_at,
                error=j.error,
            )
            for j in jobs
        ],
        total=total or 0,
    )


# One row per domain served by at least one enabled backend; outputs are unnested so
# a domain served by several backends reports the union of what they produce.
_SERVED_DOMAINS_SQL = text(
    """
    SELECT d.name, d.label, d.description, d.options_schema, d.legacy_engine_id,
           array_agg(DISTINCT o.output ORDER BY o.output) AS outputs
    FROM domains d
    JOIN backend_domains bd ON bd.domain_id = d.id
    JOIN backends b ON b.id = bd.backend_id AND b.enabled
    CROSS JOIN LATERAL unnest(bd.outputs) AS o(output)
    GROUP BY d.id
    ORDER BY d.name
    """
).columns(
    name=Text,
    label=Text,
    description=Text,
    options_schema=JSONB,
    legacy_engine_id=Integer,
    outputs=ARRAY(Text),
)


@router.get(
    "/domains",
    response_model=DomainListResponse,
    summary="List the domains you can submit to",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def list_domains(
    username: str = Depends(rate_limit_query()),
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> DomainListResponse:
    """List the domains served by at least one enabled backend that your key may use.

    Requires a valid API key in the ``X-API-Key`` header. For each domain, ``outputs``
    is what a job may request in ``outputs`` (the union over the backends serving it,
    limited to what your key is allowed), and ``options_schema`` is the JSON Schema the
    submit ``options`` field is validated against, derived from the engine's own OpenAPI.
    A domain your key can't use isn't listed.
    """
    rows = (await db.execute(_SERVED_DOMAINS_SQL)).mappings().all()
    entries = []
    for r in rows:
        outputs = visible_outputs(principal, r["name"], frozenset(r["outputs"]))
        if not outputs:
            continue
        entries.append(
            DomainEntry(
                name=r["name"],
                label=r["label"],
                description=r["description"],
                outputs=[o for o in r["outputs"] if o in outputs],  # keep the SQL's order
                options_schema=r["options_schema"],
                legacy_engine_id=r["legacy_engine_id"],
            )
        )
    return DomainListResponse(domains=entries)
