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
    priority: Mapped[int] = mapped_column(default=0, nullable=False, server_default="0")
    device: Mapped[str] = mapped_column(default="cpu", nullable=False, server_default="cpu")
    # True for backends owned by the Helm register hook (declarative, upserted on every
    # deploy). The dashboard flags these so operators know manual edits are transient.
    managed: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
