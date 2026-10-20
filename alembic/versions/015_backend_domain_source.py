"""Add backend_domains.source (engine | declared)

Revision ID: 015
Revises: 014
Create Date: 2026-10-20 00:00:00.000000

A backend's capabilities either come from the engine itself (domain sync) or are
declared at registration, so a workload that has never run yet (scaled to zero) is
already known to serve its domains. Domain sync replaces declared rows with engine
ones on its first successful describe().
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backend_domains",
        sa.Column("source", sa.Text, nullable=False, server_default="engine"),
    )
    op.create_check_constraint(
        "ck_backend_domains_source", "backend_domains", "source IN ('engine', 'declared')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_backend_domains_source", "backend_domains", type_="check")
    op.drop_column("backend_domains", "source")
