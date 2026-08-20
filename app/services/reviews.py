from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BusinessService, ChangeItem, ChangeSet,
    DictionaryCategory, DictionaryItem, ImportBatch, NetworkDevice, StagingRow, utcnow,
)
from . import provisioning


class ChangeApplicationError(ValueError):
    pass


def _json(value: str) -> dict:
    try:
        return json.loads(value or "{}")
    except ValueError as exc:
        raise ChangeApplicationError("变更内容格式无效。") from exc


def _item_id(db: Session, category: str, label: str) -> int | None:
    if not label:
        return None
    label = {
        ("recovery_status", "是"): "已回收",
        ("recovery_status", "否"): "未回收",
    }.get((category, label), label)
    return db.scalar(
        select(DictionaryItem.id).join(DictionaryCategory).where(
            DictionaryCategory.code == category, DictionaryItem.label == label,
            DictionaryItem.is_active.is_(True),
        )
    )


def _local_date(value: str):
    from ..config import get_settings
    from .ledger import parse_local_date
    if not value:
        return None
    value = str(value).strip()
    if len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return parse_local_date(value, get_settings().default_timezone)


def _business_changed(service: BusinessService | None, payload: dict) -> bool:
    if service is None:
        return True
    if payload.get("customer_name", "") != service.customer_name:
        return True
    labels = {
        "county": service.county_item.label if service.county_item else "",
        "grid": service.grid_item.label if service.grid_item else "",
        "service_status": service.service_status_item.label if service.service_status_item else "",
        "business_type": service.business_type_item.label if service.business_type_item else "",
    }
    for key, value in labels.items():
        if payload.get(key, "") != value:
            return True
    if any((payload.get(key, "") or "") != (getattr(service, key, "") or "") for key in ("channel_name",)):
        return True
    accessed_at = _local_date(payload.get("accessed_at", ""))
    stored_accessed_at = service.accessed_at
    if accessed_at and stored_accessed_at and stored_accessed_at.tzinfo is None:
        accessed_at = accessed_at.replace(tzinfo=None)
    if accessed_at != stored_accessed_at:
        return True
    expires_at = _local_date(payload.get("agreement_expires_at", ""))
    stored_expires_at = service.agreement_expires_at
    if expires_at and stored_expires_at and stored_expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=None)
    if expires_at != stored_expires_at:
        return True
    legacy = payload.get("contacts") or {}
    values = {
        "developer_name": payload.get("developer_name", (legacy.get("developer") or {}).get("name", "")),
        "developer_phone": payload.get("developer_phone", (legacy.get("developer") or {}).get("phone", "")),
        "account_manager_name": payload.get("account_manager_name", (legacy.get("account_manager") or {}).get("name", "")),
        "account_manager_phone": payload.get("account_manager_phone", (legacy.get("account_manager") or {}).get("phone", "")),
    }
    return any(str(values[key] or "").strip() != str(getattr(service, key) or "").strip() for key in values)


def _device_changed(device: NetworkDevice | None, data: dict) -> bool:
    if device is None:
        return True
    labels = {
        "asset_class": device.asset_class_item.label if device.asset_class_item else "",
        "device_type": device.device_type_item.label if device.device_type_item else "",
        "recovery_status": device.recovery_status_item.label if device.recovery_status_item else "",
        "recovery_reason": device.recovery_reason_item.label if device.recovery_reason_item else "",
    }
    if any(data.get(key, "") != value for key, value in labels.items()):
        return True
    if any(data.get(key, "") != (getattr(device, key, "") or "") for key in ("vendor_model", "location")):
        return True
    try:
        value = Decimal(str(data.get("asset_value", "")).replace(",", "")) if data.get("asset_value") else None
    except InvalidOperation:
        return True
    if value != device.asset_value:
        return True
    maintenance_name = str(data.get("maintenance_name", "")).strip()
    maintenance_phone = str(data.get("maintenance_phone", "")).strip()
    if maintenance_name or maintenance_phone:
        if (maintenance_name, maintenance_phone) != (device.maintenance_name, device.maintenance_phone):
            return True
    return False


