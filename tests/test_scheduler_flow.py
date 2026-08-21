"""调度器扫描触发流程的临时 SQLite 测试。

覆盖「运营支撑助手」定位下的扫描通知调度：
- 遍历 enabled 的 scan_schedules，按各自时区判断 cron 是否匹配当前分钟；
- 匹配时调用 app.services.scans.run_scan_for_schedule（契约：返回生成任务数），
  测试用注入的假模块替换，不实现、不依赖真实扫描逻辑；
- 不匹配、enabled=False、全局调度暂停时均不调用；
- 成功更新 last_run_at / 清空 last_error；失败记录 last_error 且不影响其他配置。
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select, create_engine
from sqlalchemy.orm import Session, sessionmaker
from app.models import ScanSchedule
from app.services.plans import as_utc
from app.services.settings import (
    SCHEDULER_ENABLED_KEY,
    is_scheduler_enabled,
    set_setting,
)

BASE_DIR = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def norm(value: datetime | None) -> datetime | None:
    """SQLite 读回的 datetime 不带时区，统一按 UTC 归一化后再比较。"""
    return as_utc(value)


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


@pytest.fixture()
def fake_scans(monkeypatch):
    """把 app.services.scans 替换为带 run_scan_for_schedule 假实现的模块。

    无论真实 scans 模块是否已落地（另一工作流并行开发），tick 的延迟导入
    都会解析到这个假模块，测试行为确定。调用时先记录 (schedule_id, name)
    再返回 0，避免 tick 会话关闭后访问过期对象。
    """
    module = types.ModuleType("app.services.scans")
    calls: list[tuple[int, str]] = []

    def run_scan_for_schedule(db, schedule) -> int:
        calls.append((schedule.id, schedule.name))
        return 0

    module.calls = calls
    module.run_scan_for_schedule = MagicMock(side_effect=run_scan_for_schedule)
    monkeypatch.setitem(sys.modules, "app.services.scans", module)
    return module


def make_schedule(
    db: Session,
    *,
    name: str = "到期维系扫描",
    scan_type: str = "due_renewal",
    cron_expr: str = "0 9 * * *",
    timezone_name: str = "Asia/Shanghai",
    enabled: bool = True,
) -> ScanSchedule:
    schedule = db.scalars(select(ScanSchedule).where(ScanSchedule.scan_type == scan_type)).first()
    if schedule is None:
        schedule = ScanSchedule(name=name, scan_type=scan_type)
        db.add(schedule)
    schedule.name = name
    schedule.cron_expr = cron_expr
    schedule.timezone = timezone_name
    schedule.lead_days = 14
    schedule.enabled = enabled
    schedule.sms_enabled = False
    # 每类型只有一个固定配置：测试只保留目标类型启用，其余类型禁用。
    for other in db.scalars(select(ScanSchedule).where(ScanSchedule.scan_type != scan_type)).all():
        other.enabled = False
    db.commit()
    return schedule


@pytest.fixture()
def scheduler(engine, monkeypatch):
    import app.scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True),
    )
    # 固定「当前时刻」：上海 2026-01-15 09:00:30（cron "0 9 * * *" 应匹配）。
    fixed_now = datetime(2026, 1, 15, 1, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(scheduler_module, "utcnow", lambda: fixed_now)
    return scheduler_module, fixed_now


# ---------------------------------------------------------------- 匹配触发


def test_tick_runs_scan_when_cron_matches(db: Session, fake_scans, scheduler) -> None:
    scheduler_module, fixed_now = scheduler
    schedule = make_schedule(db)

    scheduler_module.scheduler_service.tick()

    fake_scans.run_scan_for_schedule.assert_called_once()
    assert fake_scans.calls == [(schedule.id, schedule.name)]
    db.expire_all()
    refreshed = db.get(ScanSchedule, schedule.id)
    assert norm(refreshed.last_run_at) == fixed_now
    assert refreshed.last_error == ""


def test_tick_skips_when_cron_does_not_match(db: Session, fake_scans, scheduler) -> None:
    scheduler_module, _ = scheduler
    schedule = make_schedule(db, cron_expr="0 18 * * *")  # 18:00，当前 09:00 不匹配

    scheduler_module.scheduler_service.tick()

    fake_scans.run_scan_for_schedule.assert_not_called()
    db.expire_all()
    assert db.get(ScanSchedule, schedule.id).last_run_at is None

def test_tick_skips_disabled_schedules(db: Session, fake_scans, scheduler) -> None:
    scheduler_module, _ = scheduler
    make_schedule(db, enabled=False)

    scheduler_module.scheduler_service.tick()

    fake_scans.run_scan_for_schedule.assert_not_called()


def test_tick_respects_each_schedule_timezone(db: Session, fake_scans, scheduler) -> None:
    scheduler_module, _ = scheduler
    # 当前 UTC 01:00：上海 09:00 匹配；纽约为前一日 20:00，不匹配。
    matching = make_schedule(db, name="上海扫描", scan_type="due_renewal", timezone_name="Asia/Shanghai")
    make_schedule(db, name="纽约扫描", scan_type="device_recycle", timezone_name="America/New_York")
    # make_schedule 互斥禁用其他类型；时区场景启用两条，review_stuck 保持停用。
    for row in db.scalars(select(ScanSchedule)).all():
        row.enabled = row.scan_type in ("due_renewal", "device_recycle")
    db.commit()

    scheduler_module.scheduler_service.tick()

    assert fake_scans.run_scan_for_schedule.call_count == 1
    assert fake_scans.calls == [(matching.id, matching.name)]


# ---------------------------------------------------------------- 暂停与失败处理


def test_tick_respects_global_pause_setting(db: Session, fake_scans, scheduler) -> None:
    scheduler_module, _ = scheduler
    make_schedule(db)

    set_setting(db, SCHEDULER_ENABLED_KEY, "0")
    db.commit()
    assert is_scheduler_enabled(db) is False

    scheduler_module.scheduler_service.tick()
    fake_scans.run_scan_for_schedule.assert_not_called()

    # 恢复后同一分钟仍会触发（扫描配置没有 next_run 状态，按当前分钟匹配）。
    set_setting(db, SCHEDULER_ENABLED_KEY, "1")
    db.commit()
    scheduler_module.scheduler_service.tick()
    fake_scans.run_scan_for_schedule.assert_called_once()


def test_tick_records_last_error_and_continues_with_other_schedules(
    db: Session, fake_scans, scheduler
) -> None:
    scheduler_module, _ = scheduler
    failing = make_schedule(db, name="会失败的扫描")
    healthy = make_schedule(db, name="正常扫描", scan_type="device_recycle", cron_expr="0 9 * * *")
    # 只启用这两条（make_schedule 会互斥禁用其他类型）。
    failing.enabled = True
    healthy.enabled = True
    db.commit()
    fake_scans.run_scan_for_schedule.side_effect = [RuntimeError("boom"), 3]

    scheduler_module.scheduler_service.tick()

    assert fake_scans.run_scan_for_schedule.call_count == 2
    db.expire_all()
    failed_row = db.get(ScanSchedule, failing.id)
    assert failed_row.last_error == "RuntimeError: boom"
    assert failed_row.last_run_at is None
    healthy_row = db.get(ScanSchedule, healthy.id)
    assert healthy_row.last_error == ""
    assert norm(healthy_row.last_run_at) is not None


def test_tick_records_invalid_cron_as_last_error_without_running(
    db: Session, fake_scans, scheduler
) -> None:
    scheduler_module, _ = scheduler
    schedule = make_schedule(db, cron_expr="not a cron")

    scheduler_module.scheduler_service.tick()

    fake_scans.run_scan_for_schedule.assert_not_called()
    db.expire_all()
    row = db.get(ScanSchedule, schedule.id)
    assert "ValueError" in row.last_error
    assert "无效" in row.last_error


def test_tick_returns_run_counts_are_ignored_but_recorded(db: Session, fake_scans, scheduler) -> None:
    """扫描实现返回生成任务数；调度器只记录运行时间与错误，不解读数量。"""
    scheduler_module, fixed_now = scheduler
    schedule = make_schedule(db)
    fake_scans.run_scan_for_schedule.side_effect = [5]

    scheduler_module.scheduler_service.tick()

    fake_scans.run_scan_for_schedule.assert_called_once()
    db.expire_all()
    row = db.get(ScanSchedule, schedule.id)
    assert norm(row.last_run_at) == fixed_now
    assert row.last_error == ""
