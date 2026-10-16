"""SQL helpers for appending to ``jobs.attempts``.

Every worker uses these when recording a failed attempt so the same
JSONB-append logic lives in one place.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import literal
from sqlalchemy.dialects.postgresql import JSONB

from app.models.job import Job


def append_attempt(entry: dict[str, Any]) -> dict[str, Any]:
    """Return values for ``update(Job).values(...)``: appends *entry* to ``jobs.attempts``.

    Postgres ``jsonb || jsonb`` concatenates in SQL, so concurrent appends never
    overwrite each other the way a read-modify-write in Python would.
    """
    # A list bound as JSONB is serialized once by the type; casting a json.dumps()
    # string instead double-encodes it and appends a JSON *string*, not an object.
    return {"attempts": Job.attempts.op("||")(literal([entry], JSONB))}
