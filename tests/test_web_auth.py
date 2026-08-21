"""Web 路由测试：登录保护、表单校验、主要页面 smoke test。

使用 FastAPI TestClient + 临时 SQLite（alembic upgrade head），登录密码
使用 conftest 设置的测试值；不进入 lifespan，避免启动 Scheduler/Worker
或触碰真实数据库。测试环境配置见 tests/conftest.py。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import (
    Permission,
    Role,
    RolePermission,
    ScanSchedule,
    Script,
    User,
    UserRole,
)
from app.services.settings import is_scheduler_enabled
from app.services.users import hash_password

BASE_DIR = Path(__file__).resolve().parent.parent
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-password")


@pytest.fixture()
def webdb(tmp_path: Path):
    """临时 SQLite：alembic 升到 head，返回可调用的 session factory。"""
    db_path = tmp_path / "web.db"
    url = f"sqlite:///{db_path}"
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield factory
    engine.dispose()


@pytest.fixture()
def client(webdb):
    def override_get_db():
        db = webdb()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # 不用 with 进入 lifespan：不启动 Scheduler/Worker，也不校验真实库结构。
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.pop(get_db, None)


def login(client: TestClient) -> None:
    resp = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def test_admin_pages_require_system_permissions(client: TestClient, webdb) -> None:
    with webdb() as db:
        user = User(
            username="business-maintainer",
            display_name="业务维护人",
            password_hash=hash_password("business-password"),
            is_enabled=True,
        )
        db.add(user)
        db.flush()
        role = db.scalar(select(Role).where(Role.code == "business_maintainer"))
        assert role is not None
        db.add(UserRole(user_id=user.id, role_id=role.id))
        db.commit()
        user_id = user.id

    response = client.post(
        "/login",
        data={"username": "business-maintainer", "password": "business-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    for path in [
        "/admin/system",
        "/admin/system?tab=logs",
        "/admin/system?tab=settings",
        "/admin/users",
        "/admin/roles",
        "/admin/backups",
    ]:
        response = client.get(path)
        assert response.status_code == 403, path
        assert "没有此操作权限" in response.text
        assert "系统监控" not in response.text

    for path in [
        "/settings/worker",
        "/settings/scheduler",
        "/admin/backups",
        "/admin/users",
        f"/admin/users/{user_id}/edit",
        f"/admin/users/{user_id}/password",
        f"/admin/users/{user_id}/delete",
    ]:
        response = client.post(path, data={"enabled": "1"}, follow_redirects=False)
        assert response.status_code == 403, path
    response = client.get("/admin/backups/not-a-backup.zip/download")
    assert response.status_code == 403


def test_system_read_permission_only_allows_monitor_logs(client: TestClient, webdb) -> None:
    with webdb() as db:
        permission = db.scalar(select(Permission).where(Permission.code == "read"))
        assert permission is not None
        role = Role(code="system_observer", name="系统观察员", description="只读监控日志")
        user = User(
            username="system-observer",
            display_name="系统观察员",
            password_hash=hash_password("observer-password"),
            is_enabled=True,
        )
        db.add_all([role, user])
        db.flush()
        db.add_all(
            [
                RolePermission(
                    role_id=role.id, permission_id=permission.id, domain="system"
                ),
                UserRole(user_id=user.id, role_id=role.id),
            ]
        )
        db.commit()

    response = client.post(
        "/login",
        data={"username": "system-observer", "password": "observer-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/admin/system?tab=logs").status_code == 200
    assert client.get("/admin/system").status_code == 403
    assert client.get("/admin/system?tab=settings").status_code == 403
    assert client.get("/admin/users").status_code == 403


def _grant_domain_permissions(db, user: User, grants: list[tuple[str, str]]) -> None:
    role = Role(
        code=f"test-role-{user.username}",
        name=f"测试角色-{user.username}",
        description="权限审计测试",
    )
    db.add(role)
    db.flush()
    permissions = {
        permission.code: permission
        for permission in db.scalars(select(Permission)).all()
    }
    for code, domain in grants:
        db.add(
            RolePermission(
                role_id=role.id,
                permission_id=permissions[code].id,
                domain=domain,
            )
        )
    db.add(UserRole(user_id=user.id, role_id=role.id))


def test_domain_read_permissions_gate_pages_and_navigation(client: TestClient, webdb) -> None:
    with webdb() as db:
        user = User(
            username="callback-reader",
            real_name="回访只读人员",
            phone="13800000001",
            password_hash=hash_password("callback-password"),
            is_enabled=True,
        )
        db.add(user)
        db.flush()
        _grant_domain_permissions(db, user, [("read", "callback")])
        db.commit()

    response = client.post(
        "/login",
        data={"username": "callback-reader", "password": "callback-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    for path in ["/notification-settings", "/calls", "/sms"]:
        assert client.get(path).status_code == 200, path
    for path in ["/ledger", "/devices", "/daily-renewals", "/daily-recycles"]:
        assert client.get(path).status_code == 403, path

    page = client.get("/notification-settings")
    assert "回访只读人员" in page.text
    assert 'href="/notification-settings"' in page.text
    assert 'href="/ledger"' not in page.text
    assert 'href="/admin/users"' not in page.text

    for path in ["/scripts", "/scan-schedules", "/tasks/1/requeue", "/calls/1/feedback"]:
        assert client.post(path, data={}, follow_redirects=False).status_code == 403, path


def test_callback_write_permissions_are_independent(client: TestClient, webdb) -> None:
    with webdb() as db:
        user = User(
            username="callback-operator",
            real_name="回访操作员",
            phone="13800000002",
            password_hash=hash_password("operator-password"),
            is_enabled=True,
        )
        db.add(user)
        db.flush()
        _grant_domain_permissions(
            db,
            user,
            [
                ("read", "callback"),
                ("submit", "callback"),
                ("update_draft", "callback"),
                ("call_now", "callback"),
            ],
        )
        db.commit()

    client.post(
        "/login",
        data={"username": "callback-operator", "password": "operator-password"},
        follow_redirects=False,
    )
    assert client.post(
        "/scripts", data={"title": "回访话术", "body": "正文"}, follow_redirects=False
    ).status_code == 303


def test_default_auditor_roles_can_read_but_not_directly_write_ledgers(
    client: TestClient, webdb
) -> None:
    with webdb() as db:
        business_user = User(
            username="business-auditor-user",
            real_name="业务稽核员",
            phone="13800000005",
            password_hash=hash_password("business-auditor-password"),
            is_enabled=True,
        )
        network_user = User(
            username="network-auditor-user",
            real_name="网络稽核员",
            phone="13800000006",
            password_hash=hash_password("network-auditor-password"),
            is_enabled=True,
        )
        db.add_all([business_user, network_user])
        db.flush()
        business_role = db.scalar(select(Role).where(Role.code == "business_auditor"))
        network_role = db.scalar(select(Role).where(Role.code == "network_auditor"))
        assert business_role is not None
        assert network_role is not None
        db.add_all(
            [
                UserRole(user_id=business_user.id, role_id=business_role.id),
                UserRole(user_id=network_user.id, role_id=network_role.id),
            ]
        )
        db.commit()

    client.post(
        "/login",
        data={
            "username": "business-auditor-user",
            "password": "business-auditor-password",
        },
        follow_redirects=False,
    )
    ledger = client.get("/ledger")
    assert ledger.status_code == 200
    assert "新增业务" not in ledger.text
    assert 'data-edit-action="/ledger/' not in ledger.text
    assert client.post("/ledger", data={}, follow_redirects=False).status_code == 403
    assert client.post("/ledger/1/edit", data={}, follow_redirects=False).status_code == 403
    assert client.get("/devices").status_code == 403

    client.post("/logout")
    client.post(
        "/login",
        data={
            "username": "network-auditor-user",
            "password": "network-auditor-password",
        },
        follow_redirects=False,
    )
    devices = client.get("/devices")
    assert devices.status_code == 200
    assert "新增设备" not in devices.text
    assert 'data-edit-action="/devices/' not in devices.text
    assert client.post("/devices", data={}, follow_redirects=False).status_code == 403
    assert client.post("/devices/1/edit", data={}, follow_redirects=False).status_code == 403
    assert client.get("/ledger").status_code == 403


def test_legacy_superadmin_flag_does_not_grant_permissions(client: TestClient, webdb) -> None:
    with webdb() as db:
        user = User(
            username="legacy-superadmin",
            real_name="历史标记用户",
            phone="13800000004",
            password_hash=hash_password("legacy-password"),
            is_enabled=True,
            is_superadmin=True,
        )
        db.add(user)
        db.commit()

    client.post(
        "/login",
        data={"username": "legacy-superadmin", "password": "legacy-password"},
        follow_redirects=False,
    )
    assert client.get("/").status_code == 200
    assert client.get("/ledger").status_code == 403
    assert client.get("/admin/users").status_code == 403


def seed_basics(client: TestClient, webdb, name: str) -> dict[str, int]:
    """更新到期维系系统话术正文，返回其 id。"""
    with webdb() as db:
        script = db.scalar(
            select(Script).where(Script.role == "notification_due_renewal")
        )
        script.body = f"内容-{name}"
        db.commit()
        return {"script_id": script.id}


def _scan_schedule_data(ids: dict[str, int], **overrides) -> dict:
    data = {
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "lead_days": "14",
        "enabled": "on",
        "sms_enabled": "",
    }
    data.update({key: str(value) for key, value in overrides.items()})
    return data





# ---------------------------------------------------------------- 登录保护


def test_unauthenticated_requests_redirect_to_login(client: TestClient) -> None:
    for method, path in [
        ("GET", "/"),
        ("GET", "/scan-schedules"),
        ("GET", "/scripts"),
        ("GET", "/calls"),
        ("GET", "/admin/system"),
        ("GET", "/imports"),
        ("GET", "/password-change"),
        ("POST", "/password-change"),
        ("POST", "/scripts"),
        ("POST", "/scan-schedules"),
        ("POST", "/settings/scheduler"),
    ]:
        resp = client.request(method, path, follow_redirects=False)
        assert resp.status_code == 303, f"{method} {path} -> {resp.status_code}"
        assert resp.headers["location"] == "/login", f"{method} {path}"


def test_login_page_and_wrong_password(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200

    resp = client.post(
        "/login", data={"username": "admin", "password": "wrong-password"}
    )
    assert resp.status_code == 401
    assert "用户名或密码不正确" in resp.text

    # 登录失败后仍视为未登录。
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


def test_login_redirects_and_pages_render(client: TestClient) -> None:
    resp = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    # 已登录访问登录页重定向回首页。
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    # 主要页面 smoke：登录后全部 200。
    for path in [
        "/",
        "/scan-schedules",
        "/scripts",
        "/calls",
        "/ledger",
        "/devices",
        "/reviews",
        "/admin/users",
        "/admin/roles",
        "/admin/system",
        "/admin/system?tab=logs",
        "/admin/system?tab=settings",
        "/imports",
    ]:
        resp = client.get(path)
        assert resp.status_code == 200, f"GET {path} -> {resp.status_code}"

    # 登出后回到未登录状态。
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


# ---------------------------------------------------------------- 表单校验


def test_script_form_validation(client: TestClient) -> None:
    login(client)
    # 话术已收敛为三种系统模板，不支持新增。
    resp = client.post("/scripts", data={"title": "话术", "body": "内容"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "系统话术不支持新增" in client.get(resp.headers["location"]).text



def test_scan_schedule_form_validation(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户丙")
    with webdb() as db:
        schedule_id = db.scalar(select(ScanSchedule).where(ScanSchedule.scan_type == "due_renewal")).id

    # 不支持新增。
    resp = client.post("/scan-schedules", data=_scan_schedule_data(ids), follow_redirects=False)
    assert resp.status_code == 303
    assert "扫描配置固定为三类" in client.get(resp.headers["location"]).text

    # 编辑：无效 cron → 错误回显。
    resp = client.post(
        f"/scan-schedules/{schedule_id}/edit",
        data=_scan_schedule_data(ids, cron_expr="not a cron"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Cron 表达式「not a cron」无效" in client.get(resp.headers["location"]).text

    # 编辑：无效时区 → 错误回显。
    resp = client.post(
        f"/scan-schedules/{schedule_id}/edit",
        data=_scan_schedule_data(ids, timezone="Mars/Olympus"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "时区「Mars/Olympus」无效" in client.get(resp.headers["location"]).text

    # 编辑：提前天数非法 → 错误回显。
    resp = client.post(
        f"/scan-schedules/{schedule_id}/edit",
        data=_scan_schedule_data(ids, lead_days="abc"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "提前天数必须是整数" in client.get(resp.headers["location"]).text



def test_scan_schedule_edit_toggle(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户丁")
    with webdb() as db:
        schedule = db.scalar(select(ScanSchedule).where(ScanSchedule.scan_type == "due_renewal"))
        schedule_id = schedule.id

    # 编辑生效。
    resp = client.post(
        f"/scan-schedules/{schedule_id}/edit",
        data=_scan_schedule_data(ids, cron_expr="0 18 * * *", lead_days="7", sms_enabled="on"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        schedule = db.get(ScanSchedule, schedule_id)
        assert schedule.cron_expr == "0 18 * * *"
        assert schedule.lead_days == 7
        assert schedule.sms_enabled is True
        assert schedule.enabled is True

    # 启停切换。
    resp = client.post(f"/scan-schedules/{schedule_id}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    with webdb() as db:
        assert db.get(ScanSchedule, schedule_id).enabled is False

    # 不支持删除。
    resp = client.post(f"/scan-schedules/{schedule_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "不支持删除" in client.get(resp.headers["location"]).text



def test_scan_schedule_cron_submission(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户戊")
    with webdb() as db:
        schedule_id = db.scalar(select(ScanSchedule).where(ScanSchedule.scan_type == "due_renewal")).id

    resp = client.post(
        f"/scan-schedules/{schedule_id}/edit",
        data=_scan_schedule_data(ids, cron_expr="30 9 * * *"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        schedule = db.get(ScanSchedule, schedule_id)
        assert schedule.cron_expr == "30 9 * * *"



# ---------------------------------------------------------------- 暂停调度


def test_scheduler_toggle_through_web(client: TestClient, webdb) -> None:
    login(client)

    resp = client.post(
        "/settings/scheduler", data={"enabled": "0"}, follow_redirects=False
    )
    assert resp.status_code == 303
    with webdb() as db:
        assert is_scheduler_enabled(db) is False

    resp = client.post(
        "/settings/scheduler", data={"enabled": "1"}, follow_redirects=False
    )
    assert resp.status_code == 303
    with webdb() as db:
        assert is_scheduler_enabled(db) is True

    # 未登录不能切换。
    client.post("/logout")
    resp = client.post("/settings/scheduler", data={"enabled": "0"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------- 用户实名与手机


def test_user_create_validates_real_name_and_phone(client: TestClient, webdb) -> None:
    login(client)
    base = {
        "username": "ops-user",
        "password": "ops-password-1",
        "real_name": "张三",
        "phone": "13800000000",
    }
    # 缺实名 → 400。
    resp = client.post("/admin/users", data={**base, "real_name": ""})
    assert resp.status_code == 400
    assert "实名必填" in resp.text
    # 非管理员缺手机 → 400。
    resp = client.post("/admin/users", data={**base, "phone": ""})
    assert resp.status_code == 400
    assert "手机号必填" in resp.text
    # 手机格式错误 → 400。
    resp = client.post("/admin/users", data={**base, "phone": "123"})
    assert resp.status_code == 400
    assert "手机号格式不正确" in resp.text
    # 校验失败时用户未落库。
    with webdb() as db:
        assert db.scalar(select(User).where(User.username == "ops-user")) is None


@pytest.mark.parametrize("phone", ["13800000000", "+8613800000000"])
def test_user_create_and_edit_persist_fields(client: TestClient, webdb, phone: str) -> None:
    login(client)
    resp = client.post(
        "/admin/users",
        data={
            "username": "ops-user",
            "password": "ops-password-1",
            "real_name": "张三",
            "phone": phone,
            "display_name": "不再使用的名称",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        user = db.scalar(select(User).where(User.username == "ops-user"))
        assert user is not None
        assert user.real_name == "张三"
        assert user.phone == phone
        assert user.display_name == ""
        user_id = user.id

    # 列表页显示实名/手机；表单明确实名是页面展示名称。
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert "张三" in resp.text
    assert phone in resp.text
    assert "实名（页面展示名称）" in resp.text
    assert "显示名称" not in resp.text

    # 编辑实名/手机 → 落库。
    resp = client.post(
        f"/admin/users/{user_id}/edit",
        data={"real_name": "李四", "phone": "13900000001", "enabled": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        user = db.get(User, user_id)
        assert user.real_name == "李四"
        assert user.phone == "13900000001"

    # 编辑同样校验：缺实名 / 手机格式错误 → 400 且字段不变。
    resp = client.post(
        f"/admin/users/{user_id}/edit",
        data={"real_name": "", "phone": "13900000001"},
    )
    assert resp.status_code == 400
    assert "实名必填" in resp.text
    resp = client.post(
        f"/admin/users/{user_id}/edit",
        data={"real_name": "李四", "phone": "bad-phone"},
    )
    assert resp.status_code == 400
    assert "手机号格式不正确" in resp.text
    with webdb() as db:
        user = db.get(User, user_id)
        assert user.real_name == "李四"
        assert user.phone == "13900000001"


def test_legacy_superadmin_user_is_read_only(client: TestClient, webdb) -> None:
    login(client)
    with webdb() as db:
        superadmin = User(
            username="super-admin",
            real_name="系统管理员",
            phone="",
            password_hash=hash_password("sa-password-1"),
            is_enabled=True,
            is_superadmin=True,
        )
        db.add(superadmin)
        db.commit()
        admin_id = superadmin.id

    # 旧 is_superadmin 数据只读，不能通过用户管理修改。
    resp = client.post(
        f"/admin/users/{admin_id}/edit",
        data={"real_name": "系统管理员", "phone": "", "enabled": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    page = client.get("/admin/users")
    assert "内置管理员只读" in page.text
    assert f"edit_id={admin_id}" not in page.text


def test_admin_user_delete_removes_account_and_role_links(client: TestClient, webdb) -> None:
    login(client)
    with webdb() as db:
        role = Role(code="del-role", name="删除测试角色", description="", is_preset=False)
        db.add(role)
        db.flush()
        user = User(
            username="to-delete",
            real_name="待删除用户",
            phone="13900000002",
            password_hash=hash_password("to-delete-pass"),
            is_enabled=True,
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id))
        db.commit()
        user_id = user.id
        role_id = role.id

    resp = client.post(f"/admin/users/{user_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    with webdb() as db:
        assert db.get(User, user_id) is None
        assert db.scalar(select(UserRole).where(UserRole.user_id == user_id)) is None
        # 角色本身保留，只是解绑。
        assert db.get(Role, role_id) is not None


def test_admin_user_delete_refuses_superadmin_and_self(client: TestClient, webdb) -> None:
    login(client)
    with webdb() as db:
        superadmin = User(
            username="sa-del",
            real_name="超管",
            phone="",
            password_hash=hash_password("sa-del-pass"),
            is_enabled=True,
            is_superadmin=True,
        )
        db.add(superadmin)
        db.commit()
        super_id = superadmin.id
        manager = User(
            username="manager-1",
            real_name="管理员甲",
            phone="13900000003",
            password_hash=hash_password("mgr-pass-1"),
            is_enabled=True,
        )
        db.add(manager)
        db.flush()
        role = Role(code=f"mgr-role-{manager.id}", name="管理角色", description="")
        db.add(role)
        db.flush()
        permission = db.scalar(select(Permission).where(Permission.code == "manage_users"))
        db.add(
            RolePermission(
                role_id=role.id, permission_id=permission.id, domain="system"
            )
        )
        db.add(UserRole(user_id=manager.id, role_id=role.id))
        db.commit()
        manager_id = manager.id

    # 内置超管账号不可删除。
    resp = client.post(f"/admin/users/{super_id}/delete", follow_redirects=False)
    assert resp.status_code == 403

    # 普通管理员不能删除自己（可改为停用）。
    client.post("/logout", follow_redirects=False)
    resp = client.post(
        "/login",
        data={"username": "manager-1", "password": "mgr-pass-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = client.post(f"/admin/users/{manager_id}/delete", follow_redirects=False)
    assert resp.status_code == 403
    with webdb() as db:
        assert db.get(User, manager_id) is not None
