from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    CallEvent,
    CallRecord,
    CallTask,
    Customer,
    Contact,
    Script,
    utcnow,
)
from .customers import default_contact as default_customer_contact


PHONE_RE = re.compile(r"^\+?[0-9]{5,20}$")


def validate_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone.strip()))


def get_zone(timezone_name: str) -> ZoneInfo:
    """读取时区；非法名称回退 UTC。

    仅用于展示/调度换算等容错场景；表单校验不允许静默回退，
    见 `validate_cron_expr`。
    """
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_cron_expr(cron_expr: str, timezone_name: str) -> None:
    """校验扫描配置的 cron 表达式与时区，非法时抛出带中文说明的 ValueError（表单直接展示）。"""

    cron_expr = cron_expr.strip()
    if not cron_expr:
        raise ValueError("必须填写 Cron 表达式，例如「0 9 * * *」（每天 09:00）。")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"时区「{timezone_name}」无效，请使用 IANA 时区名（例如 Asia/Shanghai）。")
    try:
        CronTrigger.from_crontab(cron_expr, timezone=zone)
    except ValueError as exc:
        # APScheduler 的原始报错偏底层（如 "Unrecognized token"），
        # 包一层带原表达式的提示，便于用户在表单里定位问题。
        raise ValueError(f"Cron 表达式「{cron_expr}」无效：{exc}") from exc


ACTIVE_TASK_STATUSES = {"queued", "dialing", "connected"}


def create_manual_call_task(
    db: Session,
    customer: Customer,
    script: Script,
    contact: Contact | None = None,
    due_at: datetime | None = None,
    status: str = "queued",
    message: str = "Manual call task queued.",
    source: str = "manual",
) -> CallTask:
    """从客户直接发起「立即拨打一次」：生成一条独立的一次性任务。

    不创建、不修改任何 CallbackPlan / ScanSchedule（两者关联均留空），
    任务与通话记录共用一条关联，便于在通话详情追溯来源。
    """

    contact = contact or default_customer_contact(db, customer)
    dial_number = (contact.phone or "").strip() if contact else ""
    if not dial_number:
        raise ValueError("该客户没有可拨打的电话：请先添加带有效联系电话的负责人。")
    settings = get_settings()
    task = CallTask(
        plan=None,
        customer=customer,
        script=script,
        contact=contact,
        dial_number=dial_number,
        due_at=as_utc(due_at) or utcnow(),
        status=status,
        source=source,
        max_attempts=settings.max_call_attempts,
    )
    record = CallRecord(
        task=task,
        plan=None,
        customer=customer,
        script=script,
        contact=contact,
        dial_number=dial_number,
        status=status,
    )
    db.add(task)
    db.add(record)
    db.add(CallEvent(call_record=record, event_type=status, message=message))
    db.flush()
    return task


def requeue_call_task(
    db: Session,
    task: CallTask,
    message: str = "Manual requeue from web UI.",
) -> CallTask:
    """人工重新入队：重置一条已结束的任务，而不是新建任务。

    选择「重置原任务」而不是「新建任务」的理由：
    - 通话详情依赖 CallRecord.task_id 唯一关联，重置能保留整条事件时间线，
      新建任务会把历史拆成两条记录；
    - 与 Worker 自动重试（`_schedule_retry`）使用同一套重置语义，队列里
      不会出现同一通电话的重复条目；
    - attempt 归 1，重新获得一轮完整的自动重试额度（上限来自配置）。
    """

    if task.status in ACTIVE_TASK_STATUSES:
        raise ValueError("任务正在排队或拨号中，不能重复入队。")
    now = utcnow()
    record = task.call_record
    if record is None:
        record = CallRecord(
            task=task,
            plan=task.plan,
            customer=task.customer,
            script=task.script,
            contact=task.contact,
            dial_number=task.dial_number,
            status="queued",
        )
        db.add(record)
    task.status = "queued"
    task.due_at = now
    task.attempt = 1
    task.started_at = None
    task.completed_at = None
    task.error_message = ""
    record.status = "queued"
    record.dialing_started_at = None
    record.connected_at = None
    record.ended_at = None
    record.duration_seconds = None
    record.error_message = ""
    db.add(CallEvent(call_record=record, event_type="manual_requeue", message=message))
    db.flush()
    return task
