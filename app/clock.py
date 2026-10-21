"""Time helpers.

taas stores every timestamp as **naive UTC** — a deliberate, documented exception to
the house rule of timezone-aware UTC. The store is uniformly naive (DB columns are
``TIMESTAMP WITHOUT TIME ZONE`` and the poller normalizes engine-reported stamps to
naive), so a single naive clock keeps each lifecycle span single-clock and never
negative on sub-second waits. Attach an offset only at the display edge.

Always use ``utcnow()`` here rather than the deprecated ``datetime.utcnow()`` so the
naive-UTC choice stays explicit and lives in exactly one place.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator


def utcnow() -> datetime:
    """Return the current time as a naive UTC ``datetime`` (see module docstring)."""
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(dt: datetime) -> datetime:
    """Mark a stored (naive UTC) datetime as UTC; an aware one is converted to UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def utc_iso(dt: datetime) -> str:
    """ISO 8601 in UTC with a ``Z``, the form the API returns (``…T08:02:19.668317Z``)."""
    return as_utc(dt).isoformat().replace("+00:00", "Z")


# A datetime field of an API response: the display edge, where the offset is attached.
# Pydantic writes an aware UTC datetime with a trailing ``Z``.
UtcDatetime = Annotated[datetime, AfterValidator(as_utc)]
