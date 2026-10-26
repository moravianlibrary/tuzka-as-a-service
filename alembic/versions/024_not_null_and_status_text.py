"""Enforce the NOT NULLs the models declare, and store jobs.status as text.

These columns have always had a server default and the models have always declared
them NOT NULL, but the tables still allowed NULL; jobs.status was the one enum column
left VARCHAR (the others are TEXT). With the schema matching the models, autogenerate
(`make db-migrate`) diffs only what a change really needs.

Revision ID: 024
Revises: 023
Create Date: 2026-10-26 14:24:50.830458
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "024"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table, column, the value a NULL gets (the column's server default)
NOT_NULL = [
    ("backends", "enabled", "true"),
    ("backends", "max_inflight", "4"),
    ("backends", "created_at", "now()"),
    ("config", "updated_at", "now()"),
    ("users", "active", "true"),
    ("users", "created_at", "now()"),
]


def upgrade() -> None:
    for table, column, default in NOT_NULL:
        # A NULL left from before would fail the constraint: give it the default.
        op.execute(f"UPDATE {table} SET {column} = {default} WHERE {column} IS NULL")
        op.alter_column(table, column, nullable=False)
    # VARCHAR to TEXT is binary-compatible in Postgres: no table rewrite.
    op.alter_column("jobs", "status", type_=sa.Text(), existing_type=sa.VARCHAR())


def downgrade() -> None:
    op.alter_column("jobs", "status", type_=sa.VARCHAR(), existing_type=sa.Text())
    for table, column, _ in NOT_NULL:
        op.alter_column(table, column, nullable=True)
