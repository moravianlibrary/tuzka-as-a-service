"""ORM model for API users: their hashed key, priority, and per-user rate-limit overrides."""

from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(unique=True, nullable=False)
    hashed_key: Mapped[str] = mapped_column(nullable=False)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    priority: Mapped[int] = mapped_column(default=0, nullable=False, server_default="0")
    # Scope of the user's key; NULL = every domain / every output.
    allowed_domains: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)
    allowed_outputs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)

    # Rate limit overrides; NULL = inherit the default from the config table
    rate_submit_per_minute: Mapped[int | None] = mapped_column(nullable=True)
    burst_submit: Mapped[int | None] = mapped_column(nullable=True)
    rate_query_per_minute: Mapped[int | None] = mapped_column(nullable=True)
    burst_query: Mapped[int | None] = mapped_column(nullable=True)
    rate_ws_per_minute: Mapped[int | None] = mapped_column(nullable=True)
    burst_ws: Mapped[int | None] = mapped_column(nullable=True)
