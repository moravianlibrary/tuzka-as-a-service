"""Domain capabilities, domain metadata, job outputs/options

Revision ID: 014
Revises: 013
Create Date: 2026-10-19 00:00:00.000000

backend_domains gains the outputs a backend produces for a domain. domains gains
optional display metadata, the compat shim's legacy engine id, and the per-job
options JSON Schema derived from the engine's OpenAPI. jobs records the requested
outputs and options (nullable: rows from before this revision have neither).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No server default: domain sync always writes outputs explicitly. Existing rows get
    # what every backend served so far; the next healthcheck rewrites them anyway.
    op.add_column("backend_domains", sa.Column("outputs", ARRAY(sa.Text), nullable=True))
    op.execute("UPDATE backend_domains SET outputs = '{alto,txt}'")
    op.alter_column("backend_domains", "outputs", nullable=False)

    op.add_column("domains", sa.Column("label", sa.Text, nullable=True))
    op.add_column("domains", sa.Column("description", sa.Text, nullable=True))
    op.add_column(
        "domains", sa.Column("legacy_engine_id", sa.Integer, nullable=True, unique=True)
    )
    op.add_column("domains", sa.Column("options_schema", JSONB, nullable=True))

    # The compat shim's engine ids (compat/app/config.py), so get_engines stays identical.
    op.execute(
        """
        INSERT INTO domains (name, legacy_engine_id, label, description) VALUES
            ('default', 1, 'Default', 'Default OCR engine'),
            ('kramarky', 2, 'Kramarky', 'Engine for kramarky domain')
        ON CONFLICT (name) DO UPDATE SET
            legacy_engine_id = EXCLUDED.legacy_engine_id,
            label = EXCLUDED.label,
            description = EXCLUDED.description
        """
    )

    op.add_column("jobs", sa.Column("outputs", ARRAY(sa.Text), nullable=True))
    op.add_column("jobs", sa.Column("options", JSONB, nullable=True))


def downgrade() -> None:
    # The seeded domain rows stay: domains rows are kept forever for analytics anyway.
    op.drop_column("jobs", "options")
    op.drop_column("jobs", "outputs")
    op.drop_column("domains", "options_schema")
    op.drop_column("domains", "legacy_engine_id")
    op.drop_column("domains", "description")
    op.drop_column("domains", "label")
    op.drop_column("backend_domains", "outputs")
