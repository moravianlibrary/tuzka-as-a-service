import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class JobSubmitResponse(BaseModel):
    job_id: UUID
    external_id: UUID
    status: str


# The placeholder in a user's external_url_template that gets the job's external_id.
# Case-insensitive so both {UUID} (documented) and {uuid} work.
_UUID_PLACEHOLDER = re.compile(r"\{uuid\}", re.IGNORECASE)


def render_external_url(template: str | None, external_id) -> str | None:
    """Resolve a user's ``external_url_template`` for a job by substituting the
    ``{UUID}`` placeholder with the job's ``external_id`` (case-insensitive, so
    ``{uuid}`` also works). Returns ``None`` when no template is configured."""
    if not template or external_id is None:
        return None
    return _UUID_PLACEHOLDER.sub(lambda _: str(external_id), template)


class JobStatus(BaseModel):
    job_id: UUID
    external_id: UUID
    status: str
    fmt: str
    domain: str | None
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    external_url: str | None = None


class JobResultEntry(BaseModel):
    fmt: str
    url: str


class JobResultResponse(BaseModel):
    results: list[JobResultEntry]


class JobListResponse(BaseModel):
    jobs: list[JobStatus]
    total: int
