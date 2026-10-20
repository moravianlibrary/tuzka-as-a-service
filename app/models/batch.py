"""ORM model for a batch: one archive of pages, submitted together (step 7)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(ForeignKey("users.username"), nullable=False)
    # The caller's own label for the batch; its pages have none (they have source_name).
    external_id: Mapped[str | None] = mapped_column(default=None)
    domain: Mapped[str] = mapped_column(nullable=False)
    outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # open: pages still being read in; queued: all enqueued; finished: no page left to run
    status: Mapped[str] = mapped_column(nullable=False, default="open")
    total: Mapped[int] = mapped_column(nullable=False, default=0)
    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    archive_built_at: Mapped[datetime | None] = mapped_column(default=None)
    archive_size_bytes: Mapped[int | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint("status IN ('open', 'queued', 'finished')", name="ck_batches_status"),
        Index("ix_batches_username_submitted", "username", "submitted_at"),
        Index("ix_batches_username_external_id", "username", "external_id"),
    )
