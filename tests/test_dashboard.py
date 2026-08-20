from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import BusinessService, NetworkDevice
from app.services.dashboard import dashboard_data
from tests.flat_helpers import make_session


@pytest.fixture()
def db(tmp_path):
    session = make_session(tmp_path / "dashboard.db")
    yield session
    session.close()


def test_dashboard_global_scope_uses_flat_fields(db):
    service = BusinessService(service_number="S1", customer_name="客户", account_manager_phone="13800000000", agreement_expires_at=datetime.now(timezone.utc) + timedelta(days=2))
    db.add(service); db.flush()
    db.add(NetworkDevice(business_service_id=service.id, device_code="D1", maintenance_name="维护", maintenance_phone="13900000000"))
    db.commit()
    request = SimpleNamespace(session={"principal": {"type": "admin", "name": "admin"}})
    data = dashboard_data(db, request)
    assert data["counts"]["services"] == 1
    assert data["counts"]["devices"] == 1
