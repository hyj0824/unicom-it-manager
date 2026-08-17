from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_settings
from .database import SessionLocal
from .services.plans import enqueue_due_plans, mark_missed_once_plans
from .services.settings import ensure_default_settings, is_scheduler_enabled


def _scheduler_timezone() -> ZoneInfo:
    settings = get_settings()
    try:
        return ZoneInfo(settings.default_timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None

    def start(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            return

        with SessionLocal() as db:
            ensure_default_settings(db)
            mark_missed_once_plans(db)
            db.commit()

        scheduler = BackgroundScheduler(timezone=_scheduler_timezone())
        scheduler.add_job(
            self.tick,
            "interval",
            seconds=15,
            id="enqueue_due_callback_plans",
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
        self._scheduler = scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def tick(self) -> None:
        with SessionLocal() as db:
            ensure_default_settings(db)
            if is_scheduler_enabled(db):
                enqueue_due_plans(db)
            db.commit()


scheduler_service = SchedulerService()
