from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class StorageConfig(Base):
    __tablename__ = "storage_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bucket: Mapped[str] = mapped_column(unique=True, nullable=False)
    ttl_minutes: Mapped[int] = mapped_column(default=60)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
