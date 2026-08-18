from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppSetting


SCHEDULER_ENABLED_KEY = "scheduler_enabled"
CALL_WORKER_ENABLED_KEY = "call_worker_enabled"


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
    if db.get(AppSetting, CALL_WORKER_ENABLED_KEY) is None:
        # 运行时开关的默认值与 .env 硬开关保持一致，避免配置未开启却显示“已启用”。
        default_worker = "1" if get_settings().call_worker_enabled else "0"
        db.add(AppSetting(key=CALL_WORKER_ENABLED_KEY, value=default_worker))


def is_scheduler_enabled(db: Session) -> bool:
    return get_setting(db, SCHEDULER_ENABLED_KEY, "1") == "1"


def is_worker_enabled(db: Session) -> bool:
    return get_setting(db, CALL_WORKER_ENABLED_KEY, "0") == "1"
