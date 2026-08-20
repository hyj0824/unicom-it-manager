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
    Contact,
    Customer,
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
    for path in ["/contacts", "/scan-schedules", "/scripts", "/calls", "/sms"]:
        assert client.get(path).status_code == 200, path
    for path in ["/ledger", "/devices", "/daily-renewals", "/daily-recycles", "/missing"]:
        assert client.get(path).status_code == 403, path

    page = client.get("/contacts")
    assert "回访只读人员" in page.text
    assert 'href="/contacts"' in page.text
    assert 'href="/ledger"' not in page.text
    assert 'href="/admin/users"' not in page.text
    assert "新增人员" not in page.text
    assert "编辑通讯录人员" not in page.text

    for path in ["/contacts", "/scripts", "/scan-schedules", "/tasks/1/requeue", "/calls/1/feedback"]:
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
        "/contacts", data={"name": "联系人", "phone": "13800000003"}, follow_redirects=False
    ).status_code == 303
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
    assert client.post("/ledger/1/delete", data={}, follow_redirects=False).status_code == 403
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
    assert client.post("/devices/1/delete", data={}, follow_redirects=False).status_code == 403
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
    """创建话术/联系人，返回 id 供扫描配置等表单使用。"""
    with webdb() as db:
        db.add(Customer(name=name, notes=""))
        db.commit()
    client.post("/scripts", data={"title": f"话术-{name}", "body": f"内容-{name}"})
    client.post("/contacts", data={"name": f"联系人-{name}", "phone": "13800000000"})
    with webdb() as db:
        return {
            "customer_id": db.scalar(select(Customer.id).where(Customer.name == name)),
            "script_id": db.scalar(
                select(Script.id).where(Script.title == f"话术-{name}")
            ),
            "contact_id": db.scalar(
                select(Contact.id).where(Contact.name == f"联系人-{name}")
            ),
        }


def _scan_schedule_data(ids: dict[str, int], **overrides) -> dict:
    data = {
        "name": "每日到期维系扫描",
        "scan_type": "due_renewal",
        "script_id": str(ids["script_id"]),
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "lead_days": "14",
        "enabled": "on",
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
        ("POST", "/contacts"),
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
        "/contacts",
        "/scan-schedules",
        "/scripts",
        "/calls",
        "/ledger",
        "/devices",
        "/reviews",
        "/missing",
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
    resp = client.post("/scripts", data={"title": " ", "body": "内容"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/scripts?error=")
    assert "标题和话术内容必填" in client.get(resp.headers["location"]).text

    resp = client.post("/scripts", data={"title": "话术", "body": ""}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/scripts?error=")
    assert "标题和话术内容必填" in client.get(resp.headers["location"]).text

    resp = client.post(
        "/scripts", data={"title": "回访话术", "body": "您好。"}, follow_redirects=False
    )
    assert resp.status_code == 303
    resp = client.get("/scripts")
    assert "回访话术" in resp.text


def test_contact_form_validation(client: TestClient) -> None:
    login(client)
    resp = client.post("/contacts", data={"name": "", "phone": "13800000000"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/contacts?error=")
    assert "姓名和联系电话不能为空" in client.get(resp.headers["location"]).text

    resp = client.post("/contacts", data={"name": "张三", "phone": "123"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/contacts?error=")
    assert "电话格式不正确" in client.get(resp.headers["location"]).text

    resp = client.post(
        "/contacts", data={"name": "张三", "phone": "13800000000"}, follow_redirects=False
    )
    assert resp.status_code == 303


def test_scan_schedule_form_validation(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户丙")

    # 名称为空 → 列表页错误回显。
    resp = client.post("/scan-schedules", data=_scan_schedule_data(ids, name=" "), follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/scan-schedules?error=")
    assert "名称不能为空" in client.get(resp.headers["location"]).text

    # 无效 scan_type → 列表页错误回显。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, scan_type="monthly"), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/scan-schedules?error=")
    assert "扫描类型无效" in client.get(resp.headers["location"]).text

    # 无效 cron 表达式 → 列表页错误回显，提示明确。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, cron_expr="not a cron"), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/scan-schedules?error=")
    assert "Cron 表达式「not a cron」无效" in client.get(resp.headers["location"]).text

    # 无效时区 → 列表页错误回显。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, timezone="Mars/Olympus"), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/scan-schedules?error=")
    assert "时区「Mars/Olympus」无效" in client.get(resp.headers["location"]).text

    # 提前天数非法 → 列表页错误回显。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, lead_days="abc"), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/scan-schedules?error=")
    assert "提前天数必须是整数" in client.get(resp.headers["location"]).text

    # 不存在的 script_id → 列表页错误回显。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, script_id="999999"), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/scan-schedules?error=")
    assert "话术模板不存在" in client.get(resp.headers["location"]).text


def test_scan_schedule_valid_submission_edit_toggle_delete(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户丁")

    # 创建（可空话术 + 显式字段）。
    resp = client.post(
        "/scan-schedules",
        data=_scan_schedule_data(
            ids, name="设备回收扫描", scan_type="device_recycle", lead_days="7"
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        schedule = db.scalar(
            select(ScanSchedule).where(ScanSchedule.name == "设备回收扫描")
        )
        assert schedule is not None
        assert schedule.scan_type == "device_recycle"
        assert schedule.script_id == ids["script_id"]
        assert schedule.cron_expr == "0 9 * * *"
        assert schedule.timezone == "Asia/Shanghai"
        assert schedule.lead_days == 7
        assert schedule.enabled is True
        schedule_id = schedule.id

    # 列表页展示新配置。
    resp = client.get("/scan-schedules")
    assert resp.status_code == 200
    assert "设备回收扫描" in resp.text

    # 编辑。
    resp = client.post(
        f"/scan-schedules/{schedule_id}/edit",
        data=_scan_schedule_data(
            ids, name="改名后的扫描", cron_expr="30 8 * * *", timezone="UTC", lead_days="3"
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        schedule = db.get(ScanSchedule, schedule_id)
        assert schedule.name == "改名后的扫描"
        assert schedule.cron_expr == "30 8 * * *"
        assert schedule.timezone == "UTC"
        assert schedule.lead_days == 3

    # 停用/启用。
    resp = client.post(f"/scan-schedules/{schedule_id}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    with webdb() as db:
        assert db.get(ScanSchedule, schedule_id).enabled is False
    resp = client.post(f"/scan-schedules/{schedule_id}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    with webdb() as db:
        assert db.get(ScanSchedule, schedule_id).enabled is True

    # 删除。
    resp = client.post(f"/scan-schedules/{schedule_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    with webdb() as db:
        assert db.get(ScanSchedule, schedule_id) is None


def test_scan_schedule_cron_submission(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户戊")

    resp = client.post(
        "/scan-schedules",
        data=_scan_schedule_data(ids, cron_expr="0 9 * * *", script_id=""),
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with webdb() as db:
        schedule = db.scalar(
            select(ScanSchedule).where(ScanSchedule.name == "每日到期维系扫描")
        )
        assert schedule is not None
        assert schedule.cron_expr == "0 9 * * *"
        # 未选择话术 → 使用内置默认模板。
        assert schedule.script_id is None


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
