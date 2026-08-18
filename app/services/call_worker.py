from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audio import play_wav
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..modem.client import ModemClient
from ..modem.parser import ParsedModemLine, parse_modem_line
from ..models import CallEvent, CallRecord, CallTask, utcnow
from .settings import (
    CALL_WORKER_ENABLED_KEY,
    ensure_default_settings,
    is_worker_enabled,
    set_setting,
)

# 可自动重试的结果；重试上限与延迟来自配置（docs/callback-demo-plan.md 重试策略）。
RETRYABLE_STATUSES = {"no_answer", "cancelled_or_failed", "busy", "failed"}

# 未接通粗分类阈值（秒），见 docs/callback-demo-plan.md「未接通粗分类」。
# 拒接阈值来自配置（REJECTED_END_SECONDS，默认 30）：响铃更久才视为无人接听。
NO_ANSWER_END_SECONDS = 80
# 播放结束后等待对端挂断/模块上报结束的兜底时长（秒）。
AFTER_PLAY_LINGER_SECONDS = 10

END_EVENT_TYPES = {"voice_call_end", "no_carrier"}


class CallWorker:
    """单通道任务消费者：领取任务 → 拨号 → 接通后播放 WAV → 分类收尾/重试。

    只有这里访问串口和 aplay；调用方负责事务提交。事件逐条写入
    `CallEvent`，串口原始行保存在 `raw_line`。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def run_one_pending(
        self,
        db: Session,
        commit=None,
        cancel_check=None,
    ) -> CallTask | None:
        """领取并处理一条任务；返回领取到的任务（可能被重试或收尾）。"""
        task = self.claim_next_task(db)
        if task is None:
            return None
        self.handle_task(db, task, commit=commit, cancel_check=cancel_check)
        return task

    def claim_next_task(self, db: Session) -> CallTask | None:
        """领取最早到期且 `queued` 的任务并置为 `dialing`（单通道：一次只领一条）。

        只领取 `due_at <= now` 的任务：重试任务会按 `RETRY_DELAY_SECONDS`
        延后 `due_at`，未到时间不得提前领取（调度器同样只扫到期计划）。
        """
        now = utcnow()
        task = db.scalars(
            select(CallTask)
            .where(
                CallTask.status == "queued",
                CallTask.due_at <= now,
            )
            .order_by(CallTask.due_at.asc(), CallTask.created_at.asc())
            .limit(1)
        ).first()
        if task is None:
            return None

        now = utcnow()
        task.status = "dialing"
        task.started_at = now
        record = task.call_record or CallRecord(
            task=task,
            plan=task.plan,
            customer=task.customer,
            script=task.script,
            contact=task.contact,
            dial_number=task.dial_number,
            status="dialing",
        )
        record.status = "dialing"
        record.dialing_started_at = now
        db.add(CallEvent(call_record=record, event_type="dialing", message="Worker claimed task."))
        return task

    def handle_task(
        self,
        db: Session,
        task: CallTask,
        commit=None,
        cancel_check=None,
    ) -> str:
        """处理一条已领取的任务；返回最终状态（`queued` 表示已安排重试）。"""
        record = task.call_record
        if record is None:
            record = CallRecord(
                task=task,
                plan=task.plan,
                customer=task.customer,
                script=task.script,
                contact=task.contact,
                dial_number=task.dial_number,
                status=task.status,
            )
            db.add(record)

        if not task.dial_number:
            self._permanent_fail(db, task, record, "Task has no dial number.")
            return "failed"
        wav_path = task.script.wav_path
        if not wav_path or not Path(wav_path).exists():
            self._permanent_fail(db, task, record, "Script has no playable WAV file.")
            return "failed"

        try:
            status, duration, error, retryable = self._dial_and_play(
                db, task, record, wav_path, commit=commit, cancel_check=cancel_check
            )
        except Exception as exc:  # noqa: BLE001 - 串口/播放异常要落库并分类收尾
            status, duration, error, retryable = "failed", None, str(exc), True

        if retryable and task.attempt < task.max_attempts:
            self._schedule_retry(db, task, record, status, error)
            return "queued"
        self._finalize(db, task, record, status, duration, error)
        return status

    # ---------------------------------------------------------------- 拨号与播放

    def _dial_and_play(
        self,
        db: Session,
        task: CallTask,
        record: CallRecord,
        wav_path: str,
        commit=None,
        cancel_check=None,
    ) -> tuple[str, int | None, str, bool]:
        """执行一次完整呼叫；返回 (最终状态, 时长秒, 错误信息, 是否可重试)。"""
        settings = self.settings
        dial_started_mono = time.monotonic()
        begin_mono: float | None = None
        connected = False
        busy = False
        end_event: ParsedModemLine | None = None

        with ModemClient(settings.modem_port, settings.modem_baud) as modem:
            db.add(
                CallEvent(
                    call_record=record,
                    event_type="at_command",
                    message=f"ATD{task.dial_number};",
                )
            )
            modem.dial(task.dial_number)

            # 等待接通：URC 主动上报，应用层超时兜底。
            while time.monotonic() - dial_started_mono < settings.call_connect_timeout_seconds:
                if cancel_check and cancel_check():
                    self._safe_hangup(db, record, modem)
                    elapsed = int(time.monotonic() - dial_started_mono)
                    return "cancelled_or_failed", elapsed, "Worker stopped during dial.", True
                line = modem.read_line()
                if not line:
                    continue
                parsed = parse_modem_line(line)
                db.add(
                    CallEvent(
                        call_record=record,
                        event_type=parsed.event_type,
                        message=line,
                        raw_line=line,
                    )
                )
                if parsed.event_type == "voice_call_begin":
                    connected = True
                    begin_mono = time.monotonic()
                    break
                if parsed.event_type in END_EVENT_TYPES:
                    end_event = parsed
                    break
                if parsed.event_type == "busy":
                    busy = True
                    break

            if busy:
                return "busy", None, "", True
            if not connected and end_event is None:
                # 应用层接通超时兜底：模块通常会上报结束，这里只防状态机卡死。
                self._safe_hangup(db, record, modem)
                return "no_answer", None, "Connect timeout.", True
            if not connected:
                return self._classify_unconnected(end_event, dial_started_mono)

            # 已接通：更新主状态，开始播放。
            now = utcnow()
            task.status = "connected"
            record.status = "connected"
            record.connected_at = now
            db.add(CallEvent(call_record=record, event_type="connected", message="VOICE CALL: BEGIN"))
            if commit:
                commit()

            db.add(
                CallEvent(
                    call_record=record,
                    event_type="audio_start",
                    message=f"Playing {wav_path} on {settings.audio_device}",
                )
            )
            play_result = play_wav(wav_path, settings.audio_device)
            db.add(
                CallEvent(
                    call_record=record,
                    event_type="audio_end",
                    message=(
                        f"aplay exit={play_result.returncode} {play_result.message}".strip()
                        if play_result.message
                        else f"aplay exit={play_result.returncode}"
                    ),
                )
            )
            if not play_result.success:
                self._safe_hangup(db, record, modem)
                # 已接通并触达客户，不自动重拨，避免骚扰；由人工决定是否重打。
                return "failed", None, f"Audio playback failed: {play_result.message}", False

            # 播放结束后等待模块上报结束事件；没有则主动挂断。
            linger_deadline = time.monotonic() + AFTER_PLAY_LINGER_SECONDS
            while time.monotonic() < linger_deadline:
                line = modem.read_line()
                if not line:
                    continue
                parsed = parse_modem_line(line)
                db.add(
                    CallEvent(
                        call_record=record,
                        event_type=parsed.event_type,
                        message=line,
                        raw_line=line,
                    )
                )
                if parsed.event_type in END_EVENT_TYPES:
                    end_event = parsed
                    break
            if end_event is None:
                self._safe_hangup(db, record, modem)

            duration = None
            if end_event is not None and end_event.duration_seconds:
                duration = end_event.duration_seconds
            elif begin_mono is not None:
                duration = int(time.monotonic() - begin_mono)

            if duration is not None and duration < settings.min_connected_seconds:
                return "short_call", duration, "", False
            return "completed", duration, "", False

    def _classify_unconnected(
        self, end_event: ParsedModemLine, dial_started_mono: float
    ) -> tuple[str, int | None, str, bool]:
        """未接通分类（docs/callback-demo-plan.md「未接通粗分类」）。"""
        elapsed = int(time.monotonic() - dial_started_mono)
        if end_event is not None and end_event.duration_seconds is not None:
            elapsed = end_event.duration_seconds
        if elapsed < self.settings.rejected_end_seconds:
            # 响铃后被快速释放视为主动拒接/秒挂：尊重对方意愿，不自动重拨。
            return "rejected", elapsed, "", False
        if elapsed < NO_ANSWER_END_SECONDS:
            return "cancelled_or_failed", elapsed, "", True
        return "no_answer", elapsed, "", True

    def _safe_hangup(self, db: Session, record: CallRecord, modem: ModemClient) -> None:
        """保留 AT+CHUP 清理路径：异常、超时和进程退出时释放通话。"""
        try:
            db.add(CallEvent(call_record=record, event_type="hangup", message="AT+CHUP"))
            modem.hangup()
        except Exception as exc:  # noqa: BLE001 - 挂断失败也要记录而不是抛出
            db.add(CallEvent(call_record=record, event_type="error", message=f"Hangup failed: {exc}"))

    # ---------------------------------------------------------------- 收尾与重试

    def _schedule_retry(
        self, db: Session, task: CallTask, record: CallRecord, status: str, error: str
    ) -> None:
        delay = self.settings.retry_delay_seconds
        now = utcnow()
        task.attempt += 1
        task.status = "queued"
        task.due_at = now + timedelta(seconds=delay)
        task.started_at = None
        task.completed_at = None
        task.error_message = ""
        record.status = "queued"
        record.dialing_started_at = None
        record.connected_at = None
        record.ended_at = None
        record.duration_seconds = None
        record.error_message = ""
        db.add(
            CallEvent(
                call_record=record,
                event_type="retry_scheduled",
                message=f"Attempt {task.attempt - 1} ended with {status}; retry in {delay}s.",
            )
        )

    def _finalize(
        self,
        db: Session,
        task: CallTask,
        record: CallRecord,
        status: str,
        duration: int | None,
        error: str,
    ) -> None:
        now = utcnow()
        task.status = status
        task.completed_at = now
        task.error_message = error or ""
        record.status = status
        record.ended_at = now
        record.duration_seconds = duration
        record.error_message = error or ""
        db.add(
            CallEvent(
                call_record=record,
                event_type=status,
                message=error or f"Call ended with {status}.",
            )
        )

    def _permanent_fail(self, db: Session, task: CallTask, record: CallRecord, message: str) -> None:
        # 配置类错误（无号码/无音频）不自动重试。
        self._finalize(db, task, record, "failed", None, message)


class CallWorkerService:
    """后台单线程循环：负责领取队列任务并交给 `CallWorker` 执行。

    生命周期受两层开关控制：
    - `.env` 的 `CALL_WORKER_ENABLED`：进程是否允许启动（硬边界，默认关）。
    - 数据库 `AppSetting.call_worker_enabled`：管理页运行时开关。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker = CallWorker(self.settings)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._recovery_done = False
        self._status: dict[str, object] = {
            "running": False,
            "working": False,
            "task_id": None,
            "current_number": "",
            "started_at": None,
            "last_run_at": None,
            "last_result": "",
            "last_error": "",
            "processed": 0,
        }

    def status(self) -> dict[str, object]:
        with self._lock:
            return dict(self._status)

    def start(self) -> bool:
        """启动后台线程；`.env` 硬开关未开启时拒绝启动。"""
        if not self.settings.call_worker_enabled:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._recovery_done = False
        self._thread = threading.Thread(target=self._loop, name="call-worker", daemon=True)
        self._thread.start()
        with self._lock:
            self._status["running"] = True
            self._status["last_error"] = ""
        return True

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)
        with self._lock:
            self._status["running"] = False
            self._status["working"] = False
            self._status["task_id"] = None

    def set_enabled(self, db: Session, enabled: bool) -> None:
        """管理页运行时开关：持久化设置并实际启停线程。"""
        set_setting(db, CALL_WORKER_ENABLED_KEY, "1" if enabled else "0")
        if enabled:
            self.start()
        else:
            self.stop()

    def shutdown(self) -> None:
        self.stop()

    def recover_interrupted_tasks(self, db: Session) -> int:
        """Finish tasks left in an in-progress state by a process restart.

        A crashed process may leave the modem with an active call.  Attempt one
        real ``AT+CHUP`` cleanup before marking every stale task failed.  The
        caller commits the transaction; when no stale task exists, no serial
        port is opened.
        """

        tasks = db.scalars(
            select(CallTask)
            .where(CallTask.status.in_(("dialing", "connected")))
            .order_by(CallTask.started_at.asc(), CallTask.created_at.asc())
        ).all()
        if not tasks:
            return 0

        cleanup_error = ""
        try:
            with ModemClient(self.settings.modem_port, self.settings.modem_baud) as modem:
                modem.hangup()
        except Exception as exc:  # noqa: BLE001 - recovery must still close records
            cleanup_error = str(exc)

        interrupted_message = "进程重启中断，未完成释放。"
        for task in tasks:
            record = task.call_record
            if record is None:
                record = CallRecord(
                    task=task,
                    plan=task.plan,
                    customer=task.customer,
                    script=task.script,
                    contact=task.contact,
                    dial_number=task.dial_number,
                    status=task.status,
                )
                db.add(record)
            db.add(
                CallEvent(
                    call_record=record,
                    event_type="recovery",
                    message=interrupted_message,
                )
            )
            if cleanup_error:
                db.add(
                    CallEvent(
                        call_record=record,
                        event_type="error",
                        message=f"AT+CHUP 清理失败: {cleanup_error}",
                    )
                )
            else:
                db.add(
                    CallEvent(
                        call_record=record,
                        event_type="hangup",
                        message="AT+CHUP (进程重启清理)",
                    )
                )
            self.worker._finalize(
                db,
                task,
                record,
                "failed",
                None,
                interrupted_message
                if not cleanup_error
                else f"{interrupted_message} AT+CHUP 清理失败: {cleanup_error}",
            )
        return len(tasks)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    ensure_default_settings(db)
                    if is_worker_enabled(db):
                        self._tick(db)
                    db.commit()
            except Exception as exc:  # noqa: BLE001 - 循环不能因单次错误退出
                with self._lock:
                    self._status["last_error"] = str(exc)
            self._stop.wait(self.settings.worker_poll_seconds)
        with self._lock:
            self._status["running"] = False

    def _tick(self, db: Session) -> None:
        if not self._recovery_done:
            self.recover_interrupted_tasks(db)
            db.commit()
            self._recovery_done = True

        task = self.worker.claim_next_task(db)
        db.commit()  # 尽早持久化 dialing，防止重复领取。
        if task is None:
            with self._lock:
                self._status["working"] = False
                self._status["task_id"] = None
            return

        with self._lock:
            self._status["working"] = True
            self._status["task_id"] = task.id
            self._status["current_number"] = task.dial_number
            self._status["started_at"] = utcnow()
        try:
            final_status = self.worker.handle_task(
                db,
                task,
                commit=lambda: db.commit(),
                cancel_check=self._stop.is_set,
            )
            db.commit()
            with self._lock:
                self._status["last_result"] = final_status
                self._status["processed"] = int(self._status["processed"]) + 1
                self._status["last_run_at"] = utcnow()
                self._status["last_error"] = ""
        except Exception as exc:  # noqa: BLE001 - 任务级错误记录到状态，循环继续
            db.rollback()
            with self._lock:
                self._status["last_error"] = str(exc)
        finally:
            with self._lock:
                self._status["working"] = False
                self._status["task_id"] = None
                self._status["current_number"] = ""


call_worker_service = CallWorkerService()
