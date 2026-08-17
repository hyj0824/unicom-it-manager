from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessService, ImportBatch, NetworkDevice, StagingRow

# 0811 台账标准列（0 起）：A-M 业务，N-W 设备。导入向导后续再支持表头映射，
# 当前按固定标准布局解析。
LEDGER_COLUMNS = [
    "号码",
    "户名",
    "县分",
    "网格",
    "服务状态",
    "入网时间",
    "协议到期时间",
    "业务类型",
    "渠道名称",
    "发展人",
    "发展人联系电话",
    "客户经理",
    "客户经理联系电话",
    "网络维护责任人",
    "网络维护责任人联系电话",
    "设备属性",
    "设备编码",
    "资产原值或物资购置价格",
    "设备及物资类型",
    "设备厂家+型号",
    "设备放置地点",
    "设备是否已回收",
    "设备未回收原因",
]

# 按数据质量结论监控的缺项列：这些列当前基本为空，导入时标记缺项而不是静默放行。
MISSING_WATCH_COLUMNS = [
    "协议到期时间",
    "客户经理",
    "网络维护责任人",
    "设备属性",
    "资产原值或物资购置价格",
    "设备放置地点",
]

DEVICE_CODE_SEPARATORS = re.compile(r"[、，,;；\n]+")
PHONE_RE = re.compile(r"^\+?[0-9]{5,20}$")


def parse_ledger_rows(path: Path) -> list[dict]:
    """读取台账工作簿，返回数据行（第 3 行起），跳过整行为空的行。"""

    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    rows: list[dict] = []
    for row_number, values in enumerate(sheet.iter_rows(min_row=3, values_only=True), start=3):
        if all(v is None or str(v).strip() == "" for v in values):
            continue
        raw = {}
        for index, column in enumerate(LEDGER_COLUMNS):
            value = values[index] if index < len(values) else None
            raw[column] = "" if value is None else str(value).strip()
        rows.append({"row_number": row_number, "raw": raw})
    workbook.close()
    return rows


def _split_device_codes(value: str) -> list[str]:
    return [part for part in DEVICE_CODE_SEPARATORS.split(value) if part.strip()]


def _validate_rows(rows: list[dict], existing_numbers: set[str], existing_codes: set[str]) -> list[dict]:
    """逐行校验，返回带状态的行。错误优先于缺项，重复次之。"""

    seen_numbers: dict[str, int] = {}
    seen_codes: dict[str, int] = {}
    for row in rows:
        raw = row["raw"]
        number = raw.get("号码", "")
        if number and number not in seen_numbers:
            seen_numbers[number] = row["row_number"]
        for code in _split_device_codes(raw.get("设备编码", "")):
            if code not in seen_codes:
                seen_codes[code] = row["row_number"]

    result: list[dict] = []
    for row in rows:
        raw = row["raw"]
        errors: list[str] = []
        if not raw.get("号码"):
            errors.append("业务号码为空")
        if not raw.get("户名"):
            errors.append("客户名称为空")
        if raw.get("号码") and seen_numbers.get(raw["号码"]) != row["row_number"]:
            errors.append(f"业务号码 {raw['号码']} 在本批次内重复（首次出现在第 {seen_numbers[raw['号码']]} 行）")
        if raw.get("号码") in existing_numbers:
            errors.append(f"业务号码 {raw['号码']} 已存在于正式库")
        for label in ("发展人联系电话", "客户经理联系电话", "网络维护责任人联系电话"):
            phone = raw.get(label, "")
            if phone and not PHONE_RE.match(phone):
                errors.append(f"{label}格式不正确（{phone}）")

        device_codes = _split_device_codes(raw.get("设备编码", ""))
        for code in device_codes:
            if seen_codes.get(code) != row["row_number"]:
                errors.append(
                    f"设备编码 {code} 同时出现在多个业务行（首次出现在第 {seen_codes[code]} 行）"
                )
            if code in existing_codes:
                errors.append(f"设备编码 {code} 已存在于正式库")

        mapped = {
            "service_number": raw.get("号码", ""),
            "customer_name": raw.get("户名", ""),
            "county": raw.get("县分", ""),
            "grid": raw.get("网格", ""),
            "service_status": raw.get("服务状态", ""),
            "business_type": raw.get("业务类型", ""),
            "channel_name": raw.get("渠道名称", ""),
            "accessed_at": raw.get("入网时间", ""),
            "agreement_expires_at": raw.get("协议到期时间", ""),
            "contacts": {
                "developer": {"name": raw.get("发展人", ""), "phone": raw.get("发展人联系电话", "")},
                "account_manager": {"name": raw.get("客户经理", ""), "phone": raw.get("客户经理联系电话", "")},
                "network_maintenance": {
                    "name": raw.get("网络维护责任人", ""),
                    "phone": raw.get("网络维护责任人联系电话", ""),
                },
            },
            "devices": [
                {
                    "device_code": code,
                    "asset_class": raw.get("设备属性", ""),
                    "asset_value": raw.get("资产原值或物资购置价格", ""),
                    "device_type": raw.get("设备及物资类型", ""),
                    "vendor_model": raw.get("设备厂家+型号", ""),
                    "location": raw.get("设备放置地点", ""),
                    "recovery_status": raw.get("设备是否已回收", ""),
                    "recovery_reason": raw.get("设备未回收原因", ""),
                }
                for code in device_codes
            ],
        }
        missing = [c for c in MISSING_WATCH_COLUMNS if not raw.get(c)]
        if errors:
            status = "error"
        elif missing:
            status = "missing"
        else:
            status = "valid"
        result.append(
            {
                "row_number": row["row_number"],
                "raw_json": json.dumps(raw, ensure_ascii=False),
                "mapped_json": json.dumps(mapped, ensure_ascii=False),
                "status": status,
                "error_messages": "\n".join(errors),
            }
        )
    return result


def import_ledger_workbook(db: Session, batch: ImportBatch, path: Path) -> ImportBatch:
    """把台账工作簿解析到暂存区并生成校验报告；不写正式表。"""

    existing_numbers = set(
        db.scalars(select(BusinessService.service_number)).all()
    )
    existing_codes = set(db.scalars(select(NetworkDevice.device_code)).all())

    rows = parse_ledger_rows(path)
    validated = _validate_rows(rows, existing_numbers, existing_codes)

    batch.total_rows = len(validated)
    batch.error_count = sum(1 for r in validated if r["status"] == "error")
    batch.missing_count = sum(1 for r in validated if r["status"] == "missing")
    batch.duplicate_count = sum(1 for r in validated if "重复" in r["error_messages"] or "已存在于正式库" in r["error_messages"])
    for row in validated:
        db.add(
            StagingRow(
                batch=batch,
                row_number=row["row_number"],
                raw_json=row["raw_json"],
                mapped_json=row["mapped_json"],
                status=row["status"],
                error_messages=row["error_messages"],
            )
        )
    batch.status = "ready"
    return batch
