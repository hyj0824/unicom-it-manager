from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import get_settings
from .services.users import find_user, verify_password

SESSION_KEY = "principal"


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def current_user(request: Request) -> dict | None:
    """当前登录主体：{'type': 'admin'|'user', 'id': ..., 'name': ...}。"""

    principal = request.session.get(SESSION_KEY)
    if not isinstance(principal, dict):
        return None
    return principal


def require_login(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )


def verify_credentials(db: Session, username: str, password: str) -> dict | None:
    """校验登录凭据：admin 走 ADMIN_PASSWORD，其余查用户表（仅启用账号）。"""

    username = username.strip()
    if not username or not password:
        return None
    if username == "admin":
        settings = get_settings()
        if hmac.compare_digest(password, settings.admin_password):
            return {"type": "admin", "id": None, "name": "管理员"}
        return None
    user = find_user(db, username)
    if user is None or not user.is_enabled:
        return None
    if not user.password_hash or not verify_password(password, user.password_hash):
        return None
    return {"type": "user", "id": user.id, "name": user.display_name or user.username}


def login(request: Request, principal: dict) -> None:
    request.session[SESSION_KEY] = principal


def logout(request: Request) -> None:
    request.session.clear()
