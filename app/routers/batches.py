"""Batch endpoints (step 7): submit an archive of pages, watch it, download one results
archive. Each page is an ordinary job (``jobs.batch_id``); the batch adds the archive in
and the archive out."""

import asyncio
import mimetypes
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from miniopy_async.api import Minio
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
    Conflict,
    Forbidden,
    NotFound,
    NotReady,
    PayloadTooLarge,
    Unprocessable,
    UnsupportedMediaType,
)
from app.models.batch import Batch
from app.models.db import get_db
from app.models.job import Job
from app.models.user import User
from app.schemas.api.batch import (
    BatchPage,
    BatchResults,
    BatchRetryResponse,
    BatchStatusResponse,
    BatchSubmitResponse,
)
from app.schemas.redis import BatchCounts, JobState
from app.services import batch_store, redis_jobs, storage
from app.services import config as config_service
from app.services.archive import (
    HEAD_BYTES,
    Member,
    MemberTooLarge,
    detect_archive,
    iter_members,
)
from app.services.batch_archive import PageRow, batch_archive_path, build_results_archive
from app.services.batches import UploadedPage, page_score, plan_batch_member
from app.services.event_emitter import get_emitter
from app.services.events import JobEvent
from app.services.imageinfo import image_megapixels
from app.services.job_spec import (
    fmt_label,
    parse_external_id,
    parse_outputs,
    resolve_job_spec,
)
from app.services.scopes import Principal, check_scope
from app.telemetry import inject_trace_context
from app.vocabulary import (
    DEFAULT_DOMAIN,
    BatchStatus,
    Fmt,
    JobEventName,
    JobStatus,
)

router = APIRouter()

# Job rows are committed this many at a time while the archive is read.
COMMIT_EVERY = 100


