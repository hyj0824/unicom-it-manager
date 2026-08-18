from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import BusinessService, ChangeSet, Customer, ImportBatch, NetworkDevice, StagingRow
from app.services.imports import LEDGER_COLUMNS, import_ledger_workbook, parse_ledger_rows
from app.services.reviews import ChangeApplicationError, apply_change_set, build_import_change_set, build_import_change_sets

BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_LEDGER = BASE_DIR / "docs" / "政企专租线业务设备起底台账0811.xlsx"


@pytest.fixture()
def db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    url = f"sqlite:///{db_path}"
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def _row(**overrides) -> list:
    values = ["" for _ in LEDGER_COLUMNS]
    defaults = {
        "号码": "848DIA000001",
        "户名": "测试客户",
        "县分": "汉滨",
        "网格": "汉滨要客",
        "服务状态": "正常开机",
        "入网时间": "20220101",
        "协议到期时间": "20241231",
        "业务类型": "宽带业务",
        "渠道名称": "测试渠道",
        "发展人": "张三",
        "发展人联系电话": "13800000000",
        "客户经理": "李四",
        "客户经理联系电话": "13900000001",
        "网络维护责任人": "王五",
        "网络维护责任人联系电话": "13700000002",
        "设备属性": "资产类",
        "设备编码": "21000001",
        "资产原值或物资购置价格": "1200",
        "设备及物资类型": "光猫",
        "设备厂家+型号": "测试V1",
        "设备放置地点": "机房",
        "设备是否已回收": "否",
        "设备未回收原因": "在用",
    }
    defaults.update(overrides)
    for index, column in enumerate(LEDGER_COLUMNS):
        values[index] = defaults.get(column, "")
    return values


def _write_ledger(path: Path, rows: list[list]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["业务信息"] + [None] * 12 + ["设备信息"] + [None] * 18)
    sheet.append(list(LEDGER_COLUMNS))
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def test_parenthesized_flat_headers_are_supported(tmp_path: Path) -> None:
    path = tmp_path / "alias.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["业务信息"] + [None] * 12 + ["设备信息"] + [None] * 18)
    headers = list(LEDGER_COLUMNS)
    headers[15] = "设备属性（资产类/成本类）"
    headers[16] = "设备编码（资产及非资产）"
    headers[21] = "设备是否已回收（是/否）"
    headers[22] = "设备未回收原因（在用/遗失/纠纷/其他）"
    sheet.append(headers)
    sheet.append(_row())
    workbook.save(path)
    assert parse_ledger_rows(path)[0]["raw"]["设备编码"] == "21000001"


def test_sample_ledger_enters_staging_with_alias_headers(db: Session) -> None:
    """The supplied 0811 ledger must stage rather than fail on explanatory headers."""

    batch = ImportBatch(file_name=SAMPLE_LEDGER.name, status="validating")
    db.add(batch)
    db.flush()
    import_ledger_workbook(db, batch, SAMPLE_LEDGER)
    assert batch.total_rows > 3_000
    assert batch.missing_count > 0
    assert batch.error_count > 0  # Required business keys / cross-service devices need repair.
    assert "设备编码（资产及非资产）" in batch.header_mapping_json


def test_parse_and_validate_ledger_rows(db: Session, tmp_path: Path) -> None:
    path = tmp_path / "ledger.xlsx"
    _write_ledger(
        path,
        [
            _row(),  # 第 3 行：完整
            _row(号码="848DIA000002", 协议到期时间="", 设备编码="21000011"),  # 第 4 行：缺项
            _row(号码="848DIA000001", **{"设备编码": "", "设备属性": "", "资产原值或物资购置价格": "", "设备及物资类型": "", "设备厂家+型号": "", "设备放置地点": "", "设备是否已回收": "", "设备未回收原因": ""}),  # 第 5 行：同业务无设备行，允许
            _row(号码="", 设备编码="21000012"),  # 第 6 行：号码为空
            _row(号码="848DIA000003", 设备编码="21000001"),  # 第 7 行：设备编码冲突
            _row(号码="848DIA000004", 设备编码="21000003、21000004"),  # 第 8 行：拆分多设备
        ],
    )

    rows = parse_ledger_rows(path)
    assert [r["row_number"] for r in rows] == [3, 4, 5, 6, 7, 8]

    batch = ImportBatch(file_name="ledger.xlsx", status="validating")
    db.add(batch)
    db.flush()
    import_ledger_workbook(db, batch, path)

    staging = {
        row.row_number: row
        for row in db.query(StagingRow).filter_by(batch_id=batch.id).all()
    }
    assert batch.total_rows == 6
    assert batch.status == "ready"
    assert staging[3].status == "valid"
    assert staging[4].status == "missing"
    assert staging[5].status == "error" and "重复且该行未提供设备编码" in staging[5].error_messages
    assert staging[6].status == "error" and "业务号码为空" in staging[6].error_messages
    assert staging[7].status == "error" and "同时出现在多个业务行" in staging[7].error_messages
    assert staging[8].status == "valid"
    assert "21000003" in staging[8].mapped_json and "21000004" in staging[8].mapped_json


