from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Backend(Base):
    __tablename__ = "backends"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(unique=True, nullable=False)
    label: Mapped[str | None] = mapped_column(default=None)
    api_key_enc: Mapped[str | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    max_inflight: Mapped[int] = mapped_column(default=4)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
