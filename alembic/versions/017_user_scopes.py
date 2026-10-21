"""Add users.allowed_domains / users.allowed_outputs (scoped API keys)

Revision ID: 017
Revises: 016
Create Date: 2026-10-21 00:00:00.000000

NULL means every domain / output, so existing keys keep full access.

Merge note: written on a branch whose head is 014. Plans 04 (015) and 09 (016) land
first, so set down_revision to "016" when merging.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("allowed_domains", ARRAY(sa.Text), nullable=True))
    op.add_column("users", sa.Column("allowed_outputs", ARRAY(sa.Text), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "allowed_outputs")
    op.drop_column("users", "allowed_domains")
