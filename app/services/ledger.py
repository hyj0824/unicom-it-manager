from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AuditLog, BusinessService, CustomerContact, NetworkDevice
from .plans import get_zone


def log_action(
    db: Session,
    action: str,
    user_id: int | None = None,
    entity_type: str = "",
    entity_id: int | None = None,
    detail: str = "",
    ip_address: str = "",
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail_json=detail,
            ip_address=ip_address,
        )
    )


def validate_service_number(db: Session, value: str, exclude_id: int | None = None) -> str | None:
    value = value.strip()
    if not value:
        return "业务号码不能为空。"
    query = select(BusinessService.id).where(BusinessService.service_number == value)
    if exclude_id is not None:
        query = query.where(BusinessService.id != exclude_id)
    if db.scalars(query).first() is not None:
        return f"业务号码 {value} 已存在。"
    return None


def validate_device_code(db: Session, value: str, exclude_id: int | None = None) -> str | None:
    value = value.strip()
    if not value:
        return "设备编码不能为空。"
    query = select(NetworkDevice.id).where(NetworkDevice.device_code == value)
    if exclude_id is not None:
        query = query.where(NetworkDevice.id != exclude_id)
    if db.scalars(query).first() is not None:
        return f"设备编码 {value} 已存在（设备编码全局唯一）。"
    return None


def business_missing_fields(db: Session) -> list[dict[str, object]]:
    """业务实例缺项统计：按字段聚合缺失行数（仅统计有效记录）。"""

    fields = [
        ("county_item_id", "县分"),
        ("grid_item_id", "网格"),
        ("service_status_item_id", "服务状态"),
        ("business_type_item_id", "业务类型"),
        ("agreement_expires_at", "协议到期时间"),
        ("accessed_at", "入网时间"),
    ]
    rows: list[dict[str, object]] = []
    for column, label in fields:
        count = db.scalar(
            select(func.count(BusinessService.id)).where(
                BusinessService.is_active.is_(True),
                getattr(BusinessService, column).is_(None),
            )
        ) or 0
        if count:
            rows.append({"entity": "业务", "field": label, "count": count})
    return rows


def device_missing_fields(db: Session) -> list[dict[str, object]]:
    fields = [
        ("asset_class_item_id", "设备属性"),
        ("device_type_item_id", "设备类型"),
        ("asset_value", "资产价格"),
        ("location", "放置地点"),
        ("recovery_status_item_id", "回收状态"),
    ]
    rows: list[dict[str, object]] = []
    for column, label in fields:
        count = db.scalar(
            select(func.count(NetworkDevice.id)).where(
                NetworkDevice.is_active.is_(True),
                getattr(NetworkDevice, column).is_(None),
            )
        ) or 0
        if count:
            rows.append({"entity": "设备", "field": label, "count": count})
    return rows


def contact_options(db: Session) -> list[CustomerContact]:
    """设备维护责任人候选：所有有效客户-联系人关联。"""

    return db.scalars(
        select(CustomerContact)
        .where(CustomerContact.is_active.is_(True))
        .order_by(CustomerContact.id.asc())
    ).all()


def parse_local_date(value: str, timezone_name: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_zone(timezone_name))
    return parsed.astimezone(get_zone("UTC"))
