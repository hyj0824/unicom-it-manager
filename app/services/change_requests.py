from __future__ import annotations

"""变更申请（change requests）：维系与回收工作台提交的审核申请。

运营支撑助手的业务闭环：每日扫描生成「到期维系 / 退网设备回收」通知任务，
工作人员收到通知后在本模块提交变更申请，经审核中心审核、应用后写入正式
台账，扫描自然不再重复通知：

- 续签：修改业务协议到期时间（agreement_expires_at）；
- 退网：业务服务状态改为「主动退网(申请拆机)」（service_status 字典种子标签）；
- 回收：设备回收状态改为「已回收」（recovery_status 字典种子标签）。

每个申请是独立 ChangeSet，提交即进入审核（status=submitted）。patch 沿用
`app/services/reviews.py` 的导入 payload 结构（中文键 + 字典项 label），
`_apply_business` / `_apply_device` 会整体覆盖全部字段，因此 patch 必须包含
完整快照（含联系人、数据质量等），版本冲突由应用时的 base_version 校验保证。
"""

import json
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    BusinessService,
    CallTask,
    ChangeItem,
    ChangeSet,
    DictionaryCategory,
    DictionaryItem,
    NetworkDevice,
    ScanSchedule,
    utcnow,
)
from . import scans
from .ledger import parse_local_date
from .plans import get_zone

# 与扫描通知一致的目标状态标签（字典种子，见 alembic 种子迁移）。
RETIRE_STATUS_LABEL = "主动退网(申请拆机)"
RECOVERED_STATUS_LABEL = "已回收"

# 到期窗口默认提前天数：与 ScanSchedule.lead_days 默认值保持一致。
DEFAULT_LEAD_DAYS = 14

# submit_business_update 允许的变更字段（中文键，对应 _apply_business payload）。
BUSINESS_UPDATE_FIELDS = {"agreement_expires_at", "service_status"}


