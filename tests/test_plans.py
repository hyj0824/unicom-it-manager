from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import CallbackPlan, CallTask, Customer, Script
from app.services.plans import (
    advance_overdue_cron_plans,
    as_utc,
    create_plan,
    enqueue_due_plans,
    mark_missed_once_plans,
)


BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db(tmp_path: Path):
    db_path = tmp_path / "plans.db"
    url = f"sqlite:///{db_path}"
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def _plan(db: Session, trigger_type: str, next_run_at: datetime, cron_expr: str = ""):
    customer = Customer(name="计划客户")
    script = Script(title="计划话术", body="内容")
    db.add_all([customer, script])
    db.flush()
    plan = create_plan(
        db,
        customer,
        script,
        trigger_type,
        next_run_at if trigger_type == "once" else None,
        cron_expr,
        "UTC",
        True,
    )
    plan.next_run_at = next_run_at
    db.commit()
    return plan


def test_mark_missed_once_plans_creates_traceable_missed_task(db: Session) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc)
    plan = _plan(db, "once", now - timedelta(minutes=1))

    assert mark_missed_once_plans(db, now=now) == 1
    db.commit()

    db.refresh(plan)
    assert not plan.enabled
    assert plan.next_run_at is None
    task = db.scalars(select(CallTask).where(CallTask.plan_id == plan.id)).one()
    assert task.status == "missed"
    assert task.call_record.status == "missed"
    assert task.call_record.events[0].event_type == "missed"
    assert "missed" in task.call_record.events[0].message.lower()


def test_enqueue_due_once_plan_runs_once(db: Session) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc)
    plan = _plan(db, "once", now)

    assert enqueue_due_plans(db, now=now) == 1
    db.commit()

    db.refresh(plan)
    assert not plan.enabled
    assert plan.next_run_at is None
    task = db.scalars(select(CallTask).where(CallTask.plan_id == plan.id)).one()
    assert task.status == "queued"


def test_startup_advances_overdue_cron_without_backfill(db: Session) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc)
    plan = _plan(db, "cron", now - timedelta(days=2), "0 * * * *")

    assert advance_overdue_cron_plans(db, now=now) == 1
    db.commit()
    db.refresh(plan)

    assert as_utc(plan.next_run_at) > now
    assert enqueue_due_plans(db, now=now) == 0
    assert db.scalars(select(CallTask).where(CallTask.plan_id == plan.id)).all() == []


def test_enqueue_due_cron_plan_only_creates_current_occurrence(db: Session) -> None:
    now = datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc)
    plan = _plan(db, "cron", now - timedelta(minutes=1), "0 * * * *")

    assert enqueue_due_plans(db, now=now) == 1
    db.commit()
    db.refresh(plan)

    assert as_utc(plan.next_run_at) > now
    tasks = db.scalars(select(CallTask).where(CallTask.plan_id == plan.id)).all()
    assert len(tasks) == 1
