"""Batch endpoints (step 7): submit an archive of pages, watch it, download one results
archive. Each page is an ordinary job (``jobs.batch_id``); the batch adds the archive in
and the archive out."""

import asyncio
import json
import mimetypes
import time
from pathlib import PurePosixPath
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from miniopy_async import Minio
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import delete, select
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
from app.exceptions import (
    Forbidden,
    NotFound,
    PayloadTooLarge,
    Unprocessable,
    UnsupportedMediaType,
)
from app.models.batch import Batch
from app.models.db import get_db
from app.models.job import Job
from app.models.user import User
from app.schemas.batch import BatchPage, BatchStatus, BatchSubmitResponse
from app.services import batch_store
from app.services import config as config_service
from app.services import redis_jobs, storage
from app.services.archive import HEAD_BYTES, MemberTooLarge, detect_archive, iter_members
from app.services.batches import page_score, plan_batch_member
from app.services.event_emitter import get_emitter
from app.services.events import JobEvent
from app.services.job_spec import (
    DEFAULT_DOMAIN,
    fmt_label,
    parse_external_id,
    parse_outputs,
    resolve_job_spec,
)
from app.services.scopes import Principal, check_scope
from app.telemetry import inject_trace_context

router = APIRouter()

# Job rows are committed this many at a time while the archive is read.
COMMIT_EVERY = 100


