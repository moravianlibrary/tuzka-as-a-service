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


def utcnow() -> datetime:
    """Return the current time as a naive UTC ``datetime`` (see module docstring)."""
    return datetime.now(UTC).replace(tzinfo=None)
