from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CallEvent, CallRecord, CallTask, utcnow


class CallWorker:
    """Single-channel queue worker skeleton.

    The real hardware dial/play state machine belongs here. The demo app does
    not start this worker automatically, so creating plans cannot place calls by
    accident.
    """

    def run_one_pending(self, db: Session) -> CallTask | None:
        task = db.scalars(
            select(CallTask)
            .where(CallTask.status == "queued")
            .order_by(CallTask.due_at.asc(), CallTask.created_at.asc())
            .limit(1)
        ).first()
        if task is None:
            return None

        task.status = "dialing"
        task.started_at = utcnow()
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
        record.dialing_started_at = task.started_at
        db.add(CallEvent(call_record=record, event_type="dialing", message="Worker claimed task."))

        if not task.dial_number:
            self._fail_task(db, task, record, "Task has no dial number.")

        wav_path = task.script.wav_path
        if not wav_path or not Path(wav_path).exists():
            self._fail_task(db, task, record, "Script has no playable WAV file.")

        db.flush()
        return task

    def _fail_task(self, db: Session, task: CallTask, record: CallRecord, message: str) -> None:
        now = utcnow()
        task.status = "failed"
        task.completed_at = now
        task.error_message = message
        record.status = "failed"
        record.ended_at = now
        record.error_message = message
        db.add(CallEvent(call_record=record, event_type="failed", message=message))
