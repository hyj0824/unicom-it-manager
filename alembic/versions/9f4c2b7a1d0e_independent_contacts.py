"""make contacts directory independent and bind plans to contacts

Revision ID: 9f4c2b7a1d0e
Revises: 7c1d8a4f2e6b
Create Date: 2026-08-17 21:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f4c2b7a1d0e"
down_revision: Union[str, Sequence[str], None] = "7c1d8a4f2e6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch_op:
        batch_op.add_column(sa.Column("duty", sa.String(length=64), nullable=False, server_default=""))

    with op.batch_alter_table("callback_plans") as batch_op:
        batch_op.add_column(sa.Column("contact_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_callback_plans_contact_id", ["contact_id"], unique=False)
        batch_op.create_foreign_key("fk_callback_plans_contact_id", "contacts", ["contact_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("callback_plans") as batch_op:
        batch_op.drop_constraint("fk_callback_plans_contact_id", type_="foreignkey")
        batch_op.drop_index("ix_callback_plans_contact_id")
        batch_op.drop_column("contact_id")
    with op.batch_alter_table("contacts") as batch_op:
        batch_op.drop_column("duty")
