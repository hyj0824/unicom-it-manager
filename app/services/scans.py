from __future__ import annotations

"""运营扫描与聚合通知任务。

每类扫描按通知对象（负责人手机号）聚合成一条 CallTask。任务的
``meta_json.targets`` 保存本次通知覆盖的业务、设备或变更申请，供去重、
工作台统计和通话详情回放使用。扫描本身不访问真实硬件。
"""

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    BusinessService,
    CallTask,
    ChangeSet,
    NetworkDevice,
    Permission,
    RolePermission,
    ScanSchedule,
    Script,
    SmsNotification,
    User,
    UserRole,
    utcnow,
)
from . import scripts as script_service

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

# 固定系统话术。聚合通知只提供三个占位符：负责人姓名（组内唯一）、
# 待办清单（全部事项）、扫描类型。单值占位符不再支持。
DEFAULT_TEMPLATES: dict[str, str] = {
    "due_renewal": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务，您有以下待办：\n"
        "{{待办清单}}\n请{{负责人姓名}}尽快登录系统处理，感谢您的配合。"
    ),
    "device_recycle": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务，您有以下待办：\n"
        "{{待办清单}}\n请{{负责人姓名}}尽快登录系统处理，感谢您的配合。"
    ),
    "review_stuck": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务，您有以下待办：\n"
        "{{待办清单}}\n请{{负责人姓名}}尽快登录系统处理，感谢您的配合。"
    ),
}

SCAN_TYPE_LABELS: dict[str, str] = {
    "due_renewal": "协议到期维系",
    "device_recycle": "退网设备回收",
    "review_stuck": "审核卡单提醒",
}

PLACEHOLDER_SPECS: dict[str, list[dict[str, str]]] = {
    "due_renewal": [
        {"token": "负责人姓名", "example": "张三"},
        {"token": "待办清单", "example": "1. 某某有限公司（848DIA11742988）协议2026-12-31到期\n2. 某某网络（848DIA11742999）协议2027-01-15到期"},
        {"token": "扫描类型", "example": "协议到期维系"},
    ],
    "device_recycle": [
        {"token": "负责人姓名", "example": "李四"},
        {"token": "待办清单", "example": "1. 某某有限公司（848DIA11742988）设备21000001未回收\n2. 某某有限公司（848DIA11742988）设备21000002未回收"},
        {"token": "扫描类型", "example": "退网设备回收"},
    ],
    "review_stuck": [
        {"token": "负责人姓名", "example": "王五"},
        {"token": "待办清单", "example": "1. 某某有限公司（848DIA11742988）的「续签申请」待审核"},
        {"token": "扫描类型", "example": "审核卡单提醒"},
    ],
}

DUTY_ACCOUNT_MANAGER = "客户经理"
DUTY_NETWORK_MAINTENANCE = "网络维护责任人"
SOURCE_DUE_RENEWAL = "due_renewal"
SOURCE_DEVICE_RECYCLE = "device_recycle"
SOURCE_REVIEW_STUCK = "review_stuck"


def render_script_template(template: str, ctx: dict[str, str]) -> str:
    """替换已知占位符，未知占位符保留原样以便管理员发现。"""

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return ctx.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, template)


def _template_for(schedule: ScanSchedule, db: Session | None = None) -> str:
    """按扫描类型取系统话术；数据库缺失或正文为空时回退默认话术。"""

    if db is not None:
        role = f"notification_{schedule.scan_type}"
        script = db.scalar(select(Script).where(Script.role == role))
        if script is not None and (script.body or "").strip():
            return script.body
        logger.warning("扫描 #%s：系统话术 role=%s 缺失或正文为空，回退默认模板", schedule.id, role)
    return DEFAULT_TEMPLATES.get(schedule.scan_type, "")


def _get_zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local_date_utc(now: datetime, timezone_name: str) -> datetime:
    zone = _get_zone(timezone_name)
    local_now = _as_utc(now).astimezone(zone)
    return datetime.combine(local_now.date(), datetime.min.time(), tzinfo=zone).astimezone(
        timezone.utc
    )


