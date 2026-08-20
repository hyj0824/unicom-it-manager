from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import BusinessService, NetworkDevice
from app.services.change_requests import (
    RETIRE_STATUS_LABEL, business_snapshot_patch, device_snapshot_patch,
    list_due_renewal_rows, list_recycle_device_rows, submit_business_update,
    submit_device_recovery,
)
from app.services.dictionaries import resolve_or_create_item
from app.services.reviews import apply_change_set
from tests.flat_helpers import make_session

NOW = datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path):
    session = make_session(tmp_path / "changes.db")
    yield session
    session.close()


def service(db, retired=False):
    item = resolve_or_create_item(db, "service_status", RETIRE_STATUS_LABEL if retired else "正常开机")
    obj = BusinessService(service_number="S1", customer_name="客户", account_manager_name="经理", account_manager_phone="13800000000", service_status_item=item, agreement_expires_at=NOW + timedelta(days=2))
    db.add(obj); db.flush(); return obj


def test_business_snapshot_contains_flat_fields(db):
    obj = service(db)
    patch = business_snapshot_patch(db, obj)
    assert patch["customer_name"] == "客户"
    assert patch["account_manager_phone"] == "13800000000"
    assert "contacts" not in patch


def test_submit_and_apply_renewal_uses_flat_business(db):
    obj = service(db)
    change = submit_business_update(db, obj, {"agreement_expires_at": "2030-01-01"}, "续签", 1)
    change.status = "approved"; assert apply_change_set(db, change, 2) == 1
    db.commit(); assert obj.agreement_expires_at is not None


def test_device_snapshot_and_recovery(db):
    obj = service(db, retired=True)
    device = NetworkDevice(business_service_id=obj.id, device_code="D1", maintenance_name="维护", maintenance_phone="13900000000")
    db.add(device); db.flush()
    assert device_snapshot_patch(db, device)["device"]["maintenance_phone"] == "13900000000"
    change = submit_device_recovery(db, device, "回收", 1)
    change.status = "approved"; assert apply_change_set(db, change, 2) == 1


def test_workbench_rows_read_flat_names(db):
    obj = service(db)
    assert list_due_renewal_rows(db, NOW)[0]["account_manager"] == "经理"
    obj.agreement_expires_at = NOW - timedelta(days=1); db.flush()
    device = NetworkDevice(business_service_id=obj.id, device_code="D2", maintenance_name="维护", maintenance_phone="13900000000")
    db.add(device); db.flush()
    assert list_recycle_device_rows(db, NOW)[0]["maintenance_name"] == "维护"
