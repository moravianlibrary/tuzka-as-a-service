"""Initial schema

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String, unique=True, nullable=False),
        sa.Column("hashed_key", sa.String, nullable=False),
        sa.Column("active", sa.Boolean, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "backends",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("url", sa.String, unique=True, nullable=False),
        sa.Column("label", sa.String, nullable=True),
        sa.Column("api_key_enc", sa.String, nullable=True),
        sa.Column("enabled", sa.Boolean, server_default=sa.text("true")),
        sa.Column("max_inflight", sa.Integer, server_default=sa.text("4")),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "jobs",
        sa.Column(
            "id",
            sa.UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "username",
            sa.String,
            sa.ForeignKey("users.username"),
            nullable=False,
        ),
        sa.Column("external_id", sa.UUID, nullable=False),
        sa.Column("status", sa.String, server_default=sa.text("'queued'"), nullable=False),
        sa.Column("fmt", sa.String, server_default=sa.text("'multi'"), nullable=False),
        sa.Column("domain", sa.String, nullable=True),
        sa.Column("engine_job_id", sa.String, nullable=True),
        sa.Column(
            "backend_id",
            sa.Integer,
            sa.ForeignKey("backends.id"),
            nullable=True,
        ),
        sa.Column("error", sa.String, nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime,
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("finished_at", sa.DateTime, nullable=True),
    )

    op.create_index(
        "ix_jobs_username_submitted",
        "jobs",
        ["username", sa.text("submitted_at DESC")],
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_unique_constraint(
        "uq_jobs_username_external", "jobs", ["username", "external_id"]
    )

    op.create_table(
        "job_results",
        sa.Column(
            "job_id",
            sa.UUID,
            sa.ForeignKey("jobs.id"),
            primary_key=True,
        ),
        sa.Column("fmt", sa.String, primary_key=True),
        sa.Column("presigned_url", sa.String, nullable=True),
        sa.Column("presigned_until", sa.DateTime, nullable=True),
    )

    op.create_table(
        "storage_config",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("bucket", sa.String, unique=True, nullable=False),
        sa.Column("ttl_minutes", sa.Integer, server_default=sa.text("60")),
        sa.Column(
            "updated_at",
            sa.DateTime,
            server_default=sa.text("now()"),
        ),
    )

    op.execute(
        "INSERT INTO storage_config (bucket, ttl_minutes) "
        "VALUES ('incoming', 60), ('results', 60)"
    )


def downgrade() -> None:
    op.drop_table("job_results")
    op.drop_table("jobs")
    op.drop_table("backends")
    op.drop_table("storage_config")
    op.drop_table("users")
