"""Job events for ClickHouse (``taas.job_events``): one row per job state change.

The analytics store moves out of Postgres (step 5): every state change becomes an
event row in ClickHouse, which HyperDX dashboards read. The row carries durations,
metrics and an error flag/class, but **no error text** — that stays in Postgres with the
job. This module is pure: the dataclass, the builders and the classification. The
emitter that ships rows lives in ``app.services.event_emitter``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

EventName = Literal["submitted", "dispatched", "retried", "done", "failed"]

# What went wrong, coarse enough to chart; '' when nothing did.
ErrorClass = Literal[
    "http_4xx", "http_5xx", "transport", "engine_input", "engine_internal", "timeout", ""
]


def _dur_s(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return delta if delta >= 0 else None


def error_class(stage: str, http_status: int | None, transport: bool) -> ErrorClass:
    """Classify a failure for the ``error_class`` column.

    ``stage`` is a retry-policy stage (``dispatch``, ``poll``, ``engine``, ``harvest``)
    or ``timeout`` (the reaper). An engine-reported failure is ``engine_input`` when the
    engine blamed the input (4xx) and ``engine_internal`` otherwise, including when it
    gave no status. A transport failure (no HTTP response) is ``transport``.
    """
    if stage == "timeout":
        return "timeout"
    if transport:
        return "transport"
    if stage == "engine":
        blamed_input = http_status is not None and http_status < 500
        return "engine_input" if blamed_input else "engine_internal"
    if http_status is None:
        return ""
    return "http_5xx" if http_status >= 500 else "http_4xx"


@dataclass(frozen=True)
class JobEvent:
    """One ``taas.job_events`` row (step 5, D2)."""

    ts: datetime
    event: EventName
    job_id: str
    username: str
    domain: str
    outputs: list[str] = field(default_factory=list)
    batch_id: str | None = None
    backend_id: int | None = None
    adapter: str = ""
    engine_version: str = ""
    device: str = ""
    error_flag: bool = False
    error_class: ErrorClass = ""
    http_status: int | None = None
    file_size_bytes: int | None = None
    # Durations in seconds; each spans one clock (see durations()).
    queue_s: float | None = None
    engine_queue_s: float | None = None
    run_s: float | None = None
    store_s: float | None = None
    total_s: float | None = None
    lines: int | None = None
    blocks: int | None = None
    chars: int | None = None
    mean_conf: float | None = None
    # Adapter-specific metrics (step 9), as strings.
    extra: dict[str, str] = field(default_factory=dict)


def durations(
    submitted_at: datetime | None,
    dispatched_at: datetime | None,
    engine_received_at: datetime | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    stored_at: datetime | None,
) -> dict[str, float | None]:
    """The ``*_s`` columns from a job's timestamps.

    Each span stays on one clock: the taas queue spans two taas stamps (submitted →
    dispatched), the engine queue and run two engine stamps. The dispatched → engine
    received gap (network + clock skew) isn't attributed to either queue.
    """
    return {
        "queue_s": _dur_s(submitted_at, dispatched_at),
        "engine_queue_s": _dur_s(engine_received_at, started_at),
        "run_s": _dur_s(started_at, finished_at),
        "store_s": _dur_s(finished_at, stored_at),
        "total_s": _dur_s(submitted_at, stored_at or finished_at),
    }


def _ch_datetime(value: datetime) -> str:
    # ClickHouse's default DateTime64 input format (no 'T', no zone: naive UTC, as
    # everywhere in taas), so the insert doesn't depend on best_effort parsing.
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def job_event_row(e: JobEvent) -> dict[str, Any]:
    """The event as a ``JSONEachRow`` object: timestamps in ClickHouse's format, the
    flag as 0/1, ``None`` kept (Nullable columns)."""
    row = asdict(e)
    row["ts"] = _ch_datetime(e.ts)
    row["error_flag"] = int(e.error_flag)
    return row
