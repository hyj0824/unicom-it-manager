from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    CallEvent,
    CallRecord,
    CallTask,
    CallbackPlan,
    Customer,
    Contact,
    Script,
    utcnow,
)
from .customers import default_contact as default_customer_contact


PHONE_RE = re.compile(r"^\+?[0-9]{5,20}$")
TRIGGER_TYPES = {"once", "cron"}


def validate_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone.strip()))


def get_zone(timezone_name: str) -> ZoneInfo:
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


def parse_datetime_local(value: str, timezone_name: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"执行时间格式不正确：「{value}」，应为 YYYY-MM-DDTHH:MM。"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_zone(timezone_name))
    return parsed.astimezone(timezone.utc)


def datetime_local_value(value: datetime | None, timezone_name: str) -> str:
    value = as_utc(value)
    if value is None:
        return ""
    return value.astimezone(get_zone(timezone_name)).strftime("%Y-%m-%dT%H:%M")


def validate_cron_expr(cron_expr: str, timezone_name: str) -> None:
    """校验 cron 表达式，非法时抛出带中文说明的 ValueError（表单直接展示）。"""

    cron_expr = cron_expr.strip()
    if not cron_expr:
        raise ValueError("周期计划必须填写 Cron 表达式，例如「0 9 * * *」（每天 09:00）。")
    zone = get_zone(timezone_name)
    try:
        CronTrigger.from_crontab(cron_expr, timezone=zone)
    except ValueError as exc:
        # APScheduler 的原始报错偏底层（如 "Unrecognized token"），
        # 包一层带原表达式的提示，便于用户在表单里定位问题。
        raise ValueError(f"Cron 表达式「{cron_expr}」无效：{exc}") from exc


def compute_next_run_at(
    trigger_type: str,
    run_at: datetime | None,
    cron_expr: str,
    timezone_name: str,
    from_time: datetime | None = None,
) -> datetime | None:
    if trigger_type not in TRIGGER_TYPES:
        raise ValueError("trigger_type must be once or cron")

    if trigger_type == "once":
        if run_at is None:
            raise ValueError("单次计划必须填写执行时间。")
        return as_utc(run_at)

    validate_cron_expr(cron_expr, timezone_name)

    zone = get_zone(timezone_name)
    base = as_utc(from_time or utcnow())
    assert base is not None
    base_local = base.astimezone(zone)
    trigger = CronTrigger.from_crontab(cron_expr, timezone=zone)
    next_fire = trigger.get_next_fire_time(None, base_local)
    if next_fire is None:
        return None
    return next_fire.astimezone(timezone.utc)


def create_plan(
    db: Session,
    customer: Customer,
    script: Script,
    trigger_type: str,
    run_at: datetime | None,
    cron_expr: str,
    timezone_name: str,
    enabled: bool,
    contact: Contact | None = None,
) -> CallbackPlan:
    next_run_at = compute_next_run_at(trigger_type, run_at, cron_expr, timezone_name)
    plan = CallbackPlan(
        customer=customer,
        script=script,
        contact=contact,
        trigger_type=trigger_type,
        run_at=as_utc(run_at),
        cron_expr=cron_expr.strip(),
        timezone=timezone_name,
        enabled=enabled,
        next_run_at=next_run_at,
    )
    db.add(plan)
    return plan


def update_plan(
    plan: CallbackPlan,
    customer: Customer,
    script: Script,
    trigger_type: str,
    run_at: datetime | None,
    cron_expr: str,
    timezone_name: str,
    enabled: bool,
    contact: Contact | None = None,
) -> None:
    plan.customer = customer
    plan.script = script
    plan.contact = contact
    plan.trigger_type = trigger_type
    plan.run_at = as_utc(run_at)
    plan.cron_expr = cron_expr.strip()
    plan.timezone = timezone_name
    plan.enabled = enabled
    plan.next_run_at = compute_next_run_at(trigger_type, run_at, cron_expr, timezone_name)


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

    不创建、不修改任何 CallbackPlan（plan_id 留空），因此不会影响原 cron
    计划；任务与通话记录共用一条关联，便于在通话详情追溯来源。
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


