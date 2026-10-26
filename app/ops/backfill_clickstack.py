"""Backfill ClickHouse's ``taas.job_events`` from Postgres ``job_analytics`` (step 5, D7).

One ``done``/``failed`` event per historical job, so the dashboards cover the time
before events were emitted live. Run it once, in-cluster where both databases are
reachable, e.g. ``kubectl exec deploy/<release>-api -- python -m app.ops.backfill_clickstack``.
It reads with ``DATABASE_URL`` and writes with ``CLICKHOUSE_*``, like the app.

Rows are streamed in keyset order on ``(submitted_at, job_id)`` (``job_analytics`` has
no integer id), 10 000 per batch, each posted as one insert that must succeed. It
prints the cursor after every batch; ``--after`` resumes from one. ``--dry-run`` only
counts. Historical rows carry no error detail, so a failed one has ``error_class=''``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from app.config import Settings
from app.models.db import make_engine
from app.services.events import JobEvent, job_event_row
from app.vocabulary import Fmt, JobEventName, JobStatus

BATCH_ROWS = 10_000

_SELECT = """
    SELECT ja.job_id, ja.submitted_at, ja.status::text AS status, ja.fmt,
           ja.engine_device::text AS device, ja.backend_id, ja.file_size_bytes,
           ja.system_queue_s, ja.engine_queue_s, ja.ocr_running_s, ja.time_in_system_s,
           ja.alto_lines, ja.alto_blocks, ja.alto_chars, ja.mean_conf,
           u.username, d.name AS domain, ev.name AS engine_version, b.adapter, b.label AS backend
    FROM job_analytics ja
    LEFT JOIN users u ON u.id = ja.user_id
    LEFT JOIN domains d ON d.id = ja.domain_id
    LEFT JOIN engine_versions ev ON ev.id = ja.engine_version_id
    LEFT JOIN backends b ON b.id = ja.backend_id
    WHERE (ja.submitted_at, ja.job_id) > (:after_ts, CAST(:after_id AS uuid))
    ORDER BY ja.submitted_at, ja.job_id
    LIMIT :limit
"""


def event_from_analytics(row: Mapping[str, Any]) -> JobEvent:
    """A historical ``job_analytics`` row as the ``done``/``failed`` event it ended with.
    Its time is when the job was stored (submitted + time in system), or submitted if
    that's unknown."""
    total_s = row["time_in_system_s"]
    ts = row["submitted_at"] + timedelta(seconds=total_s or 0)
    try:
        outputs = sorted(Fmt(row["fmt"] or Fmt.MULTI).outputs)
    except ValueError:
        outputs = []
    return JobEvent(
        ts=ts,
        event=JobEventName.FAILED if row["status"] == JobStatus.FAILED else JobEventName.DONE,
        job_id=str(row["job_id"]),
        username=row["username"] or "",
        domain=row["domain"] or "default",
        outputs=outputs,
        backend_id=row["backend_id"],
        backend=row.get("backend")
        or (str(row["backend_id"]) if row["backend_id"] is not None else ""),
        adapter=row["adapter"] or "",
        engine_version=row["engine_version"] or "",
        device=row["device"] or "",
        error_flag=row["status"] == JobStatus.FAILED,
        file_size_bytes=row["file_size_bytes"],
        queue_s=row["system_queue_s"],
        engine_queue_s=row["engine_queue_s"],
        run_s=row["ocr_running_s"],
        total_s=total_s,
        lines=row["alto_lines"],
        blocks=row["alto_blocks"],
        chars=row["alto_chars"],
        mean_conf=row["mean_conf"],
    )


def parse_cursor(value: str) -> tuple[datetime, str]:
    """``SUBMITTED_AT/JOB_ID`` (as printed after each batch) → the keyset cursor."""
    ts, _, job_id = value.partition("/")
    return datetime.fromisoformat(ts), job_id


async def run(after: tuple[datetime, str], dry_run: bool) -> int:
    settings = Settings()
    engine = make_engine(settings)
    after_ts, after_id = after
    try:
        if dry_run:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT status::text, count(*) FROM job_analytics "
                        "WHERE (submitted_at, job_id) > (:t, CAST(:i AS uuid)) GROUP BY 1"
                    ),
                    {"t": after_ts, "i": after_id},
                )
                for status, count in rows.all():
                    print(f"{status}: {count}")
            return 0

        if not settings.clickhouse_url:
            print("CLICKHOUSE_URL is not set", file=sys.stderr)
            return 1
        query = f"INSERT INTO {settings.clickhouse_database}.job_events FORMAT JSONEachRow"
        auth = (settings.clickhouse_user, settings.clickhouse_password)
        total = 0
        async with httpx.AsyncClient(timeout=120.0) as http:
            while True:
                async with engine.connect() as conn:
                    result = await conn.execute(
                        text(_SELECT),
                        {"after_ts": after_ts, "after_id": after_id, "limit": BATCH_ROWS},
                    )
                    page = result.mappings().all()
                if not page:
                    break
                body = "\n".join(
                    json.dumps(job_event_row(event_from_analytics(dict(r)))) for r in page
                )
                resp = await http.post(
                    f"{settings.clickhouse_url.rstrip('/')}/",
                    params={"query": query},
                    content=body,
                    auth=auth,
                )
                resp.raise_for_status()  # a backfill must not drop rows silently
                total += len(page)
                after_ts, after_id = page[-1]["submitted_at"], str(page[-1]["job_id"])
                print(f"{total} rows; resume with --after {after_ts.isoformat()}/{after_id}")
        print(f"done: {total} rows")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--after",
        type=parse_cursor,
        default=(datetime.min, "00000000-0000-0000-0000-000000000000"),
        help="resume after this SUBMITTED_AT/JOB_ID cursor (printed after each batch)",
    )
    parser.add_argument("--dry-run", action="store_true", help="only count what's left")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.after, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
