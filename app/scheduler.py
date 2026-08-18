from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal
from .models import ScanSchedule, utcnow
from .services.plans import as_utc, get_zone
from .services.settings import ensure_default_settings, is_scheduler_enabled

logger = logging.getLogger(__name__)


def _scheduler_timezone() -> ZoneInfo:
    settings = get_settings()
    try:
        return ZoneInfo(settings.default_timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def cron_matches_now(cron_expr: str, timezone_name: str, now: datetime | None = None) -> bool:
    """判断 cron 表达式是否匹配「当前分钟」（按配置时区）。

    调度任务每分钟执行一次：把时刻截断到分钟（秒清零）后向 APScheduler 查询
    下一次触发时间；cron 表达式没有秒字段、触发时刻恒为 xx:xx:00，因此
    「下一次触发时间仍是当前分钟」即视为匹配。以分钟而不是秒为粒度判断，
    可以避免同一分钟内多次 tick 重复触发扫描。
    """

    zone = get_zone(timezone_name)
    local_now = as_utc(now or utcnow()).astimezone(zone)
    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=zone)
    except ValueError as exc:
        # 与表单校验一致：错误信息带上原表达式，写入 last_error 后可读。
        raise ValueError(f"Cron 表达式「{cron_expr}」无效：{exc}") from exc
    minute = local_now.replace(second=0, microsecond=0)
    fire_at = trigger.get_next_fire_time(None, minute)
    return fire_at is not None and fire_at <= local_now


def _run_scan(db, schedule: ScanSchedule) -> int:
    """按契约调用扫描实现。

    用 ``importlib.import_module`` 而非 ``from ... import``：import_module 优先
    从 sys.modules 解析模块，测试里注入的假 scans 模块才能稳定生效（包属性
    缓存会绕过 sys.modules）。
    """

    import importlib

    scan_service = importlib.import_module("app.services.scans")
    return scan_service.run_scan_for_schedule(db, schedule)


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None
        self._last_tick_at: datetime | None = None

    def start(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            return

        with SessionLocal() as db:
            ensure_default_settings(db)
            db.commit()

        scheduler = BackgroundScheduler(timezone=_scheduler_timezone())
        scheduler.add_job(
            self.tick,
            "interval",
            seconds=60,
            id="run_scan_schedules",
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
                schedules = db.scalars(
                    select(ScanSchedule)
                    .where(ScanSchedule.enabled.is_(True))
                    .order_by(ScanSchedule.created_at.asc(), ScanSchedule.id.asc())
                ).all()
                for schedule in schedules:
                    try:
                        if not cron_matches_now(
                            schedule.cron_expr, schedule.timezone, self._last_tick_at
                        ):
                            continue
                        _run_scan(db, schedule)
                        schedule.last_run_at = self._last_tick_at
                        schedule.last_error = ""
                        db.commit()
                    except Exception as exc:  # noqa: BLE001 - 单条扫描失败不拖垮调度
                        db.rollback()
                        # 回滚后原对象已过期：重新取回同一记录再记错误。
                        failed = db.get(ScanSchedule, schedule.id)
                        if failed is not None:
                            failed.last_error = f"{type(exc).__name__}: {exc}"[:500]
                            db.commit()
                        logger.exception("scan schedule %s (%s) failed", schedule.id, schedule.name)
            db.commit()

    def status(self) -> dict:
        """监控页使用的调度器状态。"""

        return {
            "running": bool(self._scheduler is not None and self._scheduler.running),
            "last_tick_at": self._last_tick_at,
        }


scheduler_service = SchedulerService()
