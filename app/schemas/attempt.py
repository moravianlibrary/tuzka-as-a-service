"""One entry of ``jobs.attempts``: a failed dispatch, poll or harvest of a job, kept
in order on the job row (D8). Stored as JSON through ``models.types.PydanticJSONBList``."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.vocabulary import FailureStage


class Attempt(BaseModel):
    model_config = ConfigDict(frozen=True)

    # taas clock, naive UTC like the other job timestamps.
    at: datetime
    stage: FailureStage
    backend_id: int | None
    http_status: int | None
    error: str
    # Whether the job went back to its queue after this attempt.
    retry: bool
