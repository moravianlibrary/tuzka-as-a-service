"""external_id becomes an optional free string; drop users.external_url_template

Revision ID: 020
Revises: 019
Create Date: 2026-10-21 00:00:00.000000

external_id is the caller's own label (a PID like "uuid:…", a signature, a file name),
not necessarily a UUID, and optional. Changing the column type rewrites the table, so
this runs after step 6's TTLs have cut jobs down to about a day's rows.

The downgrade keeps values that are UUIDs and gives every other row a random one.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID_RE = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def upgrade() -> None:
    op.alter_column(
        "jobs",
        "external_id",
        type_=sa.Text,
        nullable=True,
        postgresql_using="external_id::text",
    )
    op.create_index("ix_jobs_username_external_id", "jobs", ["username", "external_id"])
    op.drop_column("users", "external_url_template")


def downgrade() -> None:
    op.add_column("users", sa.Column("external_url_template", sa.Text, nullable=True))
    op.drop_index("ix_jobs_username_external_id", "jobs")
    op.alter_column(
        "jobs",
        "external_id",
        type_=sa.UUID,
        nullable=False,
        postgresql_using=(
            f"CASE WHEN external_id ~ '{_UUID_RE}' THEN external_id::uuid "
            "ELSE gen_random_uuid() END"
        ),
    )
