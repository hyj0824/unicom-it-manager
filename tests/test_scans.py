import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import BusinessService, CallTask, NetworkDevice, ScanSchedule
from app.services.scans import (
    DEFAULT_TEMPLATES,
    PLACEHOLDER_SPECS,
    render_script_template,
    run_device_recycle_scan,
    run_due_renewal_scan,
)
from tests.flat_helpers import make_session

NOW = datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path):
    session = make_session(tmp_path / "scans.db")
    yield session
    session.close()


def schedule(db, scan_type, lead_days=14):
    # 种子迁移已创建三类固定配置；测试改为更新对应行。
    item = db.scalars(select(ScanSchedule).where(ScanSchedule.scan_type == scan_type)).first()
    if item is None:
        item = ScanSchedule(name=scan_type, scan_type=scan_type)
        db.add(item)
    item.name = "测试"
    item.lead_days = lead_days
    item.enabled = True
    item.sms_enabled = False
    item.timezone = "Asia/Shanghai"
    item.cron_expr = "0 9 * * *"
    db.flush()
    return item


def test_template_and_defaults():
    assert render_script_template("{{ 客户名称 }} {{missing}}", {"客户名称": "甲"}) == "甲 {{missing}}"
    assert "{{负责人姓名}}" in DEFAULT_TEMPLATES["due_renewal"]


def test_placeholder_specs_cover_default_template_tokens():
    for scan_type, template in DEFAULT_TEMPLATES.items():
        template_tokens = {
            match.group(1).strip()
            for match in re.finditer(r"\{\{\s*([^{}]+?)\s*\}\}", template)
        }
        spec_tokens = {spec["token"] for spec in PLACEHOLDER_SPECS[scan_type]}
        assert template_tokens <= spec_tokens


def test_due_renewal_uses_flat_manager_snapshot_and_deduplicates(db):
    service = BusinessService(
        service_number="S1", customer_name="客户甲", account_manager_name="经理",
        account_manager_phone="13800000000",
        agreement_expires_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    db.add(service); db.flush()
    sch = schedule(db, "due_renewal")
    assert run_due_renewal_scan(db, sch, NOW) == 1
    task = db.scalars(select(CallTask)).one()
    assert task.customer_name == "客户甲" and task.dial_number == "13800000000"
    meta = json.loads(task.meta_json)
    assert meta["targets"] == [{"business_service_id": service.id}]
    assert meta["owner_phone"] == "13800000000"
    task.created_at = NOW
    db.flush()
    assert run_due_renewal_scan(db, sch, NOW) == 0


def test_due_renewal_without_phone_is_skipped(db):
    db.add(BusinessService(service_number="S1", customer_name="甲", account_manager_name="经理", agreement_expires_at=NOW + timedelta(days=1)))
    db.flush(); sch = schedule(db, "due_renewal")
    assert run_due_renewal_scan(db, sch, NOW) == 0


def test_device_recycle_uses_device_maintenance_snapshot(db):
    service = BusinessService(service_number="S1", customer_name="甲", agreement_expires_at=NOW - timedelta(days=1))
    db.add(service); db.flush()
    device = NetworkDevice(business_service_id=service.id, device_code="D1", maintenance_name="维护", maintenance_phone="13900000000")
    db.add(device); db.flush(); sch = schedule(db, "device_recycle")
    assert run_device_recycle_scan(db, sch, NOW) == 1
    task = db.scalars(select(CallTask)).one()
    assert task.dial_number == "13900000000" and task.customer_name == "甲"
