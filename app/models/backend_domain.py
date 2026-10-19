# Many-to-many mapping of which backends serve which domains. Rebuilt for a backend
# on each healthcheck from the engine's GET /api/v1/models; the submit worker uses it
# to route a domain-tagged job only to a backend that serves that domain.
from sqlalchemy import ForeignKey, PrimaryKeyConstraint, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class BackendDomain(Base):
    __tablename__ = "backend_domains"

    backend_id: Mapped[int] = mapped_column(
        ForeignKey("backends.id", ondelete="CASCADE"), nullable=False
    )
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    # Outputs this backend produces for this domain (e.g. {alto, txt}).
    outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)

    __table_args__ = (PrimaryKeyConstraint("backend_id", "domain_id"),)
