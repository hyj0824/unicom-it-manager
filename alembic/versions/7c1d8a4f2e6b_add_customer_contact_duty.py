"""add customer contact duty

Revision ID: 7c1d8a4f2e6b
Revises: b24a633aa108
Create Date: 2026-08-17 20:30:00.000000
"""

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7c1d8a4f2e6b"
down_revision: Union[str, Sequence[str], None] = "b24a633aa108"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    categories = sa.table(
        "dictionary_categories",
        sa.column("id", sa.Integer),
        sa.column("code", sa.String),
    )
    items = sa.table(
        "dictionary_items",
        sa.column("category_id", sa.Integer),
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("sort_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    category_id = connection.execute(
        sa.select(categories.c.id).where(categories.c.code == "contact_duty")
    ).scalar_one()
    exists = connection.execute(
        sa.select(items.c.code).where(
            items.c.category_id == category_id,
            items.c.code == "customer",
        )
    ).first()
    if exists is None:
        now = datetime.now(timezone.utc)
        connection.execute(
            items.insert().values(
                category_id=category_id,
                code="customer",
                label="客户",
                sort_order=4,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    connection = op.get_bind()
    categories = sa.table(
        "dictionary_categories",
        sa.column("id", sa.Integer),
        sa.column("code", sa.String),
    )
    items = sa.table(
        "dictionary_items",
        sa.column("category_id", sa.Integer),
        sa.column("code", sa.String),
    )
    category_id = connection.execute(
        sa.select(categories.c.id).where(categories.c.code == "contact_duty")
    ).scalar_one()
    connection.execute(
        items.delete().where(items.c.category_id == category_id, items.c.code == "customer")
    )
