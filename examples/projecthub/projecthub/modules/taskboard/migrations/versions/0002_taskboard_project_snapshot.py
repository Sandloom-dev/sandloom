"""taskboard project snapshot (serialises project/task projection writers)

Revision ID: 0002_taskboard
Revises: 0001_taskboard
Create Date: 2026-10-08 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002_taskboard'
down_revision: Union[str, Sequence[str], None] = '0001_taskboard'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('taskboard_project_snapshot',
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('version', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('project_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('taskboard_project_snapshot')