def create_call_task(
    db: Session,
    plan: CallbackPlan,
    due_at: datetime | None = None,
    status: str = "queued",
    message: str = "Call task queued.",
    source: str = "scheduled",
) -> CallTask:
    settings = get_settings()
    contact = plan.contact or default_customer_contact(db, plan.customer)
    dial_number = (contact.phone or "").strip() if contact else ""
    task = CallTask(
        plan=plan,
        customer=plan.customer,
        script=plan.script,
        contact=contact,
        dial_number=dial_number,
        due_at=as_utc(due_at) or utcnow(),
        status=status,
        source=source,
        max_attempts=settings.max_call_attempts,
    )
    record = CallRecord(
        task=task,
        plan=plan,
        customer=plan.customer,
        script=plan.script,
        contact=contact,
        dial_number=dial_number,
        status=status,
    )
    event = CallEvent(call_record=record, event_type=status, message=message)
    db.add(task)
    db.add(record)
    db.add(event)
    db.flush()
    return task


def mark_missed_once_plans(db: Session, now: datetime | None = None) -> int:
    now = as_utc(now) or utcnow()
    plans = db.scalars(
        select(CallbackPlan)
        .where(
            CallbackPlan.enabled.is_(True),
            CallbackPlan.trigger_type == "once",
            CallbackPlan.next_run_at.is_not(None),
            CallbackPlan.next_run_at < now,
        )
        .order_by(CallbackPlan.next_run_at.asc(), CallbackPlan.created_at.asc())
    ).all()

    for plan in plans:
        create_call_task(
            db,
            plan,
            due_at=plan.next_run_at,
            status="missed",
            message="One-time plan was missed during downtime and will not be replayed.",
        )
        plan.enabled = False
        plan.next_run_at = None
    return len(plans)


def advance_overdue_cron_plans(db: Session, now: datetime | None = None) -> int:
    """Move overdue cron plans to their next future fire time after a restart.

    The scheduler normally enqueues one due cron occurrence per tick.  On
    startup, however, ``next_run_at`` may point into a period when the process
    was down; advancing it first prevents replaying that historical occurrence.
    """

    now = as_utc(now) or utcnow()
    plans = db.scalars(
        select(CallbackPlan)
        .where(
            CallbackPlan.enabled.is_(True),
            CallbackPlan.trigger_type == "cron",
            CallbackPlan.next_run_at.is_not(None),
            CallbackPlan.next_run_at <= now,
        )
        .order_by(CallbackPlan.next_run_at.asc(), CallbackPlan.created_at.asc())
    ).all()

    for plan in plans:
        plan.next_run_at = compute_next_run_at(
            plan.trigger_type,
            plan.run_at,
            plan.cron_expr,
            plan.timezone,
            # CronTrigger includes an occurrence exactly at ``from_time``;
            # restart recovery must skip that historical boundary.
            from_time=now + timedelta(seconds=1),
        )
    return len(plans)


def enqueue_due_plans(db: Session, now: datetime | None = None) -> int:
    now = as_utc(now) or utcnow()
    plans = db.scalars(
        select(CallbackPlan)
        .where(
            CallbackPlan.enabled.is_(True),
            CallbackPlan.next_run_at.is_not(None),
            CallbackPlan.next_run_at <= now,
        )
        .order_by(CallbackPlan.next_run_at.asc(), CallbackPlan.created_at.asc())
    ).all()

    for plan in plans:
        create_call_task(
            db,
            plan,
            due_at=plan.next_run_at,
            status="queued",
            message="Plan reached its scheduled time and entered the call queue.",
        )
        if plan.trigger_type == "once":
            plan.enabled = False
            plan.next_run_at = None
        else:
            plan.next_run_at = compute_next_run_at(
                plan.trigger_type,
                plan.run_at,
                plan.cron_expr,
                plan.timezone,
                from_time=now + timedelta(seconds=1),
            )
    return len(plans)
