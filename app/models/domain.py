# Domain lookup table: one row per OCR domain (e.g. "default", "kramarky") the
# engines advertise via GET /api/v1/models. Referenced by job_analytics.domain_id
# and backend_domains; rows are kept forever so historical analytics resolve names.
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
