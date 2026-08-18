"""调度流程的临时 SQLite 测试：到期入队、暂停调度、立即拨打、重启恢复。

覆盖 TODO「P1：测试与可靠性」：
- 到期计划入队（once 关闭、cron 推进下一次执行时间、按 due_at 排序）；
- 暂停调度时（is_scheduler_enabled 为 False）tick 不生成任务；
- 立即拨打（create_call_task 手动路径）；
- 重启恢复（mark_missed_once_plans：once 记为 missed，cron 不补打）。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import CallEvent, CallRecord, CallTask, CallbackPlan, Customer, Script, utcnow
from app.services import plans as plan_service
from app.services.customers import sync_default_contact
from app.services.settings import (
    SCHEDULER_ENABLED_KEY,
    is_scheduler_enabled,
    set_setting,
)

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture()
def engine(tmp_path: Path):
    db_path = tmp_path / "scheduler.db"
    url = f"sqlite:///{db_path}"
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(engine):
    session = Session(engine)
    yield session
    session.close()


def norm(value):
    """SQLite 读回的 datetime 不带时区，统一按 UTC 归一化后再比较。"""
    return plan_service.as_utc(value)


def make_customer(db: Session, name: str = "客户A") -> Customer:
    customer = Customer(name=name)
    db.add(customer)
    db.flush()
    sync_default_contact(db, customer, "13800000000")
    db.commit()
    return customer


def make_plan(
    db: Session,
    customer: Customer,
    *,
    trigger_type: str = "once",
    cron_expr: str = "",
    next_run_at,
    enabled: bool = True,
) -> CallbackPlan:
    script = Script(title="话术", body="内容")
    db.add(script)
    db.flush()
    plan = CallbackPlan(
        customer=customer,
        script=script,
        trigger_type=trigger_type,
        cron_expr=cron_expr,
        timezone="Asia/Shanghai",
        enabled=enabled,
        next_run_at=next_run_at,
    )
    db.add(plan)
    db.commit()
    return plan


def queued_tasks(db: Session) -> list[CallTask]:
    return db.scalars(
        select(CallTask)
        .where(CallTask.status == "queued")
        .order_by(CallTask.due_at.asc(), CallTask.created_at.asc())
    ).all()


def task_count(db: Session) -> int:
    return db.scalar(select(func.count(CallTask.id))) or 0


# ---------------------------------------------------------------- 到期入队


def test_enqueue_due_once_plan_creates_queued_task(db: Session) -> None:
    customer = make_customer(db)
    due = utcnow() - timedelta(minutes=5)
    plan = make_plan(db, customer, next_run_at=due)

    assert plan_service.enqueue_due_plans(db) == 1
    db.commit()

    tasks = queued_tasks(db)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.plan_id == plan.id
    assert task.status == "queued"
    assert task.source == "scheduled"
    assert norm(task.due_at) == due
    assert task.dial_number == "13800000000"

    # once 计划入队后关闭并清空下一次执行时间，避免重复生成。
    db.expire_all()
    plan = db.get(CallbackPlan, plan.id)
    assert plan.enabled is False
    assert plan.next_run_at is None

    # 通话记录与事件一并落库。
    record = db.scalar(select(CallRecord).where(CallRecord.task_id == task.id))
    assert record is not None
    assert record.status == "queued"
    assert record.dial_number == "13800000000"
    assert db.scalar(select(CallEvent).where(CallEvent.call_record_id == record.id)) is not None


def test_enqueue_due_plans_is_idempotent_for_disabled_once(db: Session) -> None:
    customer = make_customer(db)
    plan = make_plan(db, customer, next_run_at=utcnow() - timedelta(minutes=5))

    assert plan_service.enqueue_due_plans(db) == 1
    assert plan_service.enqueue_due_plans(db) == 0
    db.commit()
    assert task_count(db) == 1


def test_enqueue_cron_plan_advances_next_run(db: Session) -> None:
    customer = make_customer(db)
    plan = make_plan(
        db,
        customer,
        trigger_type="cron",
        cron_expr="0 9 * * *",
        next_run_at=utcnow() - timedelta(minutes=1),
    )

    assert plan_service.enqueue_due_plans(db) == 1
    db.commit()

    db.expire_all()
    plan = db.get(CallbackPlan, plan.id)
    # cron 计划保持启用，下一次执行时间推进到未来。
    assert plan.enabled is True
    assert plan.next_run_at is not None
    assert norm(plan.next_run_at) > utcnow()
    assert len(queued_tasks(db)) == 1


def test_enqueue_skips_future_and_disabled_plans(db: Session) -> None:
    customer = make_customer(db)
    make_plan(db, customer, next_run_at=utcnow() + timedelta(days=1))
    make_plan(db, customer, next_run_at=utcnow() - timedelta(minutes=1), enabled=False)

    assert plan_service.enqueue_due_plans(db) == 0
    db.commit()
    assert queued_tasks(db) == []


def test_enqueue_orders_tasks_by_due_at(db: Session) -> None:
    customer = make_customer(db)
    earlier = utcnow() - timedelta(hours=2)
    later = utcnow() - timedelta(hours=1)
    plan_a = make_plan(db, customer, next_run_at=earlier)
    plan_b = make_plan(db, customer, next_run_at=later)

    plan_service.enqueue_due_plans(db)
    db.commit()

    tasks = queued_tasks(db)
    assert [task.plan_id for task in tasks] == [plan_a.id, plan_b.id]


# ---------------------------------------------------------------- 暂停调度


def test_scheduler_tick_respects_pause_setting(engine, db: Session, monkeypatch) -> None:
    """调度器 tick 在暂停（is_scheduler_enabled=False）时不生成任务。"""

    import app.scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True),
    )

    customer = make_customer(db)
    make_plan(db, customer, next_run_at=utcnow() - timedelta(minutes=1))

    # 暂停状态：tick 扫描到期计划但不入队。
    set_setting(db, SCHEDULER_ENABLED_KEY, "0")
    db.commit()
    assert is_scheduler_enabled(db) is False
    scheduler_module.scheduler_service.tick()
    db.expire_all()
    assert queued_tasks(db) == []

    # 恢复后：tick 正常入队。
    set_setting(db, SCHEDULER_ENABLED_KEY, "1")
    db.commit()
    assert is_scheduler_enabled(db) is True
    scheduler_module.scheduler_service.tick()
    db.expire_all()
    assert len(queued_tasks(db)) == 1


# ---------------------------------------------------------------- 立即拨打


def test_manual_call_now_creates_queued_task_and_record(db: Session) -> None:
    customer = make_customer(db)
    plan = make_plan(db, customer, next_run_at=utcnow() + timedelta(days=1))
    before_next_run = plan.next_run_at

    task = plan_service.create_call_task(
        db,
        plan,
        due_at=utcnow(),
        status="queued",
        message="网页端发起立即拨打。",
        source="manual",
    )
    db.commit()

    assert task.source == "manual"
    assert task.status == "queued"
    assert task.dial_number == "13800000000"
    assert task.plan_id == plan.id
    record = task.call_record
    assert record is not None
    assert record.status == "queued"
    assert record.dial_number == "13800000000"
    event = db.scalar(select(CallEvent).where(CallEvent.call_record_id == record.id))
    assert event is not None
    assert event.message == "网页端发起立即拨打。"

    # 手动拨打不改变计划的下一次执行时间，也不影响计划启用状态。
    assert plan.next_run_at == before_next_run
    assert plan.enabled is True


# ---------------------------------------------------------------- 重启恢复


def test_mark_missed_once_plan_on_restart(db: Session) -> None:
    customer = make_customer(db)
    due = utcnow() - timedelta(hours=3)
    plan = make_plan(db, customer, next_run_at=due)

    assert plan_service.mark_missed_once_plans(db) == 1
    db.commit()

    db.expire_all()
    plan = db.get(CallbackPlan, plan.id)
    assert plan.enabled is False
    assert plan.next_run_at is None
    task = db.scalar(select(CallTask).where(CallTask.plan_id == plan.id))
    assert task is not None
    assert task.status == "missed"
    assert norm(task.due_at) == due
    record = db.scalar(select(CallRecord).where(CallRecord.task_id == task.id))
    assert record.status == "missed"


def test_mark_missed_skips_future_once_plans(db: Session) -> None:
    customer = make_customer(db)
    plan = make_plan(db, customer, next_run_at=utcnow() + timedelta(hours=3))

    assert plan_service.mark_missed_once_plans(db) == 0
    db.commit()

    db.expire_all()
    plan = db.get(CallbackPlan, plan.id)
    assert plan.enabled is True
    assert plan.next_run_at is not None
    assert task_count(db) == 0


def test_mark_missed_skips_disabled_plans(db: Session) -> None:
    customer = make_customer(db)
    make_plan(db, customer, next_run_at=utcnow() - timedelta(hours=1), enabled=False)

    assert plan_service.mark_missed_once_plans(db) == 0
    db.commit()
    assert task_count(db) == 0


def test_cron_plans_are_not_backfilled_on_restart(db: Session) -> None:
    """重启不补打停机期间错过的 cron 次数：不生成 missed 任务、不推进计划。"""

    customer = make_customer(db)
    past = utcnow() - timedelta(days=2)
    plan = make_plan(
        db,
        customer,
        trigger_type="cron",
        cron_expr="0 9 * * *",
        next_run_at=past,
    )

    assert plan_service.mark_missed_once_plans(db) == 0
    db.commit()

    db.expire_all()
    plan = db.get(CallbackPlan, plan.id)
    assert plan.enabled is True
    assert norm(plan.next_run_at) == past  # 留给下一个正常 tick 推进
    assert task_count(db) == 0


def test_mark_missed_is_idempotent(db: Session) -> None:
    customer = make_customer(db)
    make_plan(db, customer, next_run_at=utcnow() - timedelta(hours=1))

    assert plan_service.mark_missed_once_plans(db) == 1
    db.commit()
    assert plan_service.mark_missed_once_plans(db) == 0
    db.commit()
    assert task_count(db) == 1
