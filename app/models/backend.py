"""ORM model for OCR backends: their URL, encrypted key, device, and dispatch limits."""

from datetime import datetime

from sqlalchemy import ColumnElement, String, cast, func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column

from app.vocabulary import AdapterId, Device

from .db import Base
from .types import StrEnumType


class Backend(Base):
    __tablename__ = "backends"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(unique=True, nullable=False)
    label: Mapped[str | None] = mapped_column(default=None)
    api_key_enc: Mapped[str | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    max_inflight: Mapped[int] = mapped_column(default=4)
    priority: Mapped[int] = mapped_column(default=0, nullable=False, server_default="0")
    device: Mapped[Device] = mapped_column(
        StrEnumType(Device), default=Device.CPU, nullable=False, server_default=Device.CPU
    )
    adapter: Mapped[AdapterId] = mapped_column(
        StrEnumType(AdapterId),
        default=AdapterId.TUZKAOCR,
        nullable=False,
        server_default=AdapterId.TUZKAOCR,
    )
    # True for backends owned by the Helm register hook (declarative, upserted on every
    # deploy). `taas-admin backend list` shows the flag, so operators know manual edits are transient.
    managed: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    @hybrid_property
    def display_label(self) -> str:
        """The label, or the id for a backend registered without one."""
        return self.label or str(self.id)

    @display_label.inplace.expression
    @classmethod
    def _display_label_expression(cls) -> ColumnElement[str]:
        return func.coalesce(cls.label, cast(cls.id, String))
