# Domain lookup table: one row per OCR domain (e.g. "default", "kramarky") the
# engines advertise via GET /api/v1/models. Referenced by job_analytics.domain_id
# and backend_domains; rows are kept forever so historical analytics resolve names.
from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    label: Mapped[str | None] = mapped_column(default=None)
    description: Mapped[str | None] = mapped_column(default=None)
    # Engine id the legacy compat shim exposes for this domain (get_engines).
    legacy_engine_id: Mapped[int | None] = mapped_column(unique=True, default=None)
    # Per-job options JSON Schema, derived from the engine's OpenAPI; newest wins.
    options_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # Per-domain limits; None = the global one (Settings.max_upload_bytes,
    # jobs.running_timeout_seconds).
    max_upload_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    running_timeout_seconds: Mapped[int | None] = mapped_column(default=None)
