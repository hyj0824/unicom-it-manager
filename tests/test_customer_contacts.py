from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import CallRecord, CallTask, Contact, Customer, CustomerContact, Script, utcnow
from app.services.customers import (
    default_contact,
    sync_default_contact,
)
from app.services.plans import create_manual_call_task

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


def make_customer(db: Session, name: str = "客户A", phone: str = "13800000000") -> Customer:
    customer = Customer(name=name)
    db.add(customer)
    db.flush()
    sync_default_contact(db, customer, phone)
    db.commit()
    return customer


def make_script(db: Session) -> Script:
    script = Script(title="话术", body="内容")
    db.add(script)
    db.flush()
    return script


def test_sync_default_contact_creates_then_updates(db: Session) -> None:
    customer = Customer(name="客户A")
    db.add(customer)
    db.flush()
    sync_default_contact(db, customer, "13800000000")
    db.commit()

    links = db.query(CustomerContact).filter_by(customer_id=customer.id).all()
    assert len(links) == 1
    assert links[0].contact.phone == "13800000000"
    assert links[0].contact.name == "客户A"

    sync_default_contact(db, customer, "13900000001")
    db.commit()
    assert db.query(CustomerContact).filter_by(customer_id=customer.id).count() == 1
    assert default_contact(db, customer).phone == "13900000001"


def test_default_contact_prefers_default_duty_and_active(db: Session) -> None:
    customer = Customer(name="客户A")
    db.add(customer)
    db.flush()
    contact_a = Contact(name="甲", phone="13800000000")
    contact_b = Contact(name="乙", phone="13900000001")
    db.add_all([contact_a, contact_b])
    db.add(CustomerContact(customer=customer, contact=contact_a, duty=""))
    db.add(CustomerContact(customer=customer, contact=contact_b, duty="客户经理"))
    inactive = Contact(name="丙", phone="13700000002")
    db.add(inactive)
    db.add(
        CustomerContact(
            customer=customer, contact=inactive, duty="", is_active=False
        )
    )
    db.commit()

    assert default_contact(db, customer).id == contact_a.id


def test_create_manual_call_task_snapshots_contact_and_number(db: Session) -> None:
    customer = make_customer(db)
    script = make_script(db)

    task = create_manual_call_task(db, customer, script, source="manual")
    db.commit()

    assert task.source == "manual"
    assert task.dial_number == "13800000000"
    assert task.contact is not None
    record = task.call_record
    assert record is not None
    assert record.dial_number == "13800000000"
    assert record.contact_id == task.contact_id

    # 入队后联系人电话变更不影响已入队任务的拨号快照。
    contact = default_contact(db, customer)
    contact.phone = "13700000003"
    db.commit()
    assert task.dial_number == "13800000000"


def test_manual_call_task_without_contact_raises(db: Session) -> None:
    customer = Customer(name="无联系人客户")
    db.add(customer)
    db.commit()
    script = make_script(db)

    with pytest.raises(ValueError, match="没有可拨打的电话"):
        create_manual_call_task(db, customer, script)


def test_task_record_snapshots_empty_number_when_no_contact(db: Session) -> None:
    """扫描任务允许拨号号码为空（Worker 领取后按错误收尾）；快照语义在此固化。"""

    customer = Customer(name="无联系人客户")
    db.add(customer)
    db.flush()
    script = make_script(db)
    task = CallTask(
        customer=customer,
        script=script,
        due_at=utcnow(),
        status="queued",
        source="due_renewal",
        dial_number="",
    )
    db.add(task)
    db.flush()
    record = CallRecord(
        task=task,
        customer=customer,
        script=script,
        status="queued",
        dial_number=task.dial_number,
    )
    db.add(record)
    db.commit()

    assert task.contact is None
    assert task.dial_number == ""
    assert record.dial_number == ""