@router.post(
    "/batches",
    response_model=BatchSubmitResponse,
    status_code=202,
    summary="Submit an archive of pages as one batch",
    responses={
        400: {"description": "Malformed multipart body"},
        401: {"description": "Missing or invalid X-API-Key header"},
        403: {"description": "The API key isn't allowed this domain or these outputs"},
        413: {"description": "Archive, a page in it, or the page count over its limit"},
        415: {"description": "Not a tar, tar.gz, tar.zst or zip archive"},
        422: {
            "description": "Bad external_id/outputs/options, or no non-empty images in the archive; "
            "empty image members are listed in skipped"
        },
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
        requested = parse_outputs(outputs, Fmt.MULTI)
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

    submitted = time.time()
    batch = Batch(
        id=uuid4(),
        username=username,
        external_id=label,
        domain=spec.domain,
        outputs=sorted(spec.outputs),
        options=spec.options,
        status=BatchStatus.OPEN,
    )
    db.add(batch)
    await db.commit()

    incoming = request.app.state.incoming_client
    members = iter_members(upload, kind, settings.max_upload_bytes)
    stored = await _store_pages(db, incoming, settings, batch, members)
    batch.total = len(stored.pages)
    batch.status = BatchStatus.QUEUED
    await db.commit()

    await _enqueue_pages(db, r, batch, stored.pages, submitted)
    return BatchSubmitResponse(
        batch_id=batch.id, external_id=label, total=len(stored.pages), skipped=stored.skipped
    )


@dataclass(frozen=True, slots=True)
class StoredPages:
    """A batch archive's pages, uploaded and with job rows, in archive order; and the
    members that weren't images."""

    pages: list[UploadedPage]
    skipped: list[str]


async def _store_pages(
    db: AsyncSession,
    incoming: Minio,
    settings: Settings,
    batch: Batch,
    members: Iterator[Member],
) -> StoredPages:
    """Upload every image member and add its job row. A refused or failed archive is
    undone (``_abandon``) before the error goes on; a member over the upload limit is
    a 413."""
    bucket = settings.minio_incoming_bucket
    pages: list[UploadedPage] = []
    skipped: list[str] = []
    try:
        # Archive reading is blocking I/O: one member at a time, in a worker thread.
        while (member := await asyncio.to_thread(next, members, None)) is not None:
            if (
                not member.data
                or plan_batch_member(member.name, settings.allowed_extensions) == "skip"
            ):
                skipped.append(member.name)
                continue
            if len(pages) >= settings.batch_max_pages:
                raise PayloadTooLarge(f"More than {settings.batch_max_pages} pages")
            pages.append(await _store_page(db, incoming, bucket, batch, member))
            if len(pages) % COMMIT_EVERY == 0:
                await db.commit()
        if not pages:
            raise Unprocessable("No images in the archive")
    except Exception as exc:
        await _abandon(db, incoming, bucket, batch.username, batch.id, pages)
        if isinstance(exc, MemberTooLarge):
            raise PayloadTooLarge(str(exc)) from exc
        raise
    return StoredPages(pages, skipped)


async def _store_page(
    db: AsyncSession, incoming: Minio, bucket: str, batch: Batch, member: Member
) -> UploadedPage:
    """Upload one image member and add its job row (committed with the next batch)."""
    page = UploadedPage(
        job_id=uuid4(),
        ext=PurePosixPath(member.name).suffix.lower(),
        megapixels=image_megapixels(member.data),
    )
    content_type = mimetypes.guess_type(member.name)[0] or "application/octet-stream"
    await storage.put_object(
        incoming, bucket, page.incoming_path(batch.username), member.data, content_type
    )
    db.add(
        Job(
            id=page.job_id,
            username=batch.username,
            batch_id=batch.id,
            source_name=member.name,
            status=JobStatus.QUEUED,
            fmt=fmt_label(frozenset(batch.outputs)),
            domain=batch.domain,
            outputs=batch.outputs,
            options=batch.options,
            file_size_bytes=member.size,
            megapixels=page.megapixels,
        )
    )
    return page


async def _enqueue_pages(
    db: AsyncSession,
    r: aioredis.Redis,
    batch: Batch,
    pages: list[UploadedPage],
    submitted: float,
) -> None:
    """Start the batch's counters and queue its pages, in archive order."""
    state_ttl = await config_service.get_state_ttl_seconds(db)
    priority = await _user_priority(db, batch.username)
    await redis_jobs.init_batch(r, BatchCounts(batch_id=str(batch.id), total=len(pages)), state_ttl)
    trace = inject_trace_context()
    emitter = get_emitter()
    outputs = frozenset(batch.outputs)
    for index, page in enumerate(pages):
        state = JobState(
            username=batch.username,
            batch_id=str(batch.id),
            fmt=fmt_label(outputs),
            domain=batch.domain,
            outputs=outputs,
            options=batch.options,
            ext=page.ext,
            megapixels=page.megapixels,
            submitted_at=page_score(submitted, index),
            priority=priority,
            trace=trace,
        )
        job_id = str(page.job_id)
        await redis_jobs.enqueue_job(r, job_id, state, state_ttl)
        emitter.emit(JobEvent.from_state(utcnow(), JobEventName.SUBMITTED, job_id, state))


async def _user_priority(db: AsyncSession, username: str) -> int:
    priority = await db.scalar(select(User.priority).where(User.username == username))
    return int(priority or 0)


async def _abandon(
    db: AsyncSession,
    incoming: Minio,
    bucket: str,
    username: str,
    batch_id: UUID,
    pages: list[UploadedPage],
) -> None:
    """Undo a batch that failed while being read: its uploaded pages and its rows (the
    jobs go with the batch row)."""
    await db.rollback()
    if pages:
        paths = [page.incoming_path(username) for page in pages]
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
    response_model=BatchStatusResponse,
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
) -> BatchStatusResponse:
    """A batch's progress: how many pages are queued, running, done and failed, and a
    rough ETA. ``pages=1`` lists every page (its path in your archive, job id, status,
    error)."""
    batch = await _owned_batch(db, batch_id, username)
    counts = await batch_store.page_counts(db, batch_id)
    # Keep the aggregate running count compatible with existing batch clients;
    # pages=1 exposes the more specific storing state per page.
    queued = counts.get(JobStatus.QUEUED, 0)
    running = counts.get(JobStatus.RUNNING, 0) + counts.get(JobStatus.STORING, 0)
    spp_raw = await r.hget("load:spp", batch.domain)  # seconds per page, if measured
    eta = batch_store.estimate_eta(
        queued + running,
        float(spp_raw) if spp_raw is not None else None,
        await batch_store.domain_slots(db, batch.domain),
    )
    listed = None
    if pages:
        listed = [
            BatchPage(
                source_name=page.source_name or "",
                job_id=page.job_id,
                status=page.status,
                error=page.error,
            )
            for page in await batch_store.pages(db, batch_id)
        ]
    return BatchStatusResponse(
        batch_id=batch.id,
        external_id=batch.external_id,
        status=batch.status,
        domain=batch.domain,
        outputs=batch.outputs,
        total=batch.total,
        queued=queued,
        running=running,
        done=counts.get(JobStatus.DONE, 0),
        failed=counts.get(JobStatus.FAILED, 0),
        submitted_at=batch.submitted_at,
        finished_at=batch.finished_at,
        eta_s=eta if batch.status != BatchStatus.FINISHED else 0.0,
        pages=listed,
    )


@router.get(
    "/batches/{batch_id}/results",
    response_model=BatchResults,
    summary="Download a batch's results as one archive",
    responses={
        202: {"description": "The batch isn't finished yet; retry later (or ?partial=1)"},
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "Batch not found for the authenticated user"},
        429: {"description": "Rate limit exceeded (see Retry-After header)"},
    },
)
async def get_batch_results(
    request: Request,
    batch_id: UUID,
    partial: bool = Query(False, description="Build an archive of the pages done so far."),
    username: str = Depends(rate_limit_query()),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BatchResults:
    """A presigned URL to a ``.tar.zst`` with every page's outputs under ``pages/`` (named
    after the page's path in your archive) and a ``manifest.json`` listing each page:
    ``source_name``, ``job_id``, ``status``, ``outputs`` (output → path in the archive)
    and ``error``.

    The archive is built at the first request after the batch finished, then reused.
    Before that the answer is ``202``, unless ``partial=1``, which builds a fresh archive
    of whatever is done (not kept).
    """
    batch = await _owned_batch(db, batch_id, username)
    if batch.status != BatchStatus.FINISHED and not partial:
        raise NotReady("The batch isn't finished yet")

    results = request.app.state.results_client
    bucket = settings.minio_results_bucket
    final = batch.status == BatchStatus.FINISHED and not partial
    path = batch_archive_path(username, batch_id, partial=not final)
    if not (final and batch.archive_built_at is not None):
        stored = await batch_store.stored_results(db, batch_id)
        rows = [
            PageRow(
                page.job_id,
                page.source_name or "",
                page.status,
                page.error,
                stored.get(page.job_id, []),
            )
            for page in await batch_store.pages(db, batch_id)
        ]
        size = await build_results_archive(rows, username, results, bucket, path)
        if final:
            batch.archive_built_at = utcnow()
            batch.archive_size_bytes = size
            await db.commit()

    ttl = (await config_service.get_runtime_config(db)).presigned_ttl_minutes
    url = await storage.presign_get(request.app.state.results_public_client, bucket, path, ttl)
    return BatchResults(status=batch.status, url=url, expires_at=utcnow() + timedelta(minutes=ttl))


@router.post(
    "/batches/{batch_id}/retry",
    response_model=BatchRetryResponse,
    summary="Run a finished batch's failed pages again",
    responses={
        401: {"description": "Missing or invalid X-API-Key header"},
        404: {"description": "Batch not found for the authenticated user"},
        409: {"description": "The batch isn't finished yet"},
        429: {"description": "Rate limit exceeded (one submit per retry)"},
    },
)
async def retry_batch(
    request: Request,
    batch_id: UUID,
    username: str = Depends(rate_limit_submit()),
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> BatchRetryResponse:
    """Resubmit every failed page of a finished batch as a new job (same
    ``source_name``, new job id), and reopen the batch. The failed jobs stay until their
    TTL; the batch's counts, page list and results archive use each page's newest job.
    A page whose uploaded image has already expired from storage can't be retried; it's
    listed in ``expired``.
    """
    batch = await _owned_batch(db, batch_id, username)
    if batch.status != BatchStatus.FINISHED:
        raise Conflict("Only a finished batch can be retried")

    failed = [
        (page.job_id, page.source_name or "")
        for page in await batch_store.pages(db, batch_id)
        if page.status == JobStatus.FAILED
    ]
    # A retried page is the same image: dispatch scoring and the per-backend speed
    # divide by its megapixels.
    rows = await db.execute(
        select(Job.id, Job.megapixels).where(Job.id.in_([job_id for job_id, _ in failed]))
    )
    megapixels = {job_id: mp for job_id, mp in rows.tuples()}
    incoming = request.app.state.incoming_client
    bucket = settings.minio_incoming_bucket
    retried: list[UploadedPage] = []
    expired: list[str] = []
    for old_id, name in failed:
        # A row from before megapixels were recorded counts as 1 MP, like a fresh job.
        page = UploadedPage(
            uuid4(), PurePosixPath(name).suffix.lower(), megapixels.get(old_id) or 1.0
        )
        src = f"{username}/{old_id}{page.ext}"  # the failed job's upload
        if not await storage.copy_object(incoming, bucket, src, page.incoming_path(username)):
            expired.append(name)
            continue
        db.add(
            Job(
                id=page.job_id,
                username=username,
                batch_id=batch_id,
                source_name=name,
                status=JobStatus.QUEUED,
                fmt=fmt_label(frozenset(batch.outputs)),
                domain=batch.domain,
                outputs=batch.outputs,
                options=batch.options,
                megapixels=megapixels.get(old_id),
            )
        )
        retried.append(page)
    if not retried:
        return BatchRetryResponse(batch_id=batch_id, retried=0, expired=expired)

    batch.status = BatchStatus.QUEUED
    batch.finished_at = None
    batch.archive_built_at = None  # rebuilt with the new results
    await db.commit()

    # The counters restart from what the pages are now (the retried ones are queued).
    counts = await batch_store.page_counts(db, batch_id)
    state_ttl = await config_service.get_state_ttl_seconds(db)
    priority = await _user_priority(db, username)
    restarted = BatchCounts(
        batch_id=str(batch_id),
        total=batch.total,
        done=counts.get(JobStatus.DONE, 0),
        failed=counts.get(JobStatus.FAILED, 0),
    )
    await redis_jobs.init_batch(r, restarted, state_ttl)
    submitted = time.time()
    for index, page in enumerate(retried):
        await redis_jobs.enqueue_job(
            r,
            str(page.job_id),
            JobState(
                username=username,
                batch_id=str(batch_id),
                fmt=fmt_label(frozenset(batch.outputs)),
                domain=batch.domain,
                outputs=frozenset(batch.outputs),
                options=batch.options,
                ext=page.ext,
                megapixels=page.megapixels,
                submitted_at=page_score(submitted, index),
                priority=priority,
            ),
            state_ttl,
        )
    return BatchRetryResponse(batch_id=batch_id, retried=len(retried), expired=expired)
