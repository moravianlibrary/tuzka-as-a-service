import os
import time
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_redis, get_settings, rate_limit_query, rate_limit_submit
from app.models.db import get_db
from app.models.job import Job, JobResult
from app.schemas.job import (
    JobListResponse,
    JobResultEntry,
    JobResultResponse,
    JobStatus,
    JobSubmitResponse,
)
from app.services import redis_jobs, storage

router = APIRouter()


@router.post("/jobs", response_model=JobSubmitResponse, status_code=202)
async def submit_job(
    image: UploadFile = File(...),
    uuid: str = Form(...),
    fmt: str = Form("multi"),
    domain: str | None = Form(None),
    username: str = Depends(rate_limit_submit()),
    db: AsyncSession = Depends(get_db),
    r=Depends(get_redis),
    settings: Settings = Depends(get_settings),
):
    # Validate UUID
    try:
        external_id = UUID(uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")

    # Validate fmt
    if fmt not in ("alto", "txt", "multi"):
        raise HTTPException(status_code=400, detail="fmt must be alto, txt, or multi")

    # Validate extension
    filename = image.filename or "image"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in settings.allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Extension {ext} not allowed. Allowed: {settings.allowed_extensions}",
        )

    # Validate file size
    image_bytes = await image.read()
    if len(image_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max: {settings.max_upload_bytes} bytes",
        )

    # Upload to MinIO incoming
    incoming_client = storage.get_incoming_client(settings)
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
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Enqueue in Redis
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
        },
    )

    return JobSubmitResponse(job_id=job.id, external_id=external_id, status="queued")


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job_status(
    job_id: UUID,
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).where(Job.id == job_id, Job.username == username))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

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
    )


@router.get("/jobs/{job_id}/result", response_model=JobResultResponse)
async def get_job_result(
    job_id: UUID,
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = await db.execute(select(Job).where(Job.id == job_id, Job.username == username))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == "failed":
        raise HTTPException(status_code=500, detail=job.error or "Job failed")
    if job.status != "done":
        raise HTTPException(status_code=202, detail="Job not completed yet")

    # Get results
    res = await db.execute(select(JobResult).where(JobResult.job_id == job_id))
    job_results = res.scalars().all()

    results_client = storage.get_results_client(settings)
    entries = []
    from datetime import datetime

    now = datetime.utcnow()
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
                settings.presigned_ttl_minutes,
            )
            from datetime import timedelta

            jr.presigned_until = now + timedelta(minutes=settings.presigned_ttl_minutes)
            await db.commit()

        entries.append(JobResultEntry(fmt=jr.fmt, url=jr.presigned_url))

    return JobResultResponse(results=entries)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
):
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
            )
            for j in jobs
        ],
        total=total or 0,
    )
