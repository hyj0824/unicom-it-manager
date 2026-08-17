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
    app_host: str
    app_port: int
    admin_password: str
    session_secret: str
    database_url: str
    modem_port: str
    modem_baud: int
    audio_device: str
    call_connect_timeout_seconds: int
    min_connected_seconds: int
    retry_delay_seconds: int
    max_call_attempts: int
    tts_provider: str
    tts_api_key: str
    tts_voice: str
    default_timezone: str

    @property
    def cookie_secret(self) -> str:
        return self.session_secret or self.admin_password or "callback-demo-session-secret"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_host=_env("APP_HOST", "0.0.0.0"),
        app_port=_env_int("APP_PORT", 8000),
        admin_password=_env("ADMIN_PASSWORD"),
        session_secret=_env("SESSION_SECRET"),
        database_url=_env("DATABASE_URL", "sqlite:///./data/app.db"),
        modem_port=_env("MODEM_PORT", "/dev/ttyUSB1"),
        modem_baud=_env_int("MODEM_BAUD", 115200),
        audio_device=_env("AUDIO_DEVICE", "plughw:1,0"),
        call_connect_timeout_seconds=_env_int("CALL_CONNECT_TIMEOUT_SECONDS", 90),
        min_connected_seconds=_env_int("MIN_CONNECTED_SECONDS", 8),
        retry_delay_seconds=_env_int("RETRY_DELAY_SECONDS", 300),
        max_call_attempts=_env_int("MAX_CALL_ATTEMPTS", 2),
        tts_provider=_env("TTS_PROVIDER", "none"),
        tts_api_key=_env("TTS_API_KEY"),
        tts_voice=_env("TTS_VOICE"),
        default_timezone=_env("DEFAULT_TIMEZONE", "Asia/Shanghai"),
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
