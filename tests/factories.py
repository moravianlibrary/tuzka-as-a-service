"""Builders for the typed values tests hand to the code under test."""

import time
from typing import Any

from app.schemas.redis import JobState


def job_state(**fields: Any) -> JobState:
    """A submitted job's Redis state; ``fields`` override the defaults."""
    defaults: dict[str, Any] = {
        "username": "alice",
        "fmt": "multi",
        "outputs": frozenset({"alto", "txt"}),
        "submitted_at": time.time(),
    }
    return JobState(**(defaults | fields))
