from __future__ import annotations

from app.services.users import hash_password, verify_password


def test_hash_and_verify_round_trip() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("pbkdf2_sha256$200000$")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_verify_rejects_malformed_stored_value() -> None:
    assert not verify_password("anything", "")
    assert not verify_password("anything", "plain$1$aa$bb")
    assert not verify_password("anything", "pbkdf2_sha256$notanint$aa$bb")
