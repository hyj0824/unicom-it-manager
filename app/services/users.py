from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Role, User, UserRole

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


def find_user(db: Session, username: str) -> User | None:
    return db.scalars(select(User).where(User.username == username)).first()


def role_names(db: Session, user: User) -> list[str]:
    names = db.scalars(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
        .order_by(Role.id.asc())
    ).all()
    return list(names)


def set_user_roles(db: Session, user: User, role_ids: list[int]) -> None:
    db.query(UserRole).filter(UserRole.user_id == user.id).delete()
    for role_id in role_ids:
        db.add(UserRole(user_id=user.id, role_id=role_id))
