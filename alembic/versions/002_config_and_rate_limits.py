"""Config KV table, rate limit defaults, per-user overrides

Revision ID: 002
Revises: 001
Create Date: 2026-06-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_LIMIT_COLUMNS = (
    "rate_submit_per_minute",
    "burst_submit",
    "rate_query_per_minute",
    "burst_query",
    "rate_ws_per_minute",
    "burst_ws",
)


def upgrade() -> None:
    op.create_table(
        "config",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("now()")),
    )

    # Seed rate-limit defaults (previous env-setting values)
    op.execute(
        "INSERT INTO config (key, value) VALUES "
        "('rate_limit.submit', '{\"per_minute\": 60, \"burst\": 10}'), "
        "('rate_limit.query', '{\"per_minute\": 120, \"burst\": 20}'), "
        "('rate_limit.ws_connect', '{\"per_minute\": 5, \"burst\": 2}')"
    )

    # Migrate storage TTLs: storage_config(bucket, ttl_minutes)
    # -> config key 'storage.{bucket}_ttl_minutes'
    op.execute(
        "INSERT INTO config (key, value) "
        "SELECT 'storage.' || bucket || '_ttl_minutes', to_json(ttl_minutes) "
        "FROM storage_config"
    )
    op.drop_table("storage_config")

    for col in USER_LIMIT_COLUMNS:
        op.add_column("users", sa.Column(col, sa.Integer, nullable=True))


def downgrade() -> None:
    for col in USER_LIMIT_COLUMNS:
        op.drop_column("users", col)

    op.create_table(
        "storage_config",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("bucket", sa.String, unique=True, nullable=False),
        sa.Column("ttl_minutes", sa.Integer, server_default=sa.text("60")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("now()")),
    )
    op.execute(
        "INSERT INTO storage_config (bucket, ttl_minutes) "
        "SELECT replace(replace(key, 'storage.', ''), '_ttl_minutes', ''), "
        "(value)::text::int "
        "FROM config WHERE key LIKE 'storage.%_ttl_minutes'"
    )
    op.drop_table("config")
