"""Pydantic schemas for job submit/status/result responses."""

from uuid import UUID

from pydantic import BaseModel

from app.clock import UtcDatetime
from app.vocabulary import JobStatus


class JobSubmitResponse(BaseModel):
    job_id: UUID
    # Your label from the submit, echoed back; None if you sent none.
    external_id: str | None = None
    status: JobStatus


class JobStatusResponse(BaseModel):
    job_id: UUID
    external_id: str | None = None
    status: JobStatus
    fmt: str
    domain: str | None
    # Requested outputs; None for jobs submitted before outputs were stored.
    outputs: list[str] | None = None
    submitted_at: UtcDatetime
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None
    error: str | None


class JobResultEntry(BaseModel):
    fmt: str
    url: str


class JobResultResponse(BaseModel):
    # A failed job returns 200 with status="failed" + error and no results, rather than a
    # 5xx (an engine-side failure is not a fault of this API). The status endpoint already
    # exposes `error`, so this surfaces nothing new.
    status: JobStatus = JobStatus.DONE
    error: str | None = None
    results: list[JobResultEntry] = []


class JobListResponse(BaseModel):
    jobs: list[JobStatusResponse]
    total: int
