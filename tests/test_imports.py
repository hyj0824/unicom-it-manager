from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import ImportBatch, StagingRow
from app.services.imports import LEDGER_COLUMNS, import_ledger_workbook, parse_ledger_rows

BASE_DIR = Path(__file__).resolve().parent.parent


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


def test_parse_and_validate_ledger_rows(db: Session, tmp_path: Path) -> None:
    path = tmp_path / "ledger.xlsx"
    _write_ledger(
        path,
        [
            _row(),  # 第 3 行：完整
            _row(号码="848DIA000002", 协议到期时间="", 设备编码="21000011"),  # 第 4 行：缺项
            _row(号码="848DIA000001", 设备编码=""),  # 第 5 行：号码批次内重复
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
    assert staging[5].status == "error" and "批次内重复" in staging[5].error_messages
    assert staging[6].status == "error" and "业务号码为空" in staging[6].error_messages
    assert staging[7].status == "error" and "同时出现在多个业务行" in staging[7].error_messages
    assert staging[8].status == "valid"
    assert "21000003" in staging[8].mapped_json and "21000004" in staging[8].mapped_json


def test_existing_service_number_and_device_code_conflict(db: Session, tmp_path: Path) -> None:
    from app.models import BusinessService, Customer, NetworkDevice

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
    assert "已存在于正式库" in rows[3].error_messages
    assert "已存在于正式库" in rows[4].error_messages
