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
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_zone(timezone_name))
    return parsed.astimezone(timezone.utc)


def datetime_local_value(value: datetime | None, timezone_name: str) -> str:
    value = as_utc(value)
    if value is None:
        return ""
    return value.astimezone(get_zone(timezone_name)).strftime("%Y-%m-%dT%H:%M")


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
            raise ValueError("run_at is required for once plans")
        return as_utc(run_at)

    cron_expr = cron_expr.strip()
    if not cron_expr:
        raise ValueError("cron_expr is required for cron plans")

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