def _existing_owner_phones(
    db: Session, source: str, day_start_utc: datetime
) -> set[str]:
    """当天同一来源已经通知过的负责人手机号。"""

    tasks = db.scalars(
        select(CallTask).where(
            CallTask.source == source,
            CallTask.created_at >= day_start_utc,
            CallTask.created_at < day_start_utc + timedelta(days=1),
        )
    ).all()
    phones: set[str] = set()
    for task in tasks:
        try:
            meta = json.loads(task.meta_json or "{}")
        except (TypeError, ValueError):
            continue
        phone = str(meta.get("owner_phone") or "").strip()
        if phone:
            phones.add(phone)
    return phones


def _make_scan_script(db: Session, schedule: ScanSchedule, body: str, scan_date: str) -> Script:
    """为负责人聚合后的渲染正文创建脚本并按配置生成音频。"""

    script = Script(title=f"[扫描]{schedule.name} {scan_date}", body=body)
    db.add(script)
    db.flush()
    settings = get_settings()
    if settings.tts_provider.strip().lower() != "none":
        script_service.generate_script_audio(db, script, settings)
    return script


def _maybe_enqueue_sms(
    db: Session, settings, schedule: ScanSchedule, task: CallTask, phone: str, body: str
) -> None:
    if schedule.sms_enabled and settings.sms_enabled:
        db.add(
            SmsNotification(
                call_task_id=task.id,
                phone=phone,
                content=body,
                status="pending",
            )
        )


def _task(
    db: Session,
    schedule: ScanSchedule,
    source: str,
    owner_phone: str,
    customer_name: str,
    body: str,
    scan_date: str,
    targets: list[dict[str, int]],
    settings,
) -> CallTask:
    script = _make_scan_script(db, schedule, body, scan_date)
    meta = {
        "scan_schedule_id": schedule.id,
        "owner_phone": owner_phone,
        "targets": targets,
        "rendered_script": body,
        "scan_date": scan_date,
    }
    task = CallTask(
        scan_schedule=schedule,
        customer_name=customer_name,
        script=script,
        dial_number=owner_phone,
        due_at=utcnow(),
        status="queued",
        source=source,
        max_attempts=settings.max_call_attempts,
        meta_json=json.dumps(meta, ensure_ascii=False),
    )
    db.add(task)
    db.flush()
    _maybe_enqueue_sms(db, settings, schedule, task, owner_phone, body)
    return task


def run_due_renewal_scan(
    db: Session, schedule: ScanSchedule, now: datetime | None = None
) -> int:
    now_utc = _as_utc(now) if now is not None else utcnow()
    zone = _get_zone(schedule.timezone)
    day_start_utc = _local_date_utc(now_utc, schedule.timezone)
    window_end_utc = day_start_utc + timedelta(days=schedule.lead_days + 1)
    scan_date = now_utc.astimezone(zone).strftime("%Y-%m-%d")
    template = _template_for(schedule, db)
    existing = _existing_owner_phones(db, SOURCE_DUE_RENEWAL, day_start_utc)
    settings = get_settings()

    grouped: dict[str, list[BusinessService]] = defaultdict(list)
    services = db.scalars(
        select(BusinessService)
        .where(BusinessService.is_active.is_(True), BusinessService.agreement_expires_at.is_not(None))
        .order_by(BusinessService.id.asc())
    ).all()
    for service in services:
        expires_utc = _as_utc(service.agreement_expires_at)
        if not (day_start_utc <= expires_utc < window_end_utc):
            continue
        phone = (service.account_manager_phone or "").strip()
        if not phone:
            logger.info("扫描 #%s：业务 %s 缺少%s或电话为空，跳过", schedule.id, service.service_number, DUTY_ACCOUNT_MANAGER)
            continue
        grouped[phone].append(service)

    created = 0
    for phone, items in grouped.items():
        if phone in existing:
            logger.info("扫描 #%s：负责人 %s 当天已有到期维系任务，跳过", schedule.id, phone)
            continue
        first = items[0]
        lines = []
        targets = []
        for service in items:
            expires_utc = _as_utc(service.agreement_expires_at)
            due_date = expires_utc.astimezone(zone).strftime("%Y-%m-%d")
            lines.append(f"{len(lines) + 1}. {service.customer_name}（{service.service_number}）协议{due_date}到期")
            targets.append({"business_service_id": service.id})
        ctx = {
            "负责人姓名": (first.account_manager_name or "").strip(),
            "待办清单": "\n".join(lines),
            "扫描类型": SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
        }
        body = render_script_template(template, ctx)
        _task(db, schedule, SOURCE_DUE_RENEWAL, phone, first.customer_name, body, scan_date, targets, settings)
        created += 1
    db.flush()
    logger.info("扫描 #%s「%s」：%s 生成 %d 条聚合通知任务", schedule.id, schedule.name, SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type), created)
    return created


