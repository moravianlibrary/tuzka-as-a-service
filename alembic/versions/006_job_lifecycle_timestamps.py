"""Add dispatched_at, engine_received_at and stored_at to jobs

Revision ID: 006
Revises: 005
Create Date: 2026-06-15 00:00:00.000000

Three lifecycle stamps with distinct clock owners:
- ``dispatched_at``     — taas clock: the submit worker POSTs the job to the engine.
- ``engine_received_at`` — engine clock: the engine created/queued the job.
- ``stored_at``         — taas clock: the result was persisted.

Keeping the taas dispatch and the engine receipt as separate columns lets every
derived span stay on a single clock (taas queue = dispatched-submitted, engine
queue = started-engine_received), so cross-node skew never bleeds into a metric
or flips a sub-second span negative.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dispatched_at", sa.DateTime(), nullable=True))
    op.add_column("jobs", sa.Column("engine_received_at", sa.DateTime(), nullable=True))
    op.add_column("jobs", sa.Column("stored_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "stored_at")
    op.drop_column("jobs", "engine_received_at")
    op.drop_column("jobs", "dispatched_at")
