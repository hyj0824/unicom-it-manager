"""update system notification scripts to list-style aggregated templates"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8f0a2b1c9d6e"
down_revision: Union[str, Sequence[str], None] = "8e1a2b3c4d5e"
branch_labels = None
depends_on = None

# 与 app/services/scans.py 的 DEFAULT_TEMPLATES 保持一致；系统话术以数据库
# 正文为准，代码常量仅作缺失时的回退。
_NEW_BODIES = {
    "notification_due_renewal": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务，您有以下待办：\n"
        "{{待办清单}}\n请{{负责人姓名}}尽快登录系统处理，感谢您的配合。"
    ),
    "notification_device_recycle": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务，您有以下待办：\n"
        "{{待办清单}}\n请{{负责人姓名}}尽快登录系统处理，感谢您的配合。"
    ),
    "notification_review_stuck": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务，您有以下待办：\n"
        "{{待办清单}}\n请{{负责人姓名}}尽快登录系统处理，感谢您的配合。"
    ),
}


def upgrade() -> None:
    scripts = sa.table(
        "scripts",
        sa.column("role", sa.String()),
        sa.column("body", sa.Text()),
        sa.column("title", sa.String()),
    )
    for role, body in _NEW_BODIES.items():
        title = {
            "notification_due_renewal": "到期维系通知",
            "notification_device_recycle": "设备回收通知",
            "notification_review_stuck": "审核卡单提醒通知",
        }[role]
        op.execute(
            scripts.update()
            .where(scripts.c.role == role)
            .values(body=body, title=title)
        )


def downgrade() -> None:
    # 旧正文不可恢复为单一值；反向迁移只重置为可读占位提示。
    scripts = sa.table(
        "scripts",
        sa.column("role", sa.String()),
        sa.column("body", sa.Text()),
    )
    op.execute(
        scripts.update()
        .where(scripts.c.role == "notification_due_renewal")
        .values(body="（旧正文已由聚合话术迁移覆盖）")
    )
    op.execute(
        scripts.update()
        .where(scripts.c.role == "notification_device_recycle")
        .values(body="（旧正文已由聚合话术迁移覆盖）")
    )
    op.execute(
        scripts.update()
        .where(scripts.c.role == "notification_review_stuck")
        .values(body="（旧正文已由聚合话术迁移覆盖）")
    )
