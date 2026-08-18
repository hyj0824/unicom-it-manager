"""扫描通知配置的纯单元测试：cron 表达式 / 时区校验、调度触发时刻匹配。

原 test_plan_times.py 中的计划时间计算已随「回访计划」概念删除；这里覆盖
扫描配置表单复用的 `validate_cron_expr`（校验 cron 与时区），以及调度器
判断「当前分钟是否匹配 cron」的纯函数 `cron_matches_now`。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.scheduler import cron_matches_now
from app.services.plans import get_zone, validate_cron_expr

UTC = timezone.utc
SHANGHAI = get_zone("Asia/Shanghai")


def utc_dt(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------- cron 与时区校验


def test_validate_cron_expr_accepts_valid_expression():
    validate_cron_expr("0 9 * * *", "Asia/Shanghai")


def test_validate_cron_expr_requires_expression():
    with pytest.raises(ValueError, match="必须填写 Cron 表达式"):
        validate_cron_expr("   ", "Asia/Shanghai")


def test_validate_cron_expr_rejects_invalid_expression_with_expression_in_message():
    with pytest.raises(ValueError, match="Cron 表达式「0 9 \\* \\*」无效"):
        validate_cron_expr("0 9 * *", "Asia/Shanghai")
    with pytest.raises(ValueError, match="Cron 表达式「61 9 \\* \\* \\*」无效"):
        validate_cron_expr("61 9 * * *", "Asia/Shanghai")


def test_validate_cron_expr_rejects_invalid_timezone():
    # 扫描配置不允许静默回退 UTC：时区错误必须给出明确提示。
    with pytest.raises(ValueError, match="时区「Mars/Olympus」无效"):
        validate_cron_expr("0 9 * * *", "Mars/Olympus")


def test_get_zone_falls_back_to_utc_for_display():
    # 仅展示/调度换算容错路径保留回退 UTC（与表单校验的严格行为互补）。
    assert str(get_zone("Mars/Olympus")) == "UTC"
    assert str(get_zone("Asia/Shanghai")) == "Asia/Shanghai"


# ---------------------------------------------------------------- 调度触发时刻匹配


def test_cron_matches_when_current_minute_is_fire_time():
    # 上海 09:00 = UTC 01:00；当前时刻落在 09:00:00-09:00:59 之间均匹配。
    assert cron_matches_now("0 9 * * *", "Asia/Shanghai", utc_dt(2026, 1, 15, 1, 0)) is True
    assert cron_matches_now("0 9 * * *", "Asia/Shanghai", utc_dt(2026, 1, 15, 1, 0, 59)) is True


def test_cron_does_not_match_other_minutes():
    assert cron_matches_now("0 9 * * *", "Asia/Shanghai", utc_dt(2026, 1, 15, 0, 59)) is False
    assert cron_matches_now("0 9 * * *", "Asia/Shanghai", utc_dt(2026, 1, 15, 1, 1)) is False


def test_cron_matches_use_configured_timezone():
    # 同一 UTC 时刻（01:00），上海 09:00 匹配而纽约（前一日 20:00）不匹配。
    assert cron_matches_now("0 9 * * *", "Asia/Shanghai", utc_dt(2026, 1, 15, 1, 0)) is True
    assert cron_matches_now("0 9 * * *", "America/New_York", utc_dt(2026, 1, 15, 1, 0)) is False
    # 纽约 09:00 = UTC 14:00。
    assert cron_matches_now("0 9 * * *", "America/New_York", utc_dt(2026, 1, 15, 14, 0)) is True


def test_cron_matches_weekday_expression():
    # APScheduler 的星期字段 0=周一（与标准 cron 的 0=周日不同），这里用星期名避免歧义。
    # 2026-01-15 是周四；工作日表达式在上海 08:30 匹配。
    assert cron_matches_now("30 8 * * mon-fri", "Asia/Shanghai", utc_dt(2026, 1, 15, 0, 30)) is True
    # 2026-01-17 是周六：同一时刻不匹配。
    assert cron_matches_now("30 8 * * mon-fri", "Asia/Shanghai", utc_dt(2026, 1, 17, 0, 30)) is False


def test_cron_matches_sub_hour_expression_at_second_zero_only():
    # */30 分钟：30 分（上海 00:30 = UTC 前日 16:30）匹配，31 分不匹配。
    assert cron_matches_now("*/30 * * * *", "Asia/Shanghai", utc_dt(2026, 1, 15, 0, 30)) is True
    assert cron_matches_now("*/30 * * * *", "Asia/Shanghai", utc_dt(2026, 1, 15, 0, 31)) is False
