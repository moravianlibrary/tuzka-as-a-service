"""Pydantic schemas for job submit/status/result responses."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.clock import UtcDatetime


class JobSubmitResponse(BaseModel):
    job_id: UUID
    # Your label from the submit, echoed back; None if you sent none.
    external_id: str | None = None
    status: str


class JobStatus(BaseModel):
    job_id: UUID
    external_id: str | None = None
    status: str
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
    status: str = "done"
    error: str | None = None
    results: list[JobResultEntry] = []


class JobListResponse(BaseModel):
    jobs: list[JobStatus]
    total: int


class DomainEntry(BaseModel):
    name: str
    label: str | None
    description: str | None
    # Union of the outputs the enabled backends produce for this domain.
    outputs: list[str]
    # JSON Schema for the submit `options` field; None = no options accepted yet.
    options_schema: dict[str, Any] | None
    # The engine id the legacy compat API exposes for this domain.
    legacy_engine_id: int | None


class DomainListResponse(BaseModel):
    domains: list[DomainEntry]
