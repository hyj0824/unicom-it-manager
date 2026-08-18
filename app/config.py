from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - runtime dependency is declared
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent.parent

if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    admin_password: str
    session_secret: str
    database_url: str
    modem_port: str
    modem_baud: int
    audio_device: str
    call_connect_timeout_seconds: int
    rejected_end_seconds: int
    min_connected_seconds: int
    retry_delay_seconds: int
    max_call_attempts: int
    tts_provider: str
    tts_api_key: str
    tts_voice: str
    default_timezone: str
    # 外呼 Worker 自动启动硬开关：默认关闭，防止开发/测试时误拨电话。
    call_worker_enabled: bool
    worker_poll_seconds: int

    @property
    def cookie_secret(self) -> str:
        return self.session_secret or self.admin_password or "callback-demo-session-secret"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        admin_password=_env("ADMIN_PASSWORD"),
        session_secret=_env("SESSION_SECRET"),
        database_url=_env("DATABASE_URL", "sqlite:///./data/app.db"),
        modem_port=_env("MODEM_PORT", "/dev/ttyUSB1"),
        modem_baud=_env_int("MODEM_BAUD", 115200),
        audio_device=_env("AUDIO_DEVICE", "plughw:1,0"),
        call_connect_timeout_seconds=_env_int("CALL_CONNECT_TIMEOUT_SECONDS", 90),
        # 响铃后未接通释放的拒接阈值：小于该时长视为主动拒接，不自动重试。
        rejected_end_seconds=_env_int("REJECTED_END_SECONDS", 20),
        min_connected_seconds=_env_int("MIN_CONNECTED_SECONDS", 8),
        retry_delay_seconds=_env_int("RETRY_DELAY_SECONDS", 300),
        max_call_attempts=_env_int("MAX_CALL_ATTEMPTS", 2),
        tts_provider=_env("TTS_PROVIDER", "none"),
        tts_api_key=_env("TTS_API_KEY"),
        tts_voice=_env("TTS_VOICE"),
        default_timezone=_env("DEFAULT_TIMEZONE", "Asia/Shanghai"),
        call_worker_enabled=_env("CALL_WORKER_ENABLED", "0").lower()
        in {"1", "true", "yes", "on"},
        worker_poll_seconds=_env_int("WORKER_POLL_SECONDS", 5),
    )


def ensure_storage_paths(settings: Settings) -> None:
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        path = Path(db_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)

    (BASE_DIR / "data").mkdir(exist_ok=True)


def validate_runtime_settings(settings: Settings) -> None:
    if not settings.admin_password or settings.admin_password == "change-me":
        raise RuntimeError(
            "ADMIN_PASSWORD must be configured before starting the web server."
        )
    # SESSION_SECRET 用于签名登录会话 Cookie，与 ADMIN_PASSWORD 同级校验：
    # 为空或仍为 .env.example 的示例值时拒绝启动，防止真实部署使用弱密钥。
    if not settings.session_secret or settings.session_secret == "change-me-too":
        raise RuntimeError(
            "SESSION_SECRET must be configured with a random value "
            "before starting the web server."
        )
