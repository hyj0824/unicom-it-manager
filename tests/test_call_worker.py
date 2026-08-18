from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.audio import PlaybackResult
from app.config import Settings
from app.models import AppSetting, CallEvent, CallTask, Customer, Script, utcnow
from app.services import plans as plan_service
from app.services.call_worker import CallWorker, CallWorkerService
from app.services.settings import (
    CALL_WORKER_ENABLED_KEY,
    ensure_default_settings,
    is_worker_enabled,
    set_setting,
)

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db(tmp_path: Path):
    db_path = tmp_path / "worker.db"
    url = f"sqlite:///{db_path}"
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def fake_modem(monkeypatch):
    """替换串口与 ffplay：worker 只与内存中的假串口交互，不真实播放。"""

    from app.services import call_worker as worker_module

    holder = {"lines": [], "play_success": True, "instances": []}

    class FakeModem:
        def __init__(self, *args, **kwargs):
            self.lines = list(holder["lines"])
            self.commands = []
            holder["instances"].append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def dial(self, phone: str) -> None:
            self.commands.append(f"ATD{phone};")

        def hangup(self) -> None:
            self.commands.append("AT+CHUP")

        def read_line(self) -> str:
            if self.lines:
                return self.lines.pop(0)
            time.sleep(0.005)
            return ""

    monkeypatch.setattr(worker_module, "ModemClient", FakeModem)

    def configure(lines=(), play_success: bool = True) -> None:
        holder["lines"] = list(lines)
        holder["play_success"] = play_success
        monkeypatch.setattr(
            worker_module,
            "play_audio",
            lambda path, dev: PlaybackResult(
                play_success, 0 if play_success else 1, "" if play_success else "playback failed"
            ),
        )

    configure()

    class Context:
        @property
        def modem(self):
            return holder["instances"][-1] if holder["instances"] else None

    return Context(), configure


def _settings(**overrides) -> Settings:
    base = dict(
        admin_password="test",
        session_secret="test",
        database_url="sqlite:///:memory:",
        modem_port="/dev/ttyFAKE",
        modem_baud=115200,
        audio_device="plughw:0,0",
        call_connect_timeout_seconds=1,
        rejected_end_seconds=20,
        min_connected_seconds=3,
        retry_delay_seconds=60,
        max_call_attempts=2,
        tts_provider="none",
        tts_api_key="",
        tts_voice="",
        default_timezone="Asia/Shanghai",
        call_worker_enabled=False,
        worker_poll_seconds=1,
    )
    base.update(overrides)
    return Settings(**base)


def _make_task(
    db: Session,
    tmp_path: Path,
    phone: str = "13800000000",
    wav: bool = True,
    due_at=None,
) -> CallTask:
    customer = Customer(name="测试客户")
    db.add(customer)
    script = Script(title="话术", body="正文")
    if wav:
        wav_path = tmp_path / "audio.wav"
        wav_path.write_bytes(b"RIFF")
        script.wav_path = str(wav_path)
    db.add(script)
    db.flush()
    plan = plan_service.create_plan(db, customer, script, "once", utcnow(), "", "Asia/Shanghai", True)
    task = plan_service.create_call_task(db, plan, due_at=due_at, status="queued")
    task.dial_number = phone
    db.commit()
    return task


def _event_types(record) -> list[str]:
    return [event.event_type for event in record.events]


# ---------------------------------------------------------------- 成功与短通话


