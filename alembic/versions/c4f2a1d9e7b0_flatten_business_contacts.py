"""flatten customer and contact data into business/device snapshots"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4f2a1d9e7b0"
down_revision: Union[str, Sequence[str], None] = "37b8da0080f6"
branch_labels = None
depends_on = None


def _add(batch, name, type_, default=""):
    batch.add_column(sa.Column(name, type_, nullable=False, server_default=sa.text("''")))


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_business_services_customer_id")
    with op.batch_alter_table("business_services", schema=None) as b:
        b.drop_column("customer_id")
        _add(b, "customer_name", sa.String(160))
        _add(b, "developer_name", sa.String(120))
        _add(b, "developer_phone", sa.String(32))
        _add(b, "account_manager_name", sa.String(120))
        _add(b, "account_manager_phone", sa.String(32))

    with op.batch_alter_table("network_devices", schema=None) as b:
        b.drop_column("maintenance_contact_id")
        _add(b, "maintenance_name", sa.String(120))
        _add(b, "maintenance_phone", sa.String(32))

    with op.batch_alter_table("call_tasks", schema=None) as b:
        b.drop_column("plan_id")
        b.drop_column("customer_id")
        b.drop_column("contact_id")
        _add(b, "customer_name", sa.String(160))

    with op.batch_alter_table("call_records", schema=None) as b:
        b.drop_column("plan_id")
        b.drop_column("customer_id")
        b.drop_column("contact_id")
        _add(b, "customer_name", sa.String(160))

    # These entities are intentionally removed; historical data is out of scope.
    op.drop_table("callback_plans")
    op.drop_table("customer_contacts")
    op.drop_table("contacts")
    op.drop_table("customers")


def downgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(120)), sa.Column("phone", sa.String(32)),
        sa.Column("duty", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "customer_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("duty", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name="fk_customer_contacts_customer_id"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], name="fk_customer_contacts_contact_id"),
        sa.UniqueConstraint("customer_id", "contact_id", "duty", name="uq_customer_contacts_link"),
    )
    op.create_table(
        "callback_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("script_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer()),
        sa.Column("trigger_type", sa.String(16), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True)),
        sa.Column("cron_expr", sa.String(120), nullable=False, server_default=sa.text("''")),
        sa.Column("timezone", sa.String(80), nullable=False, server_default=sa.text("'Asia/Shanghai'")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name="fk_callback_plans_customer_id"),
        sa.ForeignKeyConstraint(["script_id"], ["scripts.id"], name="fk_callback_plans_script_id"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], name="fk_callback_plans_contact_id"),
    )
    op.create_index("ix_callback_plans_contact_id", "callback_plans", ["contact_id"])
    op.create_index("ix_callback_plans_next_run_at", "callback_plans", ["next_run_at"])
    op.create_index("ix_customer_contacts_customer_id", "customer_contacts", ["customer_id"])
    op.create_index("ix_customer_contacts_contact_id", "customer_contacts", ["contact_id"])
    op.create_index("ix_contacts_phone", "contacts", ["phone"])

    with op.batch_alter_table("business_services", schema=None) as b:
        b.add_column(sa.Column("customer_id", sa.Integer(), nullable=True))
        b.drop_column("account_manager_phone"); b.drop_column("account_manager_name")
        b.drop_column("developer_phone"); b.drop_column("developer_name"); b.drop_column("customer_name")
    op.create_index("ix_business_services_customer_id", "business_services", ["customer_id"])
    with op.batch_alter_table("network_devices", schema=None) as b:
        b.add_column(sa.Column("maintenance_contact_id", sa.Integer(), nullable=True))
        b.drop_column("maintenance_phone"); b.drop_column("maintenance_name")
    with op.batch_alter_table("call_tasks", schema=None) as b:
        b.add_column(sa.Column("plan_id", sa.Integer(), nullable=True)); b.add_column(sa.Column("customer_id", sa.Integer(), nullable=True)); b.add_column(sa.Column("contact_id", sa.Integer(), nullable=True)); b.drop_column("customer_name")
    with op.batch_alter_table("call_records", schema=None) as b:
        b.add_column(sa.Column("plan_id", sa.Integer(), nullable=True)); b.add_column(sa.Column("customer_id", sa.Integer(), nullable=True)); b.add_column(sa.Column("contact_id", sa.Integer(), nullable=True)); b.drop_column("customer_name")
