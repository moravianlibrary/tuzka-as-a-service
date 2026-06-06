from datetime import datetime
from typing import Any

from sqlalchemy import JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class ConfigEntry(Base):
    __tablename__ = "config"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