def _as_utc(value: datetime | None) -> datetime | None:
    """naive 视为 UTC（台账导入与 SQLite 存储都是 naive UTC）。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _label(item: DictionaryItem | None) -> str:
    return item.label if item is not None else ""


def _format_date(value: datetime | None, timezone_name: str) -> str:
    value = _as_utc(value)
    if value is None:
        return ""
    return value.astimezone(get_zone(timezone_name)).strftime("%Y-%m-%d")


def _contact_by_duty(service: BusinessService, duty: str) -> tuple[str, str]:
    """取客户启用的指定职责联系人（名称、电话），优先有电话号码的一条。"""

    links = [
        link
        for link in service.customer.contact_links
        if link.is_active and link.duty == duty
    ]
    for link in links:
        if (link.contact.phone or "").strip():
            return (link.contact.name or "").strip(), link.contact.phone.strip()
    if links:
        link = links[0]
        return (link.contact.name or "").strip(), (link.contact.phone or "").strip()
    return "", ""


def _dictionary_label_exists(db: Session, category: str, label: str) -> bool:
    """label 是否存在于启用的字典分类（与 reviews._item_id 的查找口径一致）。"""

    return db.scalar(
        select(DictionaryItem.id)
        .join(DictionaryCategory)
        .where(
            DictionaryCategory.code == category,
            DictionaryItem.label == label,
            DictionaryItem.is_active.is_(True),
        )
    ) is not None


def _validated_expiry_date(value: str) -> str:
    """校验续签日期：格式 YYYY-MM-DD 且晚于今天，返回规范化日期字符串。"""

    settings = get_settings()
    zone = get_zone(settings.default_timezone)
    try:
        parsed = parse_local_date(value, settings.default_timezone)
    except ValueError as exc:
        raise ValueError("协议到期时间格式不正确（应为 YYYY-MM-DD）。") from exc
    if parsed is None:
        raise ValueError("新的协议到期时间不能为空。")
    local_date = parsed.astimezone(zone).date()
    if local_date <= datetime.now(zone).date():
        raise ValueError("新的协议到期时间必须晚于今天。")
    return local_date.isoformat()


def business_snapshot_patch(db: Session, service: BusinessService) -> dict:
    """把业务当前值构造成 `reviews._apply_business` 认识的完整 patch（中文键）。

    `_apply_business` 会整体覆盖所有字段，因此 patch 必须包含完整快照：
    字典项用 label 字符串（应用时由 `_item_id` 转回 id）、日期用 YYYY-MM-DD、
    contacts 用导入 payload 的 developer / account_manager 结构。
    """

    settings = get_settings()
    contacts: dict[str, dict[str, str]] = {}
    for key, duty in (("developer", "发展人"), ("account_manager", "客户经理")):
        name, phone = _contact_by_duty(service, duty)
        if name or phone:
            contacts[key] = {"name": name, "phone": phone}
    quality = _label(service.data_quality_status_item)
    return {
        "service_number": service.service_number,
        "customer_name": service.customer.name if service.customer else "",
        "county": _label(service.county_item),
        "grid": _label(service.grid_item),
        "service_status": _label(service.service_status_item),
        "business_type": _label(service.business_type_item),
        "channel_name": service.channel_name or "",
        "accessed_at": _format_date(service.accessed_at, settings.default_timezone),
        "agreement_expires_at": _format_date(
            service.agreement_expires_at, settings.default_timezone
        ),
        # _apply_business 用 row_status 推导数据质量；快照保持当前值不变。
        "row_status": "missing" if quality == "缺项" else "valid",
        "contacts": contacts,
    }


def device_snapshot_patch(db: Session, device: NetworkDevice) -> dict:
    """设备完整快照：`reviews._apply_device` 的 payload 结构 + 回收状态设为已回收。

    `_apply_device` 只要求业务上下文 service_number（用于定位业务），业务字段
    不会被设备应用逻辑改写，因此不需要在 patch 中带业务快照。
    """

    service = device.business_service
    if service is None:
        raise ValueError(f"设备 {device.device_code} 未关联业务，不能提交回收申请。")
    link = device.maintenance_contact
    maintenance_name = link.contact.name if link and link.contact else ""
    maintenance_phone = link.contact.phone if link and link.contact else ""
    return {
        "service_number": service.service_number,
        "customer_name": service.customer.name if service.customer else "",
        "device": {
            "device_code": device.device_code,
            "asset_class": _label(device.asset_class_item),
            "asset_value": (
                str(device.asset_value) if device.asset_value is not None else ""
            ),
            "device_type": _label(device.device_type_item),
            "vendor_model": device.vendor_model or "",
            "location": device.location or "",
            "recovery_status": RECOVERED_STATUS_LABEL,
            # 已回收后回收原因不再适用，按台账填写规则留空。
            "recovery_reason": "",
            "maintenance_name": maintenance_name,
            "maintenance_phone": maintenance_phone,
        },
    }


def _new_change_set(
    db: Session, title: str, domain: str, reason: str, user_id: int | None
) -> ChangeSet:
    change_set = ChangeSet(
        title=title,
        domain=domain,
        status="submitted",
        reason=reason,
        created_by_user_id=user_id,
        submitted_at=utcnow(),
    )
    db.add(change_set)
    db.flush()
    return change_set


def submit_business_update(
    db: Session, service: BusinessService, updates: dict, reason: str, user_id: int | None
) -> ChangeSet:
    """提交业务变更申请（续签 / 退网）。

    patch = 完整快照 + updates 覆盖；base_version 取提交时的 service.version，
    应用时由 `_apply_business` 校验版本冲突。校验失败抛 ValueError（中文提示）。
    """

    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("申请理由不能为空。")
    if not updates:
        raise ValueError("没有需要提交的变更内容。")
    unknown = set(updates) - BUSINESS_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"不支持的变更字段：{'、'.join(sorted(unknown))}。")

    patch = business_snapshot_patch(db, service)
    is_retire = False
    if "agreement_expires_at" in updates:
        value = str(updates.get("agreement_expires_at") or "").strip()
        if not value:
            raise ValueError("新的协议到期时间不能为空。")
        patch["agreement_expires_at"] = _validated_expiry_date(value)
    if "service_status" in updates:
        label = str(updates.get("service_status") or "").strip()
        if not label:
            raise ValueError("服务状态不能为空。")
        if not _dictionary_label_exists(db, "service_status", label):
            raise ValueError(f"服务状态「{label}」不在服务状态字典中。")
        patch["service_status"] = label
        is_retire = "退网" in label

    title = (
        f"退网申请：{service.service_number}"
        if is_retire
        else f"续签申请：{service.service_number}"
    )
    change_set = _new_change_set(db, title, "business", reason, user_id)
    db.add(
        ChangeItem(
            change_set_id=change_set.id,
            entity_type="BusinessService",
            entity_id=service.id,
            operation="update",
            base_version=service.version,
            patch_json=json.dumps(patch, ensure_ascii=False),
        )
    )
    db.flush()
    return change_set


def submit_device_recovery(
    db: Session, device: NetworkDevice, reason: str, user_id: int | None
) -> ChangeSet:
    """提交设备回收完成申请：回收状态改为「已回收」，走 network 域审核。"""

    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("申请理由不能为空。")
    if scans._device_recovered(device):
        raise ValueError(f"设备 {device.device_code} 已标记回收，无需重复申请。")
    payload = device_snapshot_patch(db, device)
    change_set = _new_change_set(
        db, f"设备回收申请：{device.device_code}", "network", reason, user_id
    )
    db.add(
        ChangeItem(
            change_set_id=change_set.id,
            entity_type="NetworkDevice",
            entity_id=device.id,
            operation="update",
            base_version=device.version,
            patch_json=json.dumps(payload, ensure_ascii=False),
        )
    )
    db.flush()
    return change_set


# ---------------------------------------------------------------------------
# 工作台列表查询（与 scans 的判定口径一致，保证页面与通知扫描看到同一批数据）
# ---------------------------------------------------------------------------


def due_renewal_lead_days(db: Session) -> int:
    """到期窗口提前天数：取启用的到期维系扫描配置中的最大值，无配置时用默认 14。

    扫描按各配置自己的 lead_days 分别触发，取最大值保证工作台覆盖所有
    可能被通知的业务（与页面标题注明的提前天数一致）。
    """

    value = db.scalar(
        select(func.max(ScanSchedule.lead_days)).where(
            ScanSchedule.scan_type == scans.SOURCE_DUE_RENEWAL,
            ScanSchedule.enabled.is_(True),
        )
    )
    return int(value) if value is not None else DEFAULT_LEAD_DAYS


def _day_start_utc(now_utc: datetime, timezone_name: str) -> datetime:
    zone = get_zone(timezone_name)
    local_now = _as_utc(now_utc).astimezone(zone)
    return datetime.combine(local_now.date(), time.min, tzinfo=zone).astimezone(
        timezone.utc
    )


def due_window_bounds(
    now: datetime, timezone_name: str, lead_days: int
) -> tuple[datetime, datetime]:
    """到期窗口 [当天零点, 当天零点 + lead_days + 1 天)，UTC 闭开区间。

    与 `scans.run_due_renewal_scan` 的窗口语义一致：到期日恰为「提前
    lead_days 天」的业务也纳入范围。
    """

    day_start = _day_start_utc(now, timezone_name)
    return day_start, day_start + timedelta(days=lead_days + 1)


def _notified_target_ids(
    db: Session, source: str, day_start_utc: datetime, meta_key: str
) -> set[int]:
    """当天已入队扫描通知任务的目标 id 集合（meta_json 解析，口径同 scans）。"""

    tasks = db.scalars(
        select(CallTask).where(
            CallTask.source == source,
            CallTask.created_at >= day_start_utc,
            CallTask.created_at < day_start_utc + timedelta(days=1),
        )
    ).all()
    ids: set[int] = set()
    for task in tasks:
        try:
            meta = json.loads(task.meta_json or "{}")
        except ValueError:
            continue
        value = meta.get(meta_key)
        if value is not None:
            ids.add(int(value))
    return ids


def _latest_change_map(db: Session, entity_type: str) -> dict[int, tuple[str, int]]:
    """每个实体最近一次申请的状态与申请 id（含导入变更项，口径不分大小写）。"""

    items = db.scalars(
        select(ChangeItem)
        .where(ChangeItem.entity_type.in_((entity_type, entity_type.lower())))
        .order_by(ChangeItem.id.desc())
    ).all()
    latest: dict[int, tuple[str, int]] = {}
    for item in items:
        if item.entity_id is None:
            continue
        latest.setdefault(
            item.entity_id, (item.change_set.status, item.change_set.id)
        )
    return latest


def list_due_renewal_rows(
    db: Session, now: datetime | None = None, timezone_name: str | None = None
) -> list[dict]:
    """到期窗口内的业务行：协议到期日、客户经理、今日是否已通知、最近申请状态。"""

    settings = get_settings()
    tz = timezone_name or settings.default_timezone
    lead_days = due_renewal_lead_days(db)
    now_utc = _as_utc(now) if now is not None else utcnow()
    day_start, window_end = due_window_bounds(now_utc, tz, lead_days)

    services = db.scalars(
        select(BusinessService)
        .where(
            BusinessService.is_active.is_(True),
            BusinessService.agreement_expires_at.is_not(None),
        )
        .order_by(BusinessService.agreement_expires_at.asc(), BusinessService.id.asc())
    ).all()
    notified = _notified_target_ids(
        db, scans.SOURCE_DUE_RENEWAL, day_start, "business_service_id"
    )
    latest = _latest_change_map(db, "BusinessService")
    zone = get_zone(tz)
    rows: list[dict] = []
    for service in services:
        expires_utc = _as_utc(service.agreement_expires_at)
        if not (day_start <= expires_utc < window_end):
            continue
        account_manager, _ = _contact_by_duty(service, "客户经理")
        rows.append(
            {
                "service": service,
                "expires_label": expires_utc.astimezone(zone).strftime("%Y-%m-%d"),
                "account_manager": account_manager,
                "notified_today": service.id in notified,
                "latest_change": latest.get(service.id),
            }
        )
    return rows


def list_recycle_device_rows(
    db: Session, now: datetime | None = None, timezone_name: str | None = None
) -> list[dict]:
    """退网未回收设备行：复用 scans 的退网业务判定与回收状态判定。"""

    settings = get_settings()
    tz = timezone_name or settings.default_timezone
    now_utc = _as_utc(now) if now is not None else utcnow()
    day_start = _day_start_utc(now_utc, tz)

    services = db.scalars(
        select(BusinessService)
        .where(BusinessService.is_active.is_(True))
        .order_by(BusinessService.id.asc())
    ).all()
    rows: list[dict] = []
    for service in services:
        if not scans._is_retired_service(service, day_start):
            continue
        for device in service.devices:
            if not device.is_active or scans._device_recovered(device):
                continue
            link = device.maintenance_contact
            maintenance_name = (
                link.contact.name if link and link.contact else ""
            ).strip()
            if not maintenance_name:
                maintenance_name, _ = _contact_by_duty(service, "网络维护责任人")
            rows.append(
                {
                    "device": device,
                    "service": service,
                    "maintenance_name": maintenance_name,
                }
            )
    return rows
