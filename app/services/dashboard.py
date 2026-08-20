"""Queries for the personalised workbench and its global chart data.

The dashboard deliberately keeps user-specific joins here instead of in the
HTTP route.  That makes the phone/duty matching and the due-window semantics
testable without rendering a page.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session, joinedload

from .. import auth
from ..config import get_settings
from ..models import (
    BusinessService,
    CallRecord,
    CallTask,
    ChangeSet,
    NetworkDevice,
    ScanSchedule,
    User,
    utcnow,
)
from . import change_requests, ledger, scans
from .plans import get_zone


SOURCE_LABELS = {
    "manual": "人工通知",
    "due_renewal": "协议到期维系",
    "device_recycle": "退网设备回收",
    "review_stuck": "审核卡单提醒",
    "scheduled": "计划通知",
}

STATUS_LABELS = {
    "queued": "待拨打",
    "dialing": "拨号中",
    "connected": "已接通",
    "no_answer": "无人接听",
    "rejected": "疑似拒接",
    "cancelled_or_failed": "已取消或失败",
    "busy": "忙线",
    "short_call": "短通话",
    "failed": "失败",
    "completed": "已完成",
    "missed": "已错过",
}

CALL_RESULT_ORDER = (
    "completed",
    "failed",
    "no_answer",
    "short_call",
    "busy",
    "rejected",
    "cancelled_or_failed",
    "missed",
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _principal_scope(db: Session, request) -> tuple[bool, str]:
    """Return (global_scope, phone); only the built-in admin has global scope."""

    principal = auth.current_user(request)
    if not principal:
        return False, ""
    if principal.get("type") == "admin":
        return True, ""
    user_id = principal.get("id")
    user = db.get(User, user_id) if isinstance(user_id, int) else None
    if user is None:
        return False, ""
    return False, (user.phone or "").strip()


def _review_count(db: Session, request, global_scope: bool) -> tuple[int, list[str]]:
    domains = ("business", "network", "callback", "template", "system")
    if global_scope:
        return (
            int(
                db.scalar(
                    select(func.count(ChangeSet.id)).where(ChangeSet.status == "submitted")
                )
                or 0
            ),
            list(domains),
        )
    allowed = [
        domain for domain in domains if auth.has_permission(db, request, "review", domain)
    ]
    if not allowed:
        return 0, []
    return (
        int(
            db.scalar(
                select(func.count(ChangeSet.id)).where(
                    ChangeSet.status == "submitted", ChangeSet.domain.in_(allowed)
                )
            )
            or 0
        ),
        allowed,
    )


def _due_service_count(db: Session, phone: str, global_scope: bool, now: datetime) -> int:
    settings = get_settings()
    lead_days = change_requests.due_renewal_lead_days(db)
    start, end = change_requests.due_window_bounds(now, settings.default_timezone, lead_days)
    query = select(func.count(BusinessService.id)).where(
        BusinessService.is_active.is_(True),
        BusinessService.agreement_expires_at.is_not(None),
        BusinessService.agreement_expires_at >= start,
        BusinessService.agreement_expires_at < end,
    )
    if not global_scope:
        query = query.where(BusinessService.account_manager_phone == phone)
        return int(db.scalar(query) or 0)
    return int(db.scalar(query) or 0)


def _device_count(db: Session, phone: str, global_scope: bool, now: datetime) -> int:
    settings = get_settings()
    zone = get_zone(settings.default_timezone)
    local_now = (_as_utc(now) or utcnow()).astimezone(zone)
    day_start = datetime.combine(local_now.date(), time.min, tzinfo=zone).astimezone(timezone.utc)
    devices = db.scalars(
        select(NetworkDevice)
        .options(
            joinedload(NetworkDevice.business_service).joinedload(BusinessService.service_status_item),
            joinedload(NetworkDevice.recovery_status_item),
        )
        .where(NetworkDevice.is_active.is_(True))
    ).all()
    count = 0
    for device in devices:
        service = device.business_service
        if not service or not scans._is_retired_service(service, day_start) or scans._device_recovered(device):
            continue
        if global_scope:
            count += 1
            continue
        if (device.maintenance_phone or "").strip() == phone:
            count += 1
    return count


def _recent_notifications(db: Session, phone: str, global_scope: bool) -> list[dict]:
    if not global_scope and not phone:
        return []
    query = (
        select(CallTask)
        .options(joinedload(CallTask.call_record))
        .order_by(CallTask.created_at.desc(), CallTask.id.desc())
        .limit(5)
    )
    if not global_scope:
        query = query.where(CallTask.dial_number == phone)
    tasks = db.scalars(query).unique().all()
    return [
        {
            "created_at": task.created_at,
            "type": SOURCE_LABELS.get(task.source, task.source or "通知"),
            "result": STATUS_LABELS.get(
                task.call_record.status if task.call_record else task.status,
                task.call_record.status if task.call_record else task.status,
            ),
        }
        for task in tasks
    ]


def _business_status_distribution(db: Session) -> list[dict]:
    services = db.scalars(
        select(BusinessService)
        .options(joinedload(BusinessService.service_status_item))
        .where(BusinessService.is_active.is_(True))
    ).all()
    counter = Counter(
        (service.service_status_item.label if service.service_status_item else "未设置")
        for service in services
    )
    total = sum(counter.values()) or 1
    return [
        {"label": label, "count": count, "percent": round(count * 100 / total, 1)}
        for label, count in counter.most_common()
    ]


def _call_result_distribution(db: Session) -> list[dict]:
    rows = db.execute(
        select(CallRecord.status, func.count(CallRecord.id)).group_by(CallRecord.status)
    ).all()
    counts = {status: int(count) for status, count in rows}
    statuses = [status for status in CALL_RESULT_ORDER if counts.get(status, 0)]
    statuses.extend(
        sorted(status for status, count in counts.items() if count and status not in CALL_RESULT_ORDER)
    )
    if not statuses:
        statuses = list(CALL_RESULT_ORDER)
    total = sum(counts.get(status, 0) for status in statuses) or 1
    return [
        {
            "status": status,
            "label": STATUS_LABELS.get(status, status),
            "count": counts.get(status, 0),
            "percent": round(counts.get(status, 0) * 100 / total, 1),
        }
        for status in statuses
    ]


def _notification_trend(db: Session, now: datetime) -> list[dict]:
    zone = get_zone(get_settings().default_timezone)
    local_today = (_as_utc(now) or utcnow()).astimezone(zone).date()
    first_day = local_today - timedelta(days=6)
    start = datetime.combine(first_day, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(local_today + timedelta(days=1), time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    tasks = db.scalars(
        select(CallTask).where(CallTask.created_at >= start, CallTask.created_at < end)
    ).all()
    counts = Counter((_as_utc(task.created_at) or utcnow()).astimezone(zone).date() for task in tasks)
    return [
        {
            "date": (first_day + timedelta(days=offset)).strftime("%m-%d"),
            "count": counts.get(first_day + timedelta(days=offset), 0),
        }
        for offset in range(7)
    ]


def dashboard_data(db: Session, request, now: datetime | None = None) -> dict:
    """Build all values consumed by ``dashboard.html``."""

    now = now or utcnow()
    global_scope, phone = _principal_scope(db, request)
    pending_reviews, review_domains = _review_count(db, request, global_scope)
    global_pending_reviews = int(
        db.scalar(select(func.count(ChangeSet.id)).where(ChangeSet.status == "submitted")) or 0
    )
    has_phone_scope = global_scope or bool(phone)
    due_services = _due_service_count(db, phone, global_scope, now) if has_phone_scope else 0
    due_devices = _device_count(db, phone, global_scope, now) if has_phone_scope else 0
    missing_total = sum(row["count"] for row in ledger.business_missing_fields(db))
    missing_total += sum(row["count"] for row in ledger.device_missing_fields(db))
    counts = {
        "services": int(
            db.scalar(select(func.count(BusinessService.id)).where(BusinessService.is_active.is_(True)))
            or 0
        ),
        "devices": int(
            db.scalar(select(func.count(NetworkDevice.id)).where(NetworkDevice.is_active.is_(True)))
            or 0
        ),
        "customers": int(db.scalar(select(func.count(BusinessService.customer_name.distinct()))) or 0),
        "scan_schedules": int(db.scalar(select(func.count(ScanSchedule.id))) or 0),
        "records": int(db.scalar(select(func.count(CallRecord.id))) or 0),
    }
    admin_metrics = None
    if global_scope:
        settings = get_settings()
        zone = get_zone(settings.default_timezone)
        local_now = (_as_utc(now) or utcnow()).astimezone(zone)
        today_start = datetime.combine(local_now.date(), time.min, tzinfo=zone).astimezone(timezone.utc)
        tomorrow = today_start + timedelta(days=1)
        admin_metrics = {
            "queued_tasks": int(
                db.scalar(select(func.count(CallTask.id)).where(CallTask.status == "queued")) or 0
            ),
            "today_calls": int(
                db.scalar(
                    select(func.count(CallRecord.id)).where(
                        CallRecord.created_at >= today_start, CallRecord.created_at < tomorrow
                    )
                )
                or 0
            ),
        }
    return {
        "pending_reviews": pending_reviews,
        "global_pending_reviews": global_pending_reviews,
        "review_domains": review_domains,
        "my_due_services": due_services,
        "my_due_devices": due_devices,
        "show_my_due_cards": has_phone_scope,
        "recent_notifications": _recent_notifications(db, phone, global_scope),
        "business_statuses": _business_status_distribution(db),
        "call_results": _call_result_distribution(db),
        "notification_trend": _notification_trend(db, now),
        "missing_total": missing_total,
        "counts": counts,
        "admin_metrics": admin_metrics,
        "global_scope": global_scope,
    }