def test_completed_call(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    configure(lines=["OK", "VOICE CALL: BEGIN", "VOICE CALL: END: 000012"])

    worker = CallWorker(_settings())
    assert worker.run_one_pending(db) is task
    db.commit()
    db.refresh(task)

    assert task.status == "completed"
    assert task.completed_at is not None
    record = task.call_record
    assert record.status == "completed"
    assert record.duration_seconds == 12
    assert record.connected_at is not None
    assert ctx.modem.commands[0] == "ATD13800000000;"
    types = _event_types(record)
    assert "at_command" in types
    assert "connected" in types
    assert "audio_start" in types
    assert "audio_end" in types
    assert any(e.event_type == "voice_call_end" and e.raw_line == "VOICE CALL: END: 000012" for e in record.events)


def test_short_call_below_min_connected(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    configure(lines=["VOICE CALL: BEGIN", "VOICE CALL: END: 000002"])

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    assert task.status == "short_call"
    assert task.call_record.duration_seconds == 2


# ---------------------------------------------------------------- 未接通分类


def test_busy_schedules_retry(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    configure(lines=["BUSY"])

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    # busy 可重试：attempt 1 -> 2，任务回到队列并延迟 due_at。
    assert task.status == "queued"
    assert task.attempt == 2
    # SQLite 读回的 datetime 无时区，按 UTC 处理。
    assert task.due_at > datetime.now(timezone.utc).replace(tzinfo=None)
    assert any(e.event_type == "retry_scheduled" for e in task.call_record.events)


def test_retry_exhausted_finalizes(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    task.attempt = 2  # 模拟第一次重试已用完
    db.commit()
    configure(lines=["BUSY"])

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    assert task.status == "busy"
    assert task.completed_at is not None


def test_short_end_without_begin_is_rejected(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    configure(lines=["VOICE CALL: END: 000005"])

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    assert task.status == "rejected"
    assert task.call_record.duration_seconds == 5
    # 疑似拒接不自动重拨。
    assert task.attempt == 1


def test_15s_end_without_begin_is_rejected_not_retried(fake_modem, db, tmp_path) -> None:
    """拒接阈值 20s：响铃 15s 内被释放视为主动拒接，不自动重拨。"""
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    configure(lines=["VOICE CALL: END: 000015"])

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    assert task.status == "rejected"
    assert task.attempt == 1  # 不重试


def test_40s_end_without_begin_is_cancelled_or_failed(fake_modem, db, tmp_path) -> None:
    """超过拒接阈值（20s）的未接通释放视为无人接听，可重试。"""
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    configure(lines=["VOICE CALL: END: 000040"])

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    assert task.status == "queued"  # 可重试
    assert task.attempt == 2


def test_connect_timeout_is_no_answer(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    task.max_attempts = 1  # create_call_task 的 max_attempts 来自全局配置，这里直接置为 1
    db.commit()
    configure(lines=[])  # 串口无任何上报，走应用层超时兜底
    ctx.modem  # noqa: B018 - 确保假串口已实例化

    worker = CallWorker(_settings(max_call_attempts=1))
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    assert task.status == "no_answer"
    # 超时兜底后应执行 AT+CHUP 清理。
    assert "AT+CHUP" in ctx.modem.commands


# ---------------------------------------------------------------- 失败路径


def test_missing_dial_number_fails_permanently(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path, phone="")

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    assert task.status == "failed"
    assert task.attempt == 1  # 配置错误不重试
    assert "no dial number" in task.error_message


def test_missing_wav_fails_permanently(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path, wav=False)

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    assert task.status == "failed"
    assert "WAV" in task.error_message


def test_play_failure_fails_without_retry(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    task = _make_task(db, tmp_path)
    configure(lines=["VOICE CALL: BEGIN"], play_success=False)

    worker = CallWorker(_settings())
    worker.run_one_pending(db)
    db.commit()
    db.refresh(task)

    # 已接通并触达客户，音频失败不自动重拨。
    assert task.status == "failed"
    assert task.attempt == 1
    assert "AT+CHUP" in ctx.modem.commands


# ---------------------------------------------------------------- 队列与设置


def test_claim_picks_earliest_due_task(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    from datetime import timedelta

    later = _make_task(db, tmp_path, due_at=utcnow() + timedelta(minutes=10))
    earlier = _make_task(db, tmp_path, due_at=utcnow())

    worker = CallWorker(_settings())
    assert worker.claim_next_task(db) is earlier
    db.commit()

    db.refresh(earlier)
    db.refresh(later)
    assert earlier.status == "dialing"
    assert later.status == "queued"


def test_future_due_task_is_not_claimed_until_due(fake_modem, db, tmp_path) -> None:
    """重试任务按 `RETRY_DELAY_SECONDS` 延后 `due_at`，到期前不得提前领取。"""
    ctx, configure = fake_modem
    from datetime import timedelta

    future = _make_task(db, tmp_path, due_at=utcnow() + timedelta(seconds=300))

    worker = CallWorker(_settings())
    # 未到期：不领取，也不改变状态。
    assert worker.claim_next_task(db) is None
    db.commit()
    db.refresh(future)
    assert future.status == "queued"
    assert future.started_at is None

    # 到期后：正常领取并置为 dialing。
    future.due_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert worker.claim_next_task(db) is future
    db.commit()
    db.refresh(future)
    assert future.status == "dialing"


def test_recover_interrupted_tasks_finalizes_stale_calls(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    dialing = _make_task(db, tmp_path)
    connected = _make_task(db, tmp_path, phone="13900000001")
    dialing.status = "dialing"
    dialing.call_record.status = "dialing"
    connected.status = "connected"
    connected.call_record.status = "connected"
    db.commit()

    service = CallWorkerService(_settings(call_worker_enabled=True))
    assert service.recover_interrupted_tasks(db) == 2
    db.commit()
    db.refresh(dialing)
    db.refresh(connected)

    for task in (dialing, connected):
        assert task.status == "failed"
        assert task.completed_at is not None
        assert task.call_record.status == "failed"
        assert "进程重启中断，未完成释放" in task.error_message
        assert any(event.event_type == "recovery" for event in task.call_record.events)
        assert any(event.event_type == "hangup" for event in task.call_record.events)
        assert any(event.event_type == "failed" for event in task.call_record.events)
    assert ctx.modem.commands == ["AT+CHUP"]


def test_worker_tick_recovers_before_claiming_new_task(fake_modem, db, tmp_path, monkeypatch) -> None:
    ctx, configure = fake_modem
    stale = _make_task(db, tmp_path)
    stale.status = "connected"
    stale.call_record.status = "connected"
    db.commit()

    service = CallWorkerService(_settings(call_worker_enabled=True))
    observed_statuses = []

    def claim_after_recovery(session):
        observed_statuses.append(stale.status)
        return None

    monkeypatch.setattr(service.worker, "claim_next_task", claim_after_recovery)
    service._tick(db)
    db.refresh(stale)

    assert stale.status == "failed"
    assert observed_statuses == ["failed"]
    assert ctx.modem.commands == ["AT+CHUP"]


def test_events_stamped_at_creation_not_flush(db, tmp_path) -> None:
    """CallEvent 在创建时盖章：同一批 flush 的事件保留各自的创建时间。"""
    task = _make_task(db, tmp_path)
    record = task.call_record
    e1 = CallEvent(call_record=record, event_type="a")
    time.sleep(0.02)
    e2 = CallEvent(call_record=record, event_type="b")
    db.add_all([e1, e2])
    db.flush()  # 同一批 flush，不能把两者盖成同一个时间戳。
    assert e2.created_at > e1.created_at
    assert (e2.created_at - e1.created_at).total_seconds() >= 0.015


def test_no_queued_task_returns_none(fake_modem, db) -> None:
    ctx, configure = fake_modem
    worker = CallWorker(_settings())
    assert worker.run_one_pending(db) is None


def test_worker_setting_round_trip(db) -> None:
    ensure_default_settings(db)
    db.commit()
    assert db.get(AppSetting, CALL_WORKER_ENABLED_KEY) is not None
    assert not is_worker_enabled(db)
    set_setting(db, CALL_WORKER_ENABLED_KEY, "1")
    db.commit()
    assert is_worker_enabled(db)