def _group_batch_rows(db: Session, batch: ImportBatch) -> list[dict]:
    rows = db.scalars(
        select(StagingRow).where(StagingRow.batch_id == batch.id, StagingRow.status.in_(("valid", "missing"))).order_by(StagingRow.row_number)
    ).all()
    grouped: dict[str, list[tuple[StagingRow, dict]]] = {}
    for row in rows:
        payload = _json(row.mapped_json)
        grouped.setdefault(payload.get("service_number", ""), []).append((row, payload))
    result = []
    for entries in grouped.values():
        first_row, base = entries[0]
        payload = dict(base)
        payload["staging_row_ids"] = [row.id for row, _ in entries]
        payload["row_numbers"] = [row.row_number for row, _ in entries]
        payload["devices"] = [device for _, data in entries for device in data.get("devices", [])]
        payload["row_status"] = "missing" if any(data.get("row_status") == "missing" or row.status == "missing" for row, data in entries) else "valid"
        result.append(payload)
    return result


def build_import_change_sets(db: Session, batch: ImportBatch, user_id: int | None) -> list[ChangeSet]:
    if batch.status != "ready":
        raise ChangeApplicationError("只有校验完成的批次可以提交审核。")
    pending = db.scalar(select(ChangeSet.id).where(ChangeSet.import_batch_id == batch.id, ChangeSet.status.in_(("submitted", "approved"))))
    if pending is not None:
        raise ChangeApplicationError("该批次仍有未完成的业务或网络审核申请。")
    if batch.error_count:
        raise ChangeApplicationError("批次仍有错误或归属冲突行，须在暂存工作台修正后才能提交。")
    groups = _group_batch_rows(db, batch)
    if not groups:
        raise ChangeApplicationError("没有可提交的暂存行。")

    changes: dict[str, ChangeSet] = {}
    for domain, label in (("business", "业务台账"), ("network", "网络设备")):
        change = ChangeSet(
            title=f"导入批次 #{batch.id}：{label}", domain=domain, status="submitted",
            reason="由扁平台账导入生成，审核通过后仍须单独应用。",
            import_batch_id=batch.id, created_by_user_id=user_id, submitted_at=utcnow(),
        )
        db.add(change)
        changes[domain] = change
    db.flush()

    item_count = 0
    for payload in groups:
        payload["batch_id"] = batch.id
        service = db.get(BusinessService, payload.get("existing_service_id")) if payload.get("existing_service_id") else None
        if _business_changed(service, payload):
            db.add(ChangeItem(
                change_set_id=changes["business"].id, entity_type="business_service",
                entity_id=payload.get("existing_service_id"), operation=payload.get("operation", "create"),
                base_version=payload.get("base_version"), patch_json=json.dumps(payload, ensure_ascii=False),
            ))
            item_count += 1
        for data in payload.get("devices", []):
            device = db.get(NetworkDevice, data.get("existing_device_id")) if data.get("existing_device_id") else None
            if not _device_changed(device, data):
                continue
            device_payload = {
                "batch_id": batch.id, "service_number": payload.get("service_number"), "customer_name": payload.get("customer_name"),
                "staging_row_ids": payload["staging_row_ids"], "row_numbers": payload["row_numbers"],
                "row_status": payload["row_status"], "device": data,
            }
            db.add(ChangeItem(
                change_set_id=changes["network"].id, entity_type="network_device",
                entity_id=data.get("existing_device_id"), operation="update" if device else "create",
                base_version=data.get("base_version"), patch_json=json.dumps(device_payload, ensure_ascii=False),
            ))
            item_count += 1
    db.flush()
    active = [change for change in changes.values() if change.items]
    for change in changes.values():
        if not change.items:
            db.delete(change)
    if not item_count:
        raise ChangeApplicationError("文件内容与当前正式台账一致，没有需要审核的变更。")
    batch.status = "reviewing"
    return active


# Kept as a compatibility alias for callers that previously expected one change set.
def build_import_change_set(db: Session, batch: ImportBatch, user_id: int | None) -> ChangeSet:
    changes = build_import_change_sets(db, batch, user_id)
    # Compatibility for older service callers: the web route uses the plural
    # API and presents separate domain reviews.  Legacy callers receive one
    # executable aggregate so their existing apply workflow remains valid.
    if len(changes) > 1:
        primary, secondary = changes[0], changes[1]
        for item in list(secondary.items):
            item.change_set = primary
        db.flush()
        db.delete(secondary)
        db.flush()
        return primary
    return changes[0]


