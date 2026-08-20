from __future__ import annotations

from app.services.users import hash_password, validate_user_profile, verify_password


def test_hash_and_verify_round_trip() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("pbkdf2_sha256$200000$")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_verify_rejects_malformed_stored_value() -> None:
    assert not verify_password("anything", "")
    assert not verify_password("anything", "plain$1$aa$bb")
    assert not verify_password("anything", "pbkdf2_sha256$notanint$aa$bb")


# ---------------------------------------------------------------- 实名/手机校验


def test_validate_user_profile_requires_real_name() -> None:
    assert validate_user_profile("", "13800000000") == "实名必填。"
    assert validate_user_profile("   ", "13800000000") == "实名必填。"


def test_validate_user_profile_requires_phone_for_regular_user() -> None:
    assert validate_user_profile("张三", "") == "手机号必填（系统管理员可不填）。"
    assert validate_user_profile("张三", "   ") == "手机号必填（系统管理员可不填）。"


def test_validate_user_profile_rejects_bad_phone_format() -> None:
    assert "手机号格式不正确" in validate_user_profile("张三", "123")
    assert "手机号格式不正确" in validate_user_profile("张三", "13800000000-abc")
    assert "手机号格式不正确" in validate_user_profile("张三", "abcdefgh")
    assert "手机号格式不正确" in validate_user_profile("张三", "138000000000000000000")  # 21 位


def test_validate_user_profile_accepts_valid_phone() -> None:
    assert validate_user_profile("张三", "13800000000") is None
    assert validate_user_profile("张三", "+8613800000000") is None
    assert validate_user_profile("张三", " 13800000000 ") is None  # 允许首尾空白


def test_validate_user_profile_allows_blank_phone_only_when_role_authorized() -> None:
    assert validate_user_profile("系统管理员", "") == "手机号必填（系统管理员可不填）。"
    assert validate_user_profile("系统管理员", "", allow_blank_phone=True) is None
