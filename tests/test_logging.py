"""结构化日志测试：格式包含时间/级别/logger；密钥与完整手机号被打码。"""

from __future__ import annotations

import logging

from app.logging import (
    LOG_DATE_FORMAT,
    LOG_FORMAT,
    RedactFilter,
    configure_logging,
    mask_phone,
)


def test_log_format_contains_time_level_and_logger() -> None:
    assert "%(asctime)s" in LOG_FORMAT
    assert "%(levelname)s" in LOG_FORMAT
    assert "%(name)s" in LOG_FORMAT
    assert "%(message)s" in LOG_FORMAT
    assert "%Y" in LOG_DATE_FORMAT


def test_mask_phone_masks_mainland_mobile() -> None:
    assert mask_phone("13812345678") == "138****5678"
    assert mask_phone("+8613812345678") == "+86138****5678"
    assert mask_phone("+86 13812345678") == "+86 138****5678"
    assert mask_phone("来电 13912345678 请回拨") == "来电 139****5678 请回拨"


def test_mask_phone_leaves_other_numbers_untouched() -> None:
    # 短号、非手机号段、业务编号等保持原样。
    assert mask_phone("12345") == "12345"
    assert mask_phone("11000000000") == "11000000000"  # 1[0-2] 号段不匹配
    assert mask_phone("业务编号 20260115") == "业务编号 20260115"
    assert mask_phone("") == ""


def test_redact_filter_hides_secrets_and_phones() -> None:
    record = logging.LogRecord(
        "app.test",
        logging.INFO,
        __file__,
        1,
        "user %s dialing %s",
        ("admin", "13812345678"),
        None,
    )
    filt = RedactFilter(secrets=("admin",))
    assert filt.filter(record) is True
    assert record.getMessage() == "user *** dialing 138****5678"


def test_redact_filter_ignores_empty_secrets() -> None:
    record = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1, "no secrets here", (), None
    )
    filt = RedactFilter(secrets=("", "change-me-too"))
    assert filt.filter(record) is True
    assert record.getMessage() == "no secrets here"


def test_configure_logging_applies_format_and_redaction(caplog) -> None:
    configure_logging(redact_secrets=("top-secret",))
    logger = logging.getLogger("app.logging_test")
    logger.info("secret=%s number=%s", "top-secret", "13812345678")

    assert "top-secret" not in caplog.text
    assert "secret=*** number=138****5678" in caplog.text


def test_configure_logging_is_idempotent() -> None:
    configure_logging(redact_secrets=("first",))
    root = logging.getLogger()
    before = len(root.filters)

    configure_logging(redact_secrets=("second",))

    assert len(root.filters) == before
    redact_filters = [f for f in root.filters if isinstance(f, RedactFilter)]
    assert len(redact_filters) == 1
    assert redact_filters[0].secrets == ("second",)
