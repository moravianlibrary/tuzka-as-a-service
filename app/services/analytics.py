"""Shared helpers for writing job_analytics rows and parsing ALTO output."""

import logging
import uuid
from datetime import datetime
from xml.etree import ElementTree

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def parse_alto(alto_bytes: bytes) -> tuple[int, int]:
    """Return (blocks, chars) for an ALTO XML document.

    Blocks = TextBlock count, chars = sum of String CONTENT lengths. The line count is
    taken from the engine's ``n_lines`` (not parsed here). Returns (0, 0) on parse failure."""
    try:
        root = ElementTree.fromstring(alto_bytes)
        tag = root.tag
        if "}" in tag:
            ns_uri = tag.split("}")[0].lstrip("{")
            ns = {"a": ns_uri}
            blocks = len(root.findall(".//a:TextBlock", ns))
            chars = sum(len(e.get("CONTENT", "")) for e in root.findall(".//a:String", ns))
        else:
            blocks = len(root.findall(".//TextBlock"))
            chars = sum(len(e.get("CONTENT", "")) for e in root.findall(".//String"))
        return blocks, chars
    except Exception as exc:
        logger.warning("ALTO parse failed: %s", exc)
        return 0, 0


def _dur_s(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return delta if delta >= 0 else None


async def _get_or_create_id(db: AsyncSession, table: str, name: str) -> int | None:
    """Upsert a name into a lookup table and return its id.

    Does not catch DB errors: it runs inside the SAVEPOINT opened by
    ``write_analytics_row``, so a failure here cleanly rolls that savepoint back
    without poisoning the caller's transaction."""
    if not name:
        return None
    await db.execute(
        text(f"INSERT INTO {table} (name) VALUES (:name) ON CONFLICT (name) DO NOTHING"),
        {"name": name},
    )
    row = await db.execute(text(f"SELECT id FROM {table} WHERE name = :name"), {"name": name})
    return row.scalar_one()


async def write_analytics_row(
    db: AsyncSession,
    *,
    job_id: uuid.UUID | str,
    external_id: uuid.UUID | str | None,
    submitted_at: datetime,
    username: str,
    engine_version: str | None,
    engine_device: str | None,
    backend_id: int | None,
    domain: str | None,
    fmt: str | None,
    status: str,
    file_size_bytes: int | None,
    dispatched_at: datetime | None,
    engine_received_at: datetime | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    stored_at: datetime | None,
    alto_lines: int | None = None,
    alto_blocks: int | None = None,
    alto_chars: int | None = None,
    mean_conf: float | None = None,
) -> None:
    """Insert one row into job_analytics; best-effort.

    The whole write runs inside a SAVEPOINT (``db.begin_nested``) so that any DB
    error — a bad enum cast, a lookup failure, a transient conflict — rolls back
    only this row and leaves the caller's surrounding transaction (the harvest
    that already stored the OCR result) intact and committable."""
    try:
        async with db.begin_nested():
            # Resolve FKs
            user_row = await db.execute(
                text("SELECT id FROM users WHERE username = :u"), {"u": username}
            )
            user_id: int | None = user_row.scalar_one_or_none()

            engine_version_id = await _get_or_create_id(db, "engine_versions", engine_version or "")
            domain_id = await _get_or_create_id(db, "domains", domain or "") if domain else None

            stat_date = submitted_at.date()
            # Each span stays on one clock: the taas-queue wait spans two taas stamps
            # (submitted -> dispatched), the engine-queue wait two engine stamps
            # (engine_received -> started). The dispatched -> engine_received gap (network
            # + clock skew) is intentionally not attributed to either queue.
            system_queue_s = _dur_s(submitted_at, dispatched_at)
            engine_queue_s = _dur_s(engine_received_at, started_at)
            ocr_running_s = _dur_s(started_at, finished_at)
            time_in_system_s = _dur_s(submitted_at, stored_at)

            # Clamp engine_device to the enum values; unknown → cpu
            valid_devices = {"gpu", "cpu"}
            safe_device = engine_device if engine_device in valid_devices else "cpu"

            await db.execute(
                text(
                    "INSERT INTO job_analytics ("
                    "  job_id, external_id, submitted_at, stat_date,"
                    "  user_id, engine_version_id, engine_device, backend_id, domain_id,"
                    "  fmt, status, file_size_bytes,"
                    "  system_queue_s, engine_queue_s, ocr_running_s, time_in_system_s,"
                    "  alto_lines, alto_blocks, alto_chars, mean_conf"
                    ") VALUES ("
                    "  :job_id, :external_id, :submitted_at, :stat_date,"
                    "  :user_id, :engine_version_id, CAST(:engine_device AS engine_device_t), :backend_id, :domain_id,"
                    "  :fmt, CAST(:status AS job_status_t), :file_size_bytes,"
                    "  :system_queue_s, :engine_queue_s, :ocr_running_s, :time_in_system_s,"
                    "  :alto_lines, :alto_blocks, :alto_chars, :mean_conf"
                    ") ON CONFLICT (job_id) DO NOTHING"
                ),
                {
                    "job_id": str(job_id),
                    "external_id": str(external_id) if external_id else None,
                    "submitted_at": submitted_at,
                    "stat_date": stat_date,
                    "user_id": user_id,
                    "engine_version_id": engine_version_id,
                    "engine_device": safe_device if engine_device is not None else None,
                    "backend_id": backend_id,
                    "domain_id": domain_id,
                    "fmt": fmt,
                    "status": status,
                    "file_size_bytes": file_size_bytes,
                    "system_queue_s": system_queue_s,
                    "engine_queue_s": engine_queue_s,
                    "ocr_running_s": ocr_running_s,
                    "time_in_system_s": time_in_system_s,
                    "alto_lines": alto_lines,
                    "alto_blocks": alto_blocks,
                    "alto_chars": alto_chars,
                    "mean_conf": mean_conf,
                },
            )
    except Exception as exc:
        logger.error("Failed to write analytics row for job %s: %s", job_id, exc)
