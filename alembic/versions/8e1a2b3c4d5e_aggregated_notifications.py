"""aggregate scan notifications and fix system scripts/configuration"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "8e1a2b3c4d5e"
down_revision: Union[str, Sequence[str], None] = "c4f2a1d9e7b0"
branch_labels = None
depends_on = None


_scripts = sa.table(
    "scripts",
    sa.column("role", sa.String(64)),
    sa.column("title", sa.String(160)),
    sa.column("body", sa.Text()),
    sa.column("tts_status", sa.String(32)),
    sa.column("tts_error", sa.String(500)),
    sa.column("wav_path", sa.String(500)),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

_schedules = sa.table(
    "scan_schedules",
    sa.column("name", sa.String(120)),
    sa.column("scan_type", sa.String(32)),
    sa.column("cron_expr", sa.String(120)),
    sa.column("timezone", sa.String(80)),
    sa.column("lead_days", sa.Integer),
    sa.column("enabled", sa.Boolean),
    sa.column("sms_enabled", sa.Boolean),
    sa.column("last_error", sa.Text),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upgrade() -> None:
    # The old records describe a per-business notification model.  They are
    # intentionally discarded before changing the script/config foreign keys.
    for table in ("call_events", "sms_notifications", "call_records", "call_tasks", "scan_schedules", "scripts"):
        op.execute(sa.text(f"DELETE FROM {table}"))

    with op.batch_alter_table("scripts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint("uq_scripts_role", ["role"])

    with op.batch_alter_table("scan_schedules", schema=None) as batch_op:
        batch_op.drop_constraint("fk_scan_schedules_script_id_scripts", type_="foreignkey")
        batch_op.drop_column("script_id")
        batch_op.drop_index("ix_scan_schedules_scan_type")
        batch_op.create_unique_constraint("uq_scan_schedules_scan_type", ["scan_type"])

    now = _now()
    templates = [
        (
            "notification_due_renewal",
            "到期维系通知",
            "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务：客户{{客户名称}}的{{业务号码}}业务协议将于{{协议到期日}}到期，请{{负责人姓名}}提前联系客户办理续签维系。待办清单：{{待办清单}}，感谢您的配合。",
        ),
        (
            "notification_device_recycle",
            "设备回收通知",
            "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务：客户{{客户名称}}的{{业务号码}}业务已退网，其中设备{{设备编码}}尚未回收，请{{负责人姓名}}尽快安排回收。待办清单：{{待办清单}}，感谢您的配合。",
        ),
        (
            "notification_review_stuck",
            "审核卡单提醒通知",
            "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务：客户{{客户名称}}的{{业务号码}}业务提交的审核单「{{审核单标题}}」已长时间未审核，请{{负责人姓名}}尽快登录系统审核处理。待办清单：{{待办清单}}，感谢您的配合。",
        ),
    ]
    op.bulk_insert(
        _scripts,
        [
            {
                "role": role,
                "title": title,
                "body": body,
                "tts_status": "not_generated",
                "tts_error": "",
                "wav_path": "",
                "created_at": now,
                "updated_at": now,
            }
            for role, title, body in templates
        ],
    )
    op.bulk_insert(
        _schedules,
        [
            {
                "name": name,
                "scan_type": scan_type,
                "cron_expr": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "lead_days": 14,
                "enabled": True,
                "sms_enabled": False,
                "last_error": "",
                "created_at": now,
                "updated_at": now,
            }
            for name, scan_type in (
                ("到期维系", "due_renewal"),
                ("设备回收", "device_recycle"),
                ("审核卡单", "review_stuck"),
            )
        ],
    )


def downgrade() -> None:
    # Historical data is intentionally not restored; rebuild the old shape as
    # empty tables/columns so Alembic can still walk back to the previous head.
    for table in ("call_events", "sms_notifications", "call_records", "call_tasks", "scan_schedules", "scripts"):
        op.execute(sa.text(f"DELETE FROM {table}"))

    with op.batch_alter_table("scan_schedules", schema=None) as batch_op:
        batch_op.drop_constraint("uq_scan_schedules_scan_type", type_="unique")
        batch_op.add_column(sa.Column("script_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_scan_schedules_script_id_scripts", "scripts", ["script_id"], ["id"]
        )
        batch_op.create_index("ix_scan_schedules_scan_type", ["scan_type"], unique=False)

    with op.batch_alter_table("scripts", schema=None) as batch_op:
        batch_op.drop_constraint("uq_scripts_role", type_="unique")
        batch_op.drop_column("role")
