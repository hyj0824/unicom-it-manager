from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Permission, Role, RolePermission, User, UserRole
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


def has_permission(db: Session, request: Request, permission: str, domain: str) -> bool:
    principal = current_user(request)
    if principal is None:
        return False
    if principal.get("type") == "admin":
        return True
    user_id = principal.get("id")
    if not isinstance(user_id, int):
        return False
    user = db.get(User, user_id)
    if user is not None and user.is_superadmin:
        return True
    return db.scalar(
        select(RolePermission.role_id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            UserRole.user_id == user_id,
            Permission.code == permission,
            RolePermission.domain == domain,
        )
    ) is not None


def require_permission(db: Session, request: Request, permission: str, domain: str) -> None:
    require_login(request)
    if not has_permission(db, request, permission, domain):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号没有此操作权限。")


def is_system_admin(db: Session, request: Request) -> bool:
    principal = current_user(request)
    if principal is None:
        return False
    if principal.get("type") == "admin":
        return True
    user_id = principal.get("id")
    if not isinstance(user_id, int):
        return False
    user = db.get(User, user_id)
    if user is not None and user.is_superadmin:
        return True
    return db.scalar(
        select(Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, Role.code == "system_admin")
    ) is not None


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
