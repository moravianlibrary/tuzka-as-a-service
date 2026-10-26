"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
revision: str = ${repr(up_revision).replace("'", '"')}
down_revision: str | None = ${repr(down_revision).replace("'", '"')}
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
