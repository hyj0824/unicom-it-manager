import json
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.models import BusinessService, ImportBatch, NetworkDevice, StagingRow
from app.services.imports import LEDGER_COLUMNS, import_ledger_workbook, parse_ledger_rows
from app.services.reviews import apply_change_set, build_import_change_sets
from tests.flat_helpers import make_session


@pytest.fixture()
def db(tmp_path):
    session = make_session(tmp_path / "imports.db")
    yield session
    session.close()


def row(**overrides):
    values = {
        "号码": "S1", "户名": "客户甲", "服务状态": "正常开机", "入网时间": "20220101",
        "协议到期时间": "20261231", "业务类型": "宽带业务", "发展人": "开发",
        "发展人联系电话": "13800000000", "客户经理": "经理", "客户经理联系电话": "13900000000",
        "网络维护责任人": "维护", "网络维护责任人联系电话": "13700000000", "设备属性": "资产类",
        "设备编码": "D1", "资产原值或物资购置价格": "100", "设备及物资类型": "光猫",
        "设备厂家+型号": "V1", "设备放置地点": "机房", "设备是否已回收": "否", "设备未回收原因": "在用",
    }
    values.update(overrides)
    return [values.get(column, "") for column in LEDGER_COLUMNS]


def write_book(path: Path, rows):
    wb = Workbook(); ws = wb.active
    ws.append(["业务信息"] * len(LEDGER_COLUMNS)); ws.append(LEDGER_COLUMNS)
    for item in rows: ws.append(item)
    wb.save(path)


def test_flat_phone_fields_are_parsed_and_validated(tmp_path):
    path = tmp_path / "ledger.xlsx"; write_book(path, [row()])
    parsed = parse_ledger_rows(path)
    assert parsed[0]["raw"]["客户经理联系电话"] == "13900000000"


def test_import_apply_writes_flat_snapshots(db, tmp_path):
    path = tmp_path / "ledger.xlsx"; write_book(path, [row()])
    batch = ImportBatch(file_name="ledger.xlsx", status="validating"); db.add(batch); db.flush()
    import_ledger_workbook(db, batch, path)
    changes = build_import_change_sets(db, batch, 1)
    for change in changes:
        change.status = "approved"
        assert apply_change_set(db, change, 2) == 1
    db.commit()
    service = db.scalars(select(BusinessService)).one()
    device = db.scalars(select(NetworkDevice)).one()
    assert service.customer_name == "客户甲" and service.account_manager_phone == "13900000000"
    assert device.maintenance_name == "维护" and device.maintenance_phone == "13700000000"
    assert db.scalars(select(StagingRow)).one().result_entity_id == service.id