def test_existing_service_is_an_update_and_cross_service_device_is_a_conflict(
    db: Session, tmp_path: Path
) -> None:
    customer = Customer(name="已有客户")
    db.add(customer)
    db.flush()
    service = BusinessService(service_number="848OLD00001", customer_id=customer.id)
    db.add(service)
    db.flush()
    db.add(NetworkDevice(device_code="21099999", business_service_id=service.id))
    db.commit()

    path = tmp_path / "ledger2.xlsx"
    _write_ledger(
        path,
        [
            _row(号码="848OLD00001"),
            _row(号码="848DIA000009", 设备编码="21099999"),
        ],
    )
    batch = ImportBatch(file_name="ledger2.xlsx", status="validating")
    db.add(batch)
    db.flush()
    import_ledger_workbook(db, batch, path)

    rows = {
        row.row_number: row
        for row in db.query(StagingRow).filter_by(batch_id=batch.id).all()
    }
    update_payload = json.loads(rows[3].mapped_json)
    assert rows[3].status == "valid"
    assert update_payload["operation"] == "update"
    assert update_payload["existing_service_id"] == service.id
    assert rows[4].status == "error"
    assert "已归属其他业务" in rows[4].error_messages


def test_import_change_set_applies_rows_and_records_result(db: Session, tmp_path: Path) -> None:
    path = tmp_path / "apply.xlsx"
    _write_ledger(path, [_row(号码="848DIA000099", 设备编码="21000099")])
    batch = ImportBatch(file_name="apply.xlsx", status="validating")
    db.add(batch)
    db.flush()
    import_ledger_workbook(db, batch, path)

    change_sets = build_import_change_sets(db, batch, user_id=11)
    assert {change.domain for change in change_sets} == {"business", "network"}
    assert batch.status == "reviewing"
    for change_set in change_sets:
        change_set.status = "approved"
    for change_set in sorted(change_sets, key=lambda item: item.domain):
        assert apply_change_set(db, change_set, user_id=22) == 1
    db.commit()

    service = db.scalars(
        select(BusinessService).where(BusinessService.service_number == "848DIA000099")
    ).one()
    device = db.scalars(
        select(NetworkDevice).where(NetworkDevice.device_code == "21000099")
    ).one()
    row = db.scalars(select(StagingRow).where(StagingRow.batch_id == batch.id)).one()
    assert service.customer.name == "测试客户"
    assert device.business_service_id == service.id
    assert row.result_entity_id == service.id
    assert batch.status == "applied"
    assert all(change.status == "applied" for change in change_sets)


def test_apply_rejects_stale_service_version_without_partial_write(db: Session, tmp_path: Path) -> None:
    customer = Customer(name="已有客户")
    db.add(customer)
    db.flush()
    service = BusinessService(service_number="848OLD00002", customer_id=customer.id)
    db.add(service)
    db.commit()

    path = tmp_path / "conflict.xlsx"
    _write_ledger(path, [_row(号码="848OLD00002", 户名="更新客户", **{"设备编码": "", "设备属性": "", "资产原值或物资购置价格": "", "设备及物资类型": "", "设备厂家+型号": "", "设备放置地点": "", "设备是否已回收": "", "设备未回收原因": ""})])
    batch = ImportBatch(file_name="conflict.xlsx", status="validating")
    db.add(batch)
    db.flush()
    import_ledger_workbook(db, batch, path)
    change_set = build_import_change_set(db, batch, user_id=11)
    change_set.status = "approved"
    service.version += 1
    db.commit()

    with pytest.raises(ChangeApplicationError, match="已被其他变更修改"):
        apply_change_set(db, change_set, user_id=22)
    db.rollback()
    assert db.get(BusinessService, service.id).customer.name == "已有客户"