def preview_change_item(db: Session, item: ChangeItem) -> dict:
    payload = _json(item.patch_json)
    if item.entity_type.replace("_", "").lower() == "networkdevice":
        data = payload.get("device", {})
        device = db.get(NetworkDevice, item.entity_id) if item.entity_id else None
        current = {} if device is None else {
            "设备编码": device.device_code, "设备属性": device.asset_class_item.label if device.asset_class_item else "",
            "设备类型": device.device_type_item.label if device.device_type_item else "", "厂家型号": device.vendor_model,
            "放置地点": device.location, "回收状态": device.recovery_status_item.label if device.recovery_status_item else "",
            "网络维护责任人": device.maintenance_name,
            "网络维护责任人联系电话": device.maintenance_phone,
        }
        proposed = {"所属业务": payload.get("service_number", ""), "设备编码": data.get("device_code", ""), "网络维护责任人": data.get("maintenance_name", ""), "网络维护责任人联系电话": data.get("maintenance_phone", ""), "设备属性": data.get("asset_class", ""), "设备类型": data.get("device_type", ""), "厂家型号": data.get("vendor_model", ""), "放置地点": data.get("location", ""), "回收状态": data.get("recovery_status", "")}
    else:
        service = db.get(BusinessService, item.entity_id) if item.entity_id else None
        current = {} if service is None else {"业务号码": service.service_number, "客户": service.customer_name, "县分": service.county_item.label if service.county_item else "", "网格": service.grid_item.label if service.grid_item else "", "服务状态": service.service_status_item.label if service.service_status_item else "", "业务类型": service.business_type_item.label if service.business_type_item else "", "渠道名称": service.channel_name}
        proposed = {"业务号码": payload.get("service_number", ""), "客户": payload.get("customer_name", ""), "县分": payload.get("county", ""), "网格": payload.get("grid", ""), "服务状态": payload.get("service_status", ""), "业务类型": payload.get("business_type", ""), "渠道名称": payload.get("channel_name", "")}
    return {"item": item, "payload": payload, "current": current, "proposed": proposed, "changed_fields": [key for key, value in proposed.items() if value and current.get(key, "") != value]}


def _apply_business(db: Session, item: ChangeItem) -> tuple[BusinessService, dict]:
    payload = _json(item.patch_json)
    service = db.get(BusinessService, item.entity_id) if item.entity_id else None
    if service is not None and service.version != item.base_version:
        raise ChangeApplicationError(f"业务号码 {service.service_number} 已被其他变更修改，请重新导出并合并。")
    if service is None:
        if db.scalars(select(BusinessService).where(BusinessService.service_number == payload.get("service_number", ""))).first():
            raise ChangeApplicationError(f"业务号码 {payload.get('service_number')} 已在提交后发生变化，请重新导出并合并。")
    customer_name = payload.get("customer_name", "").strip()
    if not customer_name:
        raise ChangeApplicationError("业务变更缺少客户名称。")
    if service is None:
        service = BusinessService(service_number=payload.get("service_number", ""))
        db.add(service); db.flush()
    service.customer_name = customer_name
    legacy = payload.get("contacts") or {}
    values = {
        "developer_name": payload.get("developer_name", (legacy.get("developer") or {}).get("name", "")),
        "developer_phone": payload.get("developer_phone", (legacy.get("developer") or {}).get("phone", "")),
        "account_manager_name": payload.get("account_manager_name", (legacy.get("account_manager") or {}).get("name", "")),
        "account_manager_phone": payload.get("account_manager_phone", (legacy.get("account_manager") or {}).get("phone", "")),
    }
    for key, value in values.items():
        setattr(service, key, str(value or "").strip())
    service.county_item_id = _item_id(db, "county", payload.get("county", "")); service.grid_item_id = _item_id(db, "grid", payload.get("grid", ""))
    service.service_status_item_id = _item_id(db, "service_status", payload.get("service_status", "")); service.business_type_item_id = _item_id(db, "business_type", payload.get("business_type", ""))
    # 数据质量与来源行只在导入 patch 显式提供时改写：人工变更申请
    # （change_requests）的完整快照携带 row_status 保持当前值，不带 batch_id
    # 时保留原 source_row，避免整体覆盖把来源行抹成导入批次占位文本。
    if "row_status" in payload:
        service.data_quality_status_item_id = _item_id(db, "data_quality_status", "缺项" if payload.get("row_status") == "missing" else "完整")
    service.channel_name = payload.get("channel_name", ""); service.accessed_at = _local_date(payload.get("accessed_at", "")); service.agreement_expires_at = _local_date(payload.get("agreement_expires_at", ""))
    if payload.get("batch_id") is not None:
        service.source_row = f"导入批次 #{payload.get('batch_id', '')} 第 {'、'.join(map(str, payload.get('row_numbers', [])))} 行"
    service.is_active = True
    if item.operation == "update": service.version += 1
    return service, payload


