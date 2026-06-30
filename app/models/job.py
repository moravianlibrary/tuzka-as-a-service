import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(ForeignKey("users.username"), nullable=False)
    external_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(default="queued", nullable=False)
    fmt: Mapped[str] = mapped_column(default="multi", nullable=False)
    domain: Mapped[str | None] = mapped_column(default=None)
    engine_job_id: Mapped[str | None] = mapped_column(default=None)
    engine_version: Mapped[str | None] = mapped_column(default=None)
    backend_id: Mapped[int | None] = mapped_column(ForeignKey("backends.id"), default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    requeues: Mapped[int] = mapped_column(default=0, nullable=False, server_default="0")
    file_size_bytes: Mapped[int | None] = mapped_column(default=None)
    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    # dispatched_at is on the taas clock (submit worker POST); engine_received_at /
    # started_at / finished_at are on the engine clock (adopted from GET /status). Keeping
    # the two queue boundaries separate keeps each derived span on a single clock.
    dispatched_at: Mapped[datetime | None] = mapped_column(default=None)
    engine_received_at: Mapped[datetime | None] = mapped_column(default=None)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    stored_at: Mapped[datetime | None] = mapped_column(default=None)

    results: Mapped[list["JobResult"]] = relationship(back_populates="job")

    __table_args__ = (
        Index("ix_jobs_username_submitted", "username", submitted_at.desc()),
        Index("ix_jobs_status", "status"),
        UniqueConstraint("username", "external_id", name="uq_jobs_username_external"),
    )


class JobResult(Base):
    __tablename__ = "job_results"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    fmt: Mapped[str] = mapped_column(primary_key=True)
    presigned_url: Mapped[str | None] = mapped_column(default=None)
    presigned_until: Mapped[datetime | None] = mapped_column(default=None)

    job: Mapped["Job"] = relationship(back_populates="results")
