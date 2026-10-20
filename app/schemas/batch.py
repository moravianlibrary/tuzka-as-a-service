"""Pydantic schemas for the batch endpoints (step 7)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class BatchSubmitResponse(BaseModel):
    batch_id: UUID
    # Your label from the submit; None if you sent none.
    external_id: str | None = None
    total: int
    # Archive members that weren't images (wrong extension), left out of the batch.
    skipped: list[str] = []


class BatchPage(BaseModel):
    source_name: str  # the page's path in the archive you sent
    job_id: UUID
    status: str
    error: str | None = None


class BatchStatus(BaseModel):
    batch_id: UUID
    external_id: str | None = None
    status: str = Field(description="open (still reading the archive), queued or finished")
    domain: str
    outputs: list[str]
    total: int
    queued: int
    running: int
    done: int
    failed: int
    submitted_at: datetime
    finished_at: datetime | None = None
    eta_s: float | None = Field(
        default=None,
        description="A rough estimate of the seconds left, from the queue and recent "
        "engine speed; None when unknown.",
    )
    pages: list[BatchPage] | None = None


class BatchResults(BaseModel):
    status: str = "finished"
    url: str
    expires_at: datetime
