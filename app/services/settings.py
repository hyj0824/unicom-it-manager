from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AppSetting


SCHEDULER_ENABLED_KEY = "scheduler_enabled"


def get_setting(db: Session, key: str, default: str = "") -> str:
    setting = db.get(AppSetting, key)
    if setting is None:
        return default
    return setting.value


def set_setting(db: Session, key: str, value: str) -> AppSetting:
    setting = db.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value=value)
        db.add(setting)
    else:
        setting.value = value
    return setting


def ensure_default_settings(db: Session) -> None:
    if db.get(AppSetting, SCHEDULER_ENABLED_KEY) is None:
        db.add(AppSetting(key=SCHEDULER_ENABLED_KEY, value="1"))


def is_scheduler_enabled(db: Session) -> bool:
    return get_setting(db, SCHEDULER_ENABLED_KEY, "1") == "1"
