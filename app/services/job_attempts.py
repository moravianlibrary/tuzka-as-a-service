"""SQL helpers for appending to ``jobs.attempts``.

Every worker uses these when recording a failed attempt so the same
JSONB-append logic lives in one place.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB

from app.models.job import Job


def append_attempt(entry: dict[str, Any]) -> dict[str, Any]:
    """Return values for ``update(Job).values(...)``: appends *entry* to ``jobs.attempts``.

    Uses ``jsonb_path_append`` via ``||`` so the append is atomic in SQL.
    The resulting dict is passed to ``update(...).values(**append_attempt(entry))``.
    """
    return {"attempts": Job.attempts.op("||")(cast(json.dumps([entry]), JSONB))}