def _is_retired_service(service: BusinessService, day_start_utc: datetime) -> bool:
    status_label = service.service_status_item.label if service.service_status_item else ""
    if "退网" in status_label:
        return True
    if service.agreement_expires_at is not None and _as_utc(service.agreement_expires_at) < day_start_utc:
        return True
    return False


def _device_recovered(device: NetworkDevice) -> bool:
    item = device.recovery_status_item
    if item is None:
        return False
    label = (item.label or "").strip()
    if label.startswith(("未", "否")):
        return False
    return "回收" in label or "完成" in label


def run_device_recycle_scan(
    db: Session, schedule: ScanSchedule, now: datetime | None = None
) -> int:
    now_utc = _as_utc(now) if now is not None else utcnow()
    zone = _get_zone(schedule.timezone)
    day_start_utc = _local_date_utc(now_utc, schedule.timezone)
    scan_date = now_utc.astimezone(zone).strftime("%Y-%m-%d")
    template = _template_for(schedule, db)
    existing = _existing_owner_phones(db, SOURCE_DEVICE_RECYCLE, day_start_utc)
    settings = get_settings()

    grouped: dict[str, list[tuple[BusinessService, NetworkDevice]]] = defaultdict(list)
    services = db.scalars(select(BusinessService).where(BusinessService.is_active.is_(True)).order_by(BusinessService.id.asc())).all()
    for service in services:
        if not _is_retired_service(service, day_start_utc):
            continue
        for device in service.devices:
            if not device.is_active:
                logger.info("扫描 #%s：设备 %s 已停用，跳过", schedule.id, device.device_code)
                continue
            if _device_recovered(device):
                continue
            phone = (device.maintenance_phone or "").strip()
            if not phone:
                logger.info("扫描 #%s：退网业务 %s 缺少%s或电话为空，跳过", schedule.id, service.service_number, DUTY_NETWORK_MAINTENANCE)
                continue
            grouped[phone].append((service, device))

    created = 0
    for phone, items in grouped.items():
        if phone in existing:
            logger.info("扫描 #%s：负责人 %s 当天已有设备回收任务，跳过", schedule.id, phone)
            continue
        first_service, first_device = items[0]
        lines = []
        targets = []
        for service, device in items:
            lines.append(f"{len(lines) + 1}. {service.customer_name}（{service.service_number}）设备{device.device_code}未回收")
            targets.append({"device_id": device.id, "business_service_id": service.id})
        ctx = {
            "负责人姓名": (first_device.maintenance_name or "").strip(),
            "待办清单": "\n".join(lines),
            "扫描类型": SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
        }
        body = render_script_template(template, ctx)
        _task(db, schedule, SOURCE_DEVICE_RECYCLE, phone, first_service.customer_name, body, scan_date, targets, settings)
        created += 1
    db.flush()
    logger.info("扫描 #%s「%s」：%s 生成 %d 条聚合通知任务", schedule.id, schedule.name, SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type), created)
    return created


def _reviewer_users(db: Session) -> list[User]:
    permission_id = db.scalar(select(Permission.id).where(Permission.code == "review"))
    if permission_id is None:
        return []
    role_ids = db.scalars(select(RolePermission.role_id).where(RolePermission.permission_id == permission_id)).all()
    if not role_ids:
        return []
    return db.scalars(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .where(User.is_enabled.is_(True), UserRole.role_id.in_(role_ids))
        .distinct()
        .order_by(User.id.asc())
    ).all()


