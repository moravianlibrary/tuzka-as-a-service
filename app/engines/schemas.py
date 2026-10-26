"""The HTTP responses both engines' FastAPI apps share: the job id of an accepted
submit, and a job's status. Each engine's own fields extend these in its package."""

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator

from app.engines.base import EngineStatus
from app.vocabulary import EngineState


def _number_or_none(value: Any) -> Any:
    # A metric of the wrong type is dropped, not an error: polling must not fail over
    # a number taas only displays.
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value
    return None


# A numeric metric of a status payload; None when absent or not a number.
Metric = Annotated[int | float | None, BeforeValidator(_number_or_none)]


class JobAccepted(BaseModel):
    """``POST /api/v1/process``."""

    job_id: str


class JobStatusBody(BaseModel):
    """``GET /api/v1/status/{id}``; timestamps are the engine's ISO strings."""

    status: str | None = None
    error: str | None = None
    error_status: int | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def engine_status(self, metrics: dict[str, int | float | str]) -> EngineStatus:
        return EngineStatus(
            state=EngineState.parse(self.status),
            error=self.error,
            error_status=self.error_status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            metrics=metrics,
        )
