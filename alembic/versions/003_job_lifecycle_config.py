"""Seed job-lifecycle and presigned TTL config keys

Revision ID: 003
Revises: 002
Create Date: 2026-06-11 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEYS = (
    "jobs.queued_timeout_seconds",
    "jobs.running_timeout_seconds",
    "jobs.retention_days",
    "presigned.ttl_minutes",
)


def upgrade() -> None:
    op.execute(
        "INSERT INTO config (key, value) VALUES "
        "('jobs.queued_timeout_seconds', '900'), "
        "('jobs.running_timeout_seconds', '300'), "
        "('jobs.retention_days', '90'), "
        "('presigned.ttl_minutes', '60') "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    keys = ", ".join(f"'{k}'" for k in KEYS)
    op.execute(f"DELETE FROM config WHERE key IN ({keys})")
