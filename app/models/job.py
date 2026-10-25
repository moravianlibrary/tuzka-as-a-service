"""ORM models for OCR jobs and their per-format results, tracking the full lifecycle."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.vocabulary import JobStatus

from .db import Base
from .types import StrEnumType, check_values


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(ForeignKey("users.username"), nullable=False)
    # The caller's own label for the job (any string, not unique); None = none given.
    external_id: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[JobStatus] = mapped_column(
        StrEnumType(JobStatus), default=JobStatus.QUEUED, nullable=False
    )
    fmt: Mapped[str] = mapped_column(default="multi", nullable=False)
    domain: Mapped[str | None] = mapped_column(default=None)
    # A page of a batch: the batch, and the page's path inside the uploaded archive.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), default=None
    )
    source_name: Mapped[str | None] = mapped_column(default=None)
    outputs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)
    options: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    engine_job_id: Mapped[str | None] = mapped_column(default=None)
    engine_version: Mapped[str | None] = mapped_column(default=None)
    backend_id: Mapped[int | None] = mapped_column(ForeignKey("backends.id"), default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    requeues: Mapped[int] = mapped_column(default=0, nullable=False, server_default="0")
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    file_size_bytes: Mapped[int | None] = mapped_column(default=None)
    # Image size, for dispatch scoring (seconds per megapixel); None before 0.9.
    megapixels: Mapped[float | None] = mapped_column(default=None)
    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    # dispatched_at is on the taas clock (submit worker POST); engine_received_at /
    # started_at / finished_at are on the engine clock (adopted from GET /status). Keeping
    # the two queue boundaries separate keeps each derived span on a single clock.
    dispatched_at: Mapped[datetime | None] = mapped_column(default=None)
    engine_received_at: Mapped[datetime | None] = mapped_column(default=None)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    stored_at: Mapped[datetime | None] = mapped_column(default=None)

    # lazy="raise": every access must be an explicit load (selectinload / a join) — an
    # accidental lazy load is a loud error, not a silent N+1 (and would fail anyway on
    # the async session outside a greenlet context).
    results: Mapped[list["JobResult"]] = relationship(back_populates="job", lazy="raise")

    __table_args__ = (
        Index("ix_jobs_username_submitted", "username", submitted_at.desc()),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_backend_id", "backend_id"),
        Index("ix_jobs_batch_status", "batch_id", "status"),
        CheckConstraint(
            check_values("status", JobStatus),
            name="ck_jobs_status",
        ),
    )


class JobResult(Base):
    __tablename__ = "job_results"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    fmt: Mapped[str] = mapped_column(primary_key=True)
    presigned_url: Mapped[str | None] = mapped_column(default=None)
    presigned_until: Mapped[datetime | None] = mapped_column(default=None)
    # Compressed size as stored; the results storage cap sums it (None: before 0.9).
    size_bytes: Mapped[int | None] = mapped_column(default=None)
    # Set when the object was evicted (max age or the storage cap); the row stays until
    # the job's TTL so its result endpoints can answer 410 instead of 404.
    evicted_at: Mapped[datetime | None] = mapped_column(default=None)

    job: Mapped["Job"] = relationship(back_populates="results", lazy="raise")
