from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class JobSubmitResponse(BaseModel):
    job_id: UUID
    external_id: UUID
    status: str


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


class JobResultEntry(BaseModel):
    fmt: str
    url: str


class JobResultResponse(BaseModel):
    results: list[JobResultEntry]


class JobListResponse(BaseModel):
    jobs: list[JobStatus]
    total: int