@router.post(
    "/batches",
    response_model=BatchSubmitResponse,
    status_code=202,
    summary="Submit an archive of pages as one batch",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        403: {"description": "The API key isn't allowed this domain or these outputs"},
        413: {"description": "Archive, a page in it, or the page count over its limit"},
        415: {"description": "Not a tar, tar.gz, tar.zst or zip archive"},
        422: {"description": "Bad external_id/outputs/options, or no images in the archive"},
        429: {"description": "Rate limit exceeded (one submit per batch)"},
    },
)
async def submit_batch(
    request: Request,
    archive: UploadFile = File(..., description="A .tar, .tar.gz, .tar.zst or .zip of pages."),
    external_id: str | None = Form(None, description="Your own label for the batch."),
    domain: str | None = Form(None),
    outputs: str | None = Form(None, description="Comma-separated, e.g. `alto,txt`."),
    options: str | None = Form(None, description="Engine options as a JSON object."),
    username: str = Depends(rate_limit_submit()),
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> BatchSubmitResponse:
    """Submit many pages at once: one archive, one domain, one set of outputs and options
    for all of them.

    Every image in the archive becomes a job; files that aren't images are skipped and
    listed in ``skipped``. Pages run in archive order. Watch the batch with
    ``GET /batches/{id}`` (or the WebSocket's batch events) and download all results as
    one archive from ``GET /batches/{id}/results``. Pages are identified by their path in
    the archive (``source_name``), not by external ids. Costs one submit from the rate
    limit, however many pages it has.
    """
    length = int(request.headers.get("content-length") or 0)
    if length > settings.batch_max_upload_bytes:
        raise PayloadTooLarge(f"Archive over {settings.batch_max_upload_bytes} bytes")
    try:
        label = parse_external_id(external_id)
        requested = parse_outputs(outputs, "multi")
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    denied = check_scope(principal, domain or DEFAULT_DOMAIN, requested)
    if denied is not None:
        raise Forbidden(denied)
    spec = await resolve_job_spec(db, domain, requested, options)

    # FastAPI spools the upload to a temp file: seekable, and never all in memory.
    upload = archive.file
    kind = detect_archive(upload.read(HEAD_BYTES))
    upload.seek(0)
    if kind is None:
        raise UnsupportedMediaType("Expected a tar, tar.gz, tar.zst or zip archive")

    batch_id = uuid4()
    submitted = time.time()
    batch = Batch(
        id=batch_id,
        username=username,
        external_id=label,
        domain=spec.domain,
        outputs=sorted(spec.outputs),
        options=spec.options,
        status="open",
    )
    db.add(batch)
    await db.commit()

    incoming = request.app.state.incoming_client
    bucket = settings.minio_incoming_bucket
    job_fmt = fmt_label(spec.outputs)
    pages: list[tuple[UUID, str]] = []  # (job id, extension), in archive order
    skipped: list[str] = []
    members = iter_members(upload, kind, settings.max_upload_bytes)
    try:
        while True:
            # Archive reading is blocking I/O: one member at a time, in a worker thread.
            member = await asyncio.to_thread(next, members, None)
            if member is None:
                break
            if plan_batch_member(member.name, settings.allowed_extensions) == "skip":
                skipped.append(member.name)
                continue
            if len(pages) >= settings.batch_max_pages:
                raise PayloadTooLarge(f"More than {settings.batch_max_pages} pages")
            job_id = uuid4()
            ext = PurePosixPath(member.name).suffix.lower()
            content_type = mimetypes.guess_type(member.name)[0] or "application/octet-stream"
            await storage.put_object(
                incoming, bucket, f"{username}/{job_id}{ext}", member.data, content_type
            )
            db.add(
                Job(
                    id=job_id,
                    username=username,
                    batch_id=batch_id,
                    source_name=member.name,
                    status="queued",
                    fmt=job_fmt,
                    domain=spec.domain,
                    outputs=sorted(spec.outputs),
                    options=spec.options,
                    file_size_bytes=member.size,
                )
            )
            pages.append((job_id, ext))
            if len(pages) % COMMIT_EVERY == 0:
                await db.commit()
        if not pages:
            raise Unprocessable("No images in the archive")
    except (MemberTooLarge, PayloadTooLarge, Unprocessable) as exc:
        await _abandon(db, incoming, bucket, username, batch_id, pages)
        if isinstance(exc, MemberTooLarge):
            raise PayloadTooLarge(str(exc)) from exc
        raise
    except Exception:
        await _abandon(db, incoming, bucket, username, batch_id, pages)
        raise

    batch.total = len(pages)
    batch.status = "queued"
    await db.commit()

    state_ttl = await config_service.get_state_ttl_seconds(db)
    priority = await _user_priority(db, username)
    await redis_jobs.init_batch(r, str(batch_id), len(pages), state_ttl)
    trace = inject_trace_context()
    emitter = get_emitter()
    for index, (job_id, ext) in enumerate(pages):
        await redis_jobs.enqueue_job(
            r,
            str(job_id),
            {
                "username": username,
                "external_id": "",
                "batch_id": str(batch_id),
                "fmt": job_fmt,
                "domain": spec.domain,
                "outputs": ",".join(sorted(spec.outputs)),
                "options": json.dumps(spec.options),
                "ext": ext,
                "submitted_at": str(page_score(submitted, index)),
                "priority": str(priority),
                **trace,
            },
            state_ttl,
        )
        emitter.emit(
            JobEvent(
                ts=utcnow(),
                event="submitted",
                job_id=str(job_id),
                batch_id=str(batch_id),
                username=username,
                domain=spec.domain,
                outputs=sorted(spec.outputs),
            )
        )
    return BatchSubmitResponse(
        batch_id=batch_id, external_id=label, total=len(pages), skipped=skipped
    )


async def _user_priority(db: AsyncSession, username: str) -> int:
    priority = await db.scalar(select(User.priority).where(User.username == username))
    return int(priority or 0)


async def _abandon(
    db: AsyncSession,
    incoming: Minio,
    bucket: str,
    username: str,
    batch_id: UUID,
    pages: list[tuple[UUID, str]],
) -> None:
    """Undo a batch that failed while being read: its uploaded pages and its rows (the
    jobs go with the batch row)."""
    await db.rollback()
    if pages:
        paths = [f"{username}/{job_id}{ext}" for job_id, ext in pages]
        await storage.delete_objects(incoming, bucket, paths)
    await db.execute(delete(Batch).where(Batch.id == batch_id))
    await db.commit()


async def _owned_batch(db: AsyncSession, batch_id: UUID, username: str) -> Batch:
    batch = await db.scalar(select(Batch).where(Batch.id == batch_id, Batch.username == username))
    if batch is None:
        raise NotFound("Batch not found")
    return batch


@router.get(
    "/batches/{batch_id}",
    response_model=BatchStatus,
    summary="Get a batch's progress",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "Batch not found for the authenticated user"},
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def get_batch(
    batch_id: UUID,
    pages: bool = Query(False, description="Also list every page with its status."),
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
) -> BatchStatus:
    """A batch's progress: how many pages are queued, running, done and failed, and a
    rough ETA. ``pages=1`` lists every page (its path in your archive, job id, status,
    error)."""
    batch = await _owned_batch(db, batch_id, username)
    counts = await batch_store.page_counts(db, batch_id)
    queued, running = counts.get("queued", 0), counts.get("running", 0)
    spp_raw = await r.hget("load:spp", batch.domain)  # seconds per page, if measured
    eta = batch_store.estimate_eta(
        queued + running,
        float(spp_raw) if spp_raw is not None else None,
        await batch_store.domain_slots(db, batch.domain),
    )
    listed = None
    if pages:
        listed = [
            BatchPage(source_name=name or "", job_id=job_id, status=status, error=error)
            for job_id, name, status, error in await batch_store.pages(db, batch_id)
        ]
    return BatchStatus(
        batch_id=batch.id,
        external_id=batch.external_id,
        status=batch.status,
        domain=batch.domain,
        outputs=batch.outputs,
        total=batch.total,
        queued=queued,
        running=running,
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
        submitted_at=batch.submitted_at,
        finished_at=batch.finished_at,
        eta_s=eta if batch.status != "finished" else 0.0,
        pages=listed,
    )