def _apply_device(db: Session, item: ChangeItem) -> tuple[NetworkDevice, dict]:
    payload = _json(item.patch_json); data = payload.get("device", {})
    service = db.scalars(select(BusinessService).where(BusinessService.service_number == payload.get("service_number", ""))).first()
    if service is None:
        raise ChangeApplicationError(f"设备 {data.get('device_code')} 所属业务尚未应用，请先应用业务审核申请。")
    device = db.get(NetworkDevice, item.entity_id) if item.entity_id else None
    if device is not None and device.version != item.base_version:
        raise ChangeApplicationError(f"设备编码 {device.device_code} 已被其他变更修改，请重新导出并合并。")
    if device is None:
        if db.scalars(select(NetworkDevice).where(NetworkDevice.device_code == data.get("device_code", ""))).first():
            raise ChangeApplicationError(f"设备编码 {data.get('device_code')} 已在提交后发生变化，请重新导出并合并。")
        device = NetworkDevice(device_code=data.get("device_code", ""), business_service_id=service.id)
        db.add(device)
    elif device.business_service_id != service.id:
        raise ChangeApplicationError(f"设备编码 {device.device_code} 已归属其他业务。")
    device.business_service_id = service.id; device.asset_class_item_id = _item_id(db, "asset_class", data.get("asset_class", "")); device.device_type_item_id = _item_id(db, "device_type", data.get("device_type", ""))
    device.recovery_status_item_id = _item_id(db, "recovery_status", data.get("recovery_status", "")); device.recovery_reason_item_id = _item_id(db, "recovery_reason", data.get("recovery_reason", ""))
    device.vendor_model = data.get("vendor_model", ""); device.location = data.get("location", "")
    try: device.asset_value = Decimal(str(data.get("asset_value", "")).replace(",", "")) if data.get("asset_value") else None
    except InvalidOperation as exc: raise ChangeApplicationError(f"设备编码 {data.get('device_code')} 的资产价格无效。") from exc
    maintenance_name = str(data.get("maintenance_name", "")).strip()
    maintenance_phone = str(data.get("maintenance_phone", "")).strip()
    device.maintenance_name = maintenance_name
    device.maintenance_phone = maintenance_phone
    if item.operation == "update": device.version += 1
    return device, payload


def _refresh_batch_status(db: Session, batch_id: int, change_set_id: int, user_id: int | None) -> None:
    batch = db.get(ImportBatch, batch_id)
    if batch is None: return
    # SessionLocal disables autoflush; persist the current set's new status before
    # checking whether any sibling domain still needs to be applied.
    db.flush()
    pending = db.scalar(select(ChangeSet.id).where(ChangeSet.import_batch_id == batch_id, ChangeSet.status != "applied"))
    if pending is None:
        batch.status = "applied"; batch.reviewed_by_user_id = user_id; batch.applied_change_set_id = change_set_id


def apply_change_set(db: Session, change_set: ChangeSet, user_id: int | None) -> int:
    if change_set.status != "approved": raise ChangeApplicationError("只有已通过的变更申请可以应用。")
    if not change_set.items: raise ChangeApplicationError("变更申请没有可应用的条目。")
    applied = 0
    for item in change_set.items:
        if item.entity_type.replace("_", "").lower() == "networkdevice":
            entity, payload = _apply_device(db, item)
            # 台账应用后按职责自动创建登录账号（网络维护责任人 → network_maintainer）。
            provisioning.provision_users_from_device_payload(db, payload)
        else:
            entity, payload = _apply_business(db, item)
            # 台账应用后按职责自动创建登录账号（客户经理 → business_maintainer）。
            provisioning.provision_users_from_business_payload(db, payload)
        for staging_id in payload.get("staging_row_ids", []):
            row = db.get(StagingRow, staging_id)
            if row: row.result_entity_type = item.entity_type; row.result_entity_id = entity.id
        applied += 1
    change_set.status = "applied"; change_set.applied_by_user_id = user_id; change_set.applied_at = utcnow()
    if change_set.import_batch_id: _refresh_batch_status(db, change_set.import_batch_id, change_set.id, user_id)
    return applied
