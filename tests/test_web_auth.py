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
    resp = client.post("/scripts", data={"title": " ", "body": "内容"})
    assert resp.status_code == 400
    assert "标题和话术内容必填" in resp.text

    resp = client.post("/scripts", data={"title": "话术", "body": ""})
    assert resp.status_code == 400
    assert "标题和话术内容必填" in resp.text

    resp = client.post(
        "/scripts", data={"title": "回访话术", "body": "您好。"}, follow_redirects=False
    )
    assert resp.status_code == 303
    resp = client.get("/scripts")
    assert "回访话术" in resp.text


def test_contact_form_validation(client: TestClient) -> None:
    login(client)
    resp = client.post("/contacts", data={"name": "", "phone": "13800000000"})
    assert resp.status_code == 400
    assert "姓名和联系电话不能为空" in resp.text

    resp = client.post("/contacts", data={"name": "张三", "phone": "123"})
    assert resp.status_code == 400
    assert "电话格式不正确" in resp.text

    resp = client.post(
        "/contacts", data={"name": "张三", "phone": "13800000000"}, follow_redirects=False
    )
    assert resp.status_code == 303


def test_scan_schedule_form_validation(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户丙")

    # 名称为空 → 400。
    resp = client.post("/scan-schedules", data=_scan_schedule_data(ids, name=" "))
    assert resp.status_code == 400
    assert "名称不能为空" in resp.text

    # 无效 scan_type → 400。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, scan_type="monthly")
    )
    assert resp.status_code == 400
    assert "扫描类型无效" in resp.text

    # 无效 cron 表达式 → 400，提示明确。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, cron_expr="not a cron")
    )
    assert resp.status_code == 400
    assert "Cron 表达式「not a cron」无效" in resp.text

    # 无效时区 → 400。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, timezone="Mars/Olympus")
    )
    assert resp.status_code == 400
    assert "时区「Mars/Olympus」无效" in resp.text

    # 提前天数非法 → 400。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, lead_days="abc")
    )
    assert resp.status_code == 400
    assert "提前天数必须是整数" in resp.text

    # 不存在的 script_id → 400。
    resp = client.post(
        "/scan-schedules", data=_scan_schedule_data(ids, script_id="999999")
    )
    assert resp.status_code == 400
    assert "话术模板不存在" in resp.text


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
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        user = db.scalar(select(User).where(User.username == "ops-user"))
        assert user is not None
        assert user.real_name == "张三"
        assert user.phone == phone
        user_id = user.id

    # 列表页显示实名/手机；表单含系统管理员可不填的提示。
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert "张三" in resp.text
    assert phone in resp.text
    assert "系统管理员可不填" in resp.text

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


def test_superadmin_user_may_edit_without_phone(client: TestClient, webdb) -> None:
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

    # 系统管理员可留空手机；实名仍必填。
    resp = client.post(
        f"/admin/users/{admin_id}/edit",
        data={"real_name": "系统管理员", "phone": "", "enabled": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = client.post(
        f"/admin/users/{admin_id}/edit",
        data={"real_name": "", "phone": ""},
    )
    assert resp.status_code == 400
    assert "实名必填" in resp.text
    # 超管填了手机也必须格式正确。
    resp = client.post(
        f"/admin/users/{admin_id}/edit",
        data={"real_name": "系统管理员", "phone": "123"},
    )
    assert resp.status_code == 400
    assert "手机号格式不正确" in resp.text
    with webdb() as db:
        user = db.get(User, admin_id)
        assert user.phone == ""
        assert user.real_name == "系统管理员"
