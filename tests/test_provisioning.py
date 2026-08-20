import pytest
from sqlalchemy import func, select

from app.models import Role, User, UserRole
from app.services.provisioning import provision_users_from_business_payload, provision_users_from_device_payload
from tests.flat_helpers import make_session


@pytest.fixture()
def db(tmp_path):
    session = make_session(tmp_path / "provision.db")
    yield session
    session.close()


def role(db, user_id):
    rid = db.scalar(select(UserRole.role_id).where(UserRole.user_id == user_id))
    return db.scalar(select(Role.code).where(Role.id == rid))


def test_business_flat_fields_provision_manager(db):
    payload = {"customer_name": "客户", "account_manager_name": "经理", "account_manager_phone": "13900000000"}
    assert provision_users_from_business_payload(db, payload) == 1
    db.commit(); user = db.scalar(select(User).where(User.username == "13900000000"))
    assert user.real_name == "经理" and role(db, user.id) == "business_maintainer"


def test_device_flat_fields_provision_maintainer_and_idempotent(db):
    payload = {"device": {"maintenance_name": "维护", "maintenance_phone": "13800000000"}}
    assert provision_users_from_device_payload(db, payload) == 1
    assert provision_users_from_device_payload(db, payload) == 0
    db.commit(); assert db.scalar(select(func.count(User.id))) == 1


def test_missing_phone_skips(db):
    assert provision_users_from_business_payload(db, {"account_manager_name": "经理"}) == 0
    assert provision_users_from_device_payload(db, {"device": {"maintenance_name": "维护"}}) == 0
