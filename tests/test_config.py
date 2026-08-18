"""运行时配置校验单元测试：ADMIN_PASSWORD 与 SESSION_SECRET 启动校验。"""

from __future__ import annotations

import pytest

from app.config import Settings, validate_runtime_settings


def make_settings(**overrides) -> Settings:
    base = dict(
        admin_password="strong-admin-password",
        session_secret="random-session-secret-value",
        database_url="sqlite:///:memory:",
        modem_port="/dev/ttyUSB1",
        modem_baud=115200,
        audio_device="plughw:1,0",
        call_connect_timeout_seconds=90,
        rejected_end_seconds=20,
        min_connected_seconds=8,
        retry_delay_seconds=300,
        max_call_attempts=2,
        tts_provider="none",
        tts_api_key="",
        tts_voice="",
        default_timezone="Asia/Shanghai",
        call_worker_enabled=False,
        worker_poll_seconds=5,
    )
    base.update(overrides)
    return Settings(**base)


def test_valid_settings_pass_validation() -> None:
    validate_runtime_settings(make_settings())


def test_admin_password_required() -> None:
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        validate_runtime_settings(make_settings(admin_password=""))
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        validate_runtime_settings(make_settings(admin_password="change-me"))


def test_session_secret_required() -> None:
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        validate_runtime_settings(make_settings(session_secret=""))
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        validate_runtime_settings(make_settings(session_secret="change-me-too"))


def test_cookie_secret_prefers_session_secret() -> None:
    settings = make_settings(session_secret="abc123")
    assert settings.cookie_secret == "abc123"
    # 降级路径仅用于未接入启动校验的调用方，保持原有兼容行为。
    fallback = make_settings(session_secret="", admin_password="pw")
    assert fallback.cookie_secret == "pw"
