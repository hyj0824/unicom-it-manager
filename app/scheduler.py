from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_settings
from .database import SessionLocal
from .models import utcnow
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
        self._last_tick_at: datetime | None = None

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
        self._last_tick_at = utcnow()
        with SessionLocal() as db:
            ensure_default_settings(db)
            if is_scheduler_enabled(db):
                enqueue_due_plans(db)
            db.commit()

    def status(self) -> dict:
        """监控页使用的调度器状态。"""

        return {
            "running": bool(self._scheduler is not None and self._scheduler.running),
            "last_tick_at": self._last_tick_at,
        }


scheduler_service = SchedulerService()
