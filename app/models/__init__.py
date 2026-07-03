"""ORM models package.

Re-exports every mapped class so that importing ``app.models`` (or any submodule,
which triggers this package init) registers all tables on ``Base.metadata``. Alembic's
autogenerate and ``Base.metadata.create_all`` depend on this being complete — a model
missing here is a table autogenerate would propose to DROP.
"""

from app.models.backend import Backend
from app.models.backend_domain import BackendDomain
from app.models.config import ConfigEntry
from app.models.db import Base
from app.models.domain import Domain
from app.models.job import Job, JobResult
from app.models.user import User

__all__ = [
    "Backend",
    "BackendDomain",
    "Base",
    "ConfigEntry",
    "Domain",
    "Job",
    "JobResult",
    "User",
]
