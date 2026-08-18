"""scripts tts_error column

Revision ID: 627e7884c03e
Revises: 9f4c2b7a1d0e
Create Date: 2026-08-18 10:31:48.796148

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '627e7884c03e'
down_revision: Union[str, Sequence[str], None] = '9f4c2b7a1d0e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 只新增 tts_error 列；server_default 用于已有话术行的 NOT NULL 回填。
    with op.batch_alter_table('scripts', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'tts_error',
                sa.String(length=500),
                nullable=False,
                server_default='',
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('scripts', schema=None) as batch_op:
        batch_op.drop_column('tts_error')
