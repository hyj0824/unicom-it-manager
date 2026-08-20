"""台账导入应用时按职责自动创建登录账号。

客户经理（业务 payload `contacts.account_manager`）建号并绑定
`business_maintainer` 角色；网络维护责任人（设备 payload 的
`maintenance_name` / `maintenance_phone`）建号并绑定 `network_maintainer`
角色。

建号规则：用户名 = 手机号（全局唯一），初始密码为随机生成
（`secrets.token_urlsafe(9)`，只以哈希入库，日志不记录明文），首次登录
强制改密；管理员可在用户管理重置密码。已有同用户名或同手机号的账号时
跳过，不重复建号（同名/同号联系人复用已有账号）。暂存/导入阶段不建号，
只在审核应用（`reviews.apply_change_set`）时调用。
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Role, User, UserRole
from .users import hash_password

logger = logging.getLogger(__name__)


def _provision_user(db: Session, name: str, phone: str, role_code: str) -> bool:
    """按姓名/手机号建号并绑定角色；返回是否新建。

    已有同 username 或同 phone 的用户时跳过并记日志；角色 code 不存在时
    仍建号但不绑角色（种子角色正常存在，缺失属于异常配置）。
    """
    existing = db.scalars(
        select(User).where(or_(User.username == phone, User.phone == phone))
    ).first()
    if existing is not None:
        logger.info(
            "自动建号跳过：手机号 %s 已有账号 %s，不重复建号",
            phone,
            existing.username,
        )
        return False
    # 随机初始密码只存哈希；姓名缺失时以手机号兜底，避免空实名。
    user = User(
        username=phone,
        real_name=name or phone,
        phone=phone,
        password_hash=hash_password(secrets.token_urlsafe(9)),
        is_enabled=True,
        is_superadmin=False,
        auto_provisioned=True,
        force_password_change=True,
    )
    db.add(user)
    db.flush()
    role = db.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        logger.warning("自动建号 %s：角色 %s 不存在，账号已建但未绑定角色", phone, role_code)
    else:
        db.add(UserRole(user_id=user.id, role_id=role.id))
        logger.info("自动建号 %s（%s）：绑定角色 %s", name or phone, phone, role_code)
    return True


def provision_users_from_business_payload(db: Session, payload: dict) -> int:
    """业务 payload：客户经理（contacts.account_manager）→ business_maintainer。"""
    manager = (payload.get("contacts") or {}).get("account_manager") or {}
    name = str(payload.get("account_manager_name", manager.get("name", "")) or "").strip()
    phone = str(payload.get("account_manager_phone", manager.get("phone", "")) or "").strip()
    if not phone:
        return 0
    return int(_provision_user(db, name, phone, "business_maintainer"))


def provision_users_from_device_payload(db: Session, payload: dict) -> int:
    """设备 payload：网络维护责任人（device.maintenance_*）→ network_maintainer。"""
    data = payload.get("device") or {}
    name = str(data.get("maintenance_name", "")).strip()
    phone = str(data.get("maintenance_phone", "")).strip()
    if not phone:
        return 0
    return int(_provision_user(db, name, phone, "network_maintainer"))
