from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from .config import get_settings


SESSION_KEY = "admin_authenticated"


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def require_admin(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )


def verify_admin_password(password: str) -> bool:
    settings = get_settings()
    return hmac.compare_digest(password, settings.admin_password)


def login(request: Request) -> None:
    request.session[SESSION_KEY] = True


def logout(request: Request) -> None:
    request.session.clear()