def _change_set_business(db: Session, change_set: ChangeSet) -> BusinessService | None:
    for item in change_set.items:
        if item.entity_type.replace("_", "").lower() not in {"businessservice", "business"} or item.entity_id is None:
            continue
        service = db.get(BusinessService, item.entity_id)
        if service is not None:
            return service
    return None


def run_review_stuck_scan(
    db: Session, schedule: ScanSchedule, now: datetime | None = None
) -> int:
    now_utc = _as_utc(now) if now is not None else utcnow()
    zone = _get_zone(schedule.timezone)
    day_start_utc = _local_date_utc(now_utc, schedule.timezone)
    scan_date = now_utc.astimezone(zone).strftime("%Y-%m-%d")
    template = _template_for(schedule, db)
    existing = _existing_owner_phones(db, SOURCE_REVIEW_STUCK, day_start_utc)
    settings = get_settings()

    valid_sets: list[tuple[ChangeSet, BusinessService]] = []
    for change_set in db.scalars(select(ChangeSet).where(ChangeSet.status == "submitted").order_by(ChangeSet.id.asc())).all():
        service = _change_set_business(db, change_set)
        if service is None:
            logger.info("扫描 #%s：变更申请 %s（%s）无关联业务客户，跳过", schedule.id, change_set.id, change_set.title)
            continue
        valid_sets.append((change_set, service))

    grouped: dict[str, list[tuple[ChangeSet, BusinessService, User]]] = defaultdict(list)
    users_by_phone: dict[str, User] = {}
    for user in _reviewer_users(db):
        phone = (user.phone or "").strip()
        if not phone:
            continue
        users_by_phone.setdefault(phone, user)
    for phone, user in users_by_phone.items():
        for change_set, service in valid_sets:
            grouped[phone].append((change_set, service, user))
    created = 0
    for phone, items in grouped.items():
        if phone in existing:
            logger.info("扫描 #%s：审核负责人 %s 当天已有卡单提醒任务，跳过", schedule.id, phone)
            continue
        first_set, first_service, first_user = items[0]
        lines = []
        targets = []
        for change_set, service, _user in items:
            lines.append(f"{len(lines) + 1}. {service.customer_name}（{service.service_number}）的「{change_set.title}」待审核")
            targets.append({"change_set_id": change_set.id})
        ctx = {
            "负责人姓名": (first_user.real_name or "").strip() or first_user.username,
            "待办清单": "\n".join(lines),
            "扫描类型": SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
        }
        body = render_script_template(template, ctx)
        _task(db, schedule, SOURCE_REVIEW_STUCK, phone, first_service.customer_name, body, scan_date, targets, settings)
        created += 1
    db.flush()
    logger.info("扫描 #%s「%s」：%s 生成 %d 条聚合通知任务", schedule.id, schedule.name, SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type), created)
    return created


_SCAN_RUNNERS: dict[str, Callable[..., int]] = {
    SOURCE_DUE_RENEWAL: run_due_renewal_scan,
    SOURCE_DEVICE_RECYCLE: run_device_recycle_scan,
    SOURCE_REVIEW_STUCK: run_review_stuck_scan,
}


def run_scan_for_schedule(
    db: Session, schedule: ScanSchedule, now: datetime | None = None
) -> int:
    runner = _SCAN_RUNNERS.get(schedule.scan_type)
    try:
        if runner is None:
            raise ValueError(f"未知扫描类型：{schedule.scan_type}")
        count = runner(db, schedule, now=now)
    except Exception as exc:  # noqa: BLE001 - 扫描错误落配置并吞掉
        logger.exception("扫描计划 #%s「%s」执行失败：%s", schedule.id, schedule.name, exc)
        db.rollback()
        schedule.last_error = str(exc)
        return 0
    schedule.last_run_at = _as_utc(now) if now is not None else utcnow()
    schedule.last_error = ""
    return count
