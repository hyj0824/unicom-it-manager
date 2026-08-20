from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import CallRecord, CallTask, Script
from app.services.call_worker import CallWorker
from tests.flat_helpers import make_session


@pytest.fixture()
def db(tmp_path):
    session = make_session(tmp_path / "worker.db")
    yield session
    session.close()


def settings():
    return Settings(admin_password="test", session_secret="test", database_url="sqlite:///:memory:", modem_port="/dev/null", modem_baud=115200, audio_device="default", call_connect_timeout_seconds=90, rejected_end_seconds=20, min_connected_seconds=8, retry_delay_seconds=1, max_call_attempts=2, tts_provider="none", tts_api_key="", tts_voice="", default_timezone="Asia/Shanghai", call_worker_enabled=False, worker_poll_seconds=1)


def test_claim_creates_record_with_customer_name_snapshot(db):
    script = Script(title="t", body="b"); db.add(script); db.flush()
    task = CallTask(customer_name="客户甲", script_id=script.id, dial_number="13800000000", due_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    db.add(task); db.commit()
    claimed = CallWorker(settings()).claim_next_task(db)
    assert claimed is task and task.status == "dialing"
    assert task.call_record.customer_name == "客户甲"


def test_claim_orders_due_tasks(db):
    script = Script(title="t", body="b"); db.add(script); db.flush()
    for name, offset in (("late", 2), ("early", 1)):
        db.add(CallTask(customer_name=name, script_id=script.id, dial_number="13800000000", due_at=datetime.now(timezone.utc) - timedelta(seconds=offset)))
    db.commit(); task = CallWorker(settings()).claim_next_task(db)
    assert task.customer_name == "late"
