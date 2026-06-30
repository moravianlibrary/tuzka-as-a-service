# Many-to-many mapping of which backends serve which domains. Rebuilt for a backend
# on each healthcheck from the engine's GET /api/v1/models; the submit worker uses it
# to route a domain-tagged job only to a backend that serves that domain.
from sqlalchemy import ForeignKey, PrimaryKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class BackendDomain(Base):
    __tablename__ = "backend_domains"

    backend_id: Mapped[int] = mapped_column(ForeignKey("backends.id", ondelete="CASCADE"), nullable=False)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (PrimaryKeyConstraint("backend_id", "domain_id"),)
