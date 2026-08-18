"""计划时间计算单元测试：once / cron 下一次执行时间、无效 cron、时区转换。

本应用场景为固定偏移时区（Asia/Shanghai），不存在夏令时 corner case，
不测试 DST 边界。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.plans import (
    compute_next_run_at,
    datetime_local_value,
    get_zone,
    parse_datetime_local,
)

UTC = timezone.utc
SHANGHAI = get_zone("Asia/Shanghai")


def utc_dt(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------- once 计划


def test_once_returns_utc_equivalent():
    run_at = datetime(2026, 1, 15, 10, 30, tzinfo=SHANGHAI)
    assert compute_next_run_at("once", run_at, "", "Asia/Shanghai") == datetime(
        2026, 1, 15, 2, 30, tzinfo=UTC
    )


def test_once_naive_run_at_treated_as_utc():
    # 应用内 run_at 都经过 parse_datetime_local 带时区；这里固化直接调用时的行为。
    run_at = datetime(2026, 1, 15, 10, 30)
    assert compute_next_run_at("once", run_at, "", "Asia/Shanghai") == utc_dt(
        2026, 1, 15, 10, 30
    )


def test_once_requires_run_at():
    with pytest.raises(ValueError, match="必须填写执行时间"):
        compute_next_run_at("once", None, "", "Asia/Shanghai")


def test_invalid_trigger_type_rejected():
    with pytest.raises(ValueError, match="trigger_type"):
        compute_next_run_at("monthly", utc_dt(2026, 1, 1, 0), "", "Asia/Shanghai")


# ---------------------------------------------------------------- cron 计划


def test_cron_computes_next_fire_in_plan_timezone():
    # 上海 2026-01-15 00:00 = UTC 2026-01-14 16:00；下次 09:00 上海 = 01:00 UTC。
    from_time = utc_dt(2026, 1, 14, 16, 0)
    assert compute_next_run_at("cron", None, "0 9 * * *", "Asia/Shanghai", from_time) == utc_dt(
        2026, 1, 15, 1, 0
    )


def test_cron_fire_time_equal_to_now_is_returned():
    # APScheduler 的 next fire 对 now 取 >=；入队逻辑用 now + 1s 保证严格推进。
    from_time = utc_dt(2026, 1, 15, 1, 0)  # 恰好是上海 09:00
    assert compute_next_run_at("cron", None, "0 9 * * *", "Asia/Shanghai", from_time) == from_time


def test_cron_from_time_after_fire_advances_to_next_day():
    from_time = utc_dt(2026, 1, 15, 1, 0, 1)
    assert compute_next_run_at("cron", None, "0 9 * * *", "Asia/Shanghai", from_time) == utc_dt(
        2026, 1, 16, 1, 0
    )


def test_cron_requires_expression():
    with pytest.raises(ValueError, match="必须填写 Cron 表达式"):
        compute_next_run_at("cron", None, "   ", "Asia/Shanghai")


def test_cron_invalid_expression_rejected():
    # 字段数不对。
    with pytest.raises(ValueError):
        compute_next_run_at("cron", None, "not a cron", "Asia/Shanghai")
    # 分钟超出范围。
    with pytest.raises(ValueError):
        compute_next_run_at("cron", None, "61 9 * * *", "Asia/Shanghai")


def test_cron_never_fires_returns_none():
    # 2 月 30 日不存在，永远不触发。
    assert compute_next_run_at("cron", None, "0 0 30 2 *", "Asia/Shanghai") is None


# ---------------------------------------------------------------- 时区转换


def test_invalid_timezone_falls_back_to_utc():
    # 非法时区名回退 UTC：9:00 UTC 触发。
    from_time = utc_dt(2026, 1, 15, 0, 0)
    assert compute_next_run_at("cron", None, "0 9 * * *", "Mars/Olympus", from_time) == utc_dt(
        2026, 1, 15, 9, 0
    )


def test_parse_datetime_local_uses_plan_timezone():
    parsed = parse_datetime_local("2026-01-15T10:30", "Asia/Shanghai")
    assert parsed == utc_dt(2026, 1, 15, 2, 30)


def test_parse_datetime_local_empty_returns_none():
    assert parse_datetime_local("  ", "Asia/Shanghai") is None


def test_parse_datetime_local_aware_keeps_own_offset():
    # 带偏移的输入不再叠加计划时区。
    parsed = parse_datetime_local("2026-01-15T10:30+08:00", "America/New_York")
    assert parsed == utc_dt(2026, 1, 15, 2, 30)


def test_datetime_local_value_round_trip():
    parsed = parse_datetime_local("2026-01-15T10:30", "Asia/Shanghai")
    assert datetime_local_value(parsed, "Asia/Shanghai") == "2026-01-15T10:30"


def test_zone_conversion_matches_fixed_offset():
    # 固定偏移时区转换一致性：上海为 UTC+8（无夏令时）。
    local = datetime(2026, 6, 1, 12, 0, tzinfo=SHANGHAI)
    assert local.astimezone(UTC) == utc_dt(2026, 6, 1, 4, 0)
