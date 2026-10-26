"""Pydantic schemas for job submit/status/result responses, and the admin views of a job."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.clock import UtcDatetime
from app.schemas.attempt import Attempt
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


class FailedJob(BaseModel):
    """A row of ``taas-admin job list-failed``."""

    id: UUID
    username: str
    external_id: str | None
    domain: str | None
    backend_id: int | None
    finished_at: datetime | None
    error: str | None


class JobDetail(BaseModel):
    """``taas-admin job show``: the job row with every failed attempt."""

    id: UUID
    username: str
    external_id: str | None
    status: JobStatus
    domain: str | None
    outputs: list[str] | None
    options: dict[str, Any] | None
    backend_id: int | None
    engine_version: str | None
    requeues: int
    submitted_at: datetime
    dispatched_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    attempts: list[Attempt]
