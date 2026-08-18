"""Web 路由测试：登录保护、表单校验、主要页面 smoke test。

使用 FastAPI TestClient + 临时 SQLite（alembic upgrade head），登录密码
使用 conftest 设置的测试值；不进入 lifespan，避免启动 Scheduler/Worker
或触碰真实数据库。测试环境配置见 tests/conftest.py。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
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
    CallTask,
    CallbackPlan,
    Contact,
    Customer,
    Permission,
    Role,
    RolePermission,
    Script,
    User,
    UserRole,
)
from app.services import plans as plan_service
from app.services.settings import is_scheduler_enabled
from app.services.users import hash_password

BASE_DIR = Path(__file__).resolve().parent.parent
UTC = timezone.utc
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
    """创建客户主体/话术/联系人，返回 id 供计划表单使用。"""
    # 客户主体由台账导入审核链路维护，Web 不再提供客户表单；测试直接建主数据。
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


# ---------------------------------------------------------------- 登录保护


def test_unauthenticated_requests_redirect_to_login(client: TestClient) -> None:
    for method, path in [
        ("GET", "/"),
        ("GET", "/plans"),
        ("GET", "/scripts"),
        ("GET", "/calls"),
        ("GET", "/admin/system"),
        ("GET", "/imports"),
        ("POST", "/contacts"),
        ("POST", "/scripts"),
        ("POST", "/plans"),
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
        "/plans",
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


def test_plan_form_validation(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户丙")
    base = {
        "customer_id": str(ids["customer_id"]),
        "script_id": str(ids["script_id"]),
        "contact_id": str(ids["contact_id"]),
        "trigger_type": "once",
        "timezone": "Asia/Shanghai",
    }

    # 未选择联系人（占位值 0 视为未选择）→ 400，提示选择负责人。
    resp = client.post("/plans", data={**base, "contact_id": "0", "run_at": "2026-01-15T10:30"})
    assert resp.status_code == 400
    assert "请选择拨打负责人" in resp.text

    # 联系人的联系电话为空 → 400。
    with webdb() as db:
        phone_less = Contact(name="无电话", phone="")
        db.add(phone_less)
        db.commit()
        contact_id = phone_less.id
    resp = client.post(
        "/plans", data={**base, "contact_id": str(contact_id), "run_at": "2026-01-15T10:30"}
    )
    assert resp.status_code == 400
    assert "有效联系电话" in resp.text

    # once 计划缺少 run_at → 400。
    resp = client.post("/plans", data={**base, "run_at": ""})
    assert resp.status_code == 400
    assert "必须填写执行时间" in resp.text

    # 无效 trigger_type → 400。
    resp = client.post(
        "/plans",
        data={
            **base,
            "trigger_type": "monthly",
            "run_at": "2026-01-15T10:30",
        },
    )
    assert resp.status_code == 400
    assert "trigger_type" in resp.text

    # 无效 cron 表达式 → 400。
    resp = client.post(
        "/plans",
        data={
            **base,
            "trigger_type": "cron",
            "cron_expr": "not a cron",
        },
    )
    assert resp.status_code == 400


def test_plan_valid_submission_and_call_now(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户丁")

    # 创建 once 计划（上海 2026-01-15 10:30）。
    resp = client.post(
        "/plans",
        data={
            "customer_id": str(ids["customer_id"]),
            "script_id": str(ids["script_id"]),
            "contact_id": str(ids["contact_id"]),
            "trigger_type": "once",
            "run_at": "2026-01-15T10:30",
            "timezone": "Asia/Shanghai",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = client.get("/plans")
    assert resp.status_code == 200

    with webdb() as db:
        plan = db.scalar(
            select(CallbackPlan).where(CallbackPlan.customer_id == ids["customer_id"])
        )
        assert plan is not None
        assert plan.trigger_type == "once"
        # 上海 2026-01-15 10:30 = UTC 02:30。
        assert plan_service.as_utc(plan.next_run_at) == datetime(2026, 1, 15, 2, 30, tzinfo=UTC)

        # 立即拨打：生成 queued 手动任务，不改变计划的下一次执行时间。
        before = plan.next_run_at
        resp = client.post(f"/plans/{plan.id}/call-now", follow_redirects=False)
        assert resp.status_code == 303
        db.expire_all()
        plan = db.get(CallbackPlan, plan.id)
        assert plan.next_run_at == before

    with webdb() as db:
        task = db.scalar(select(CallTask).where(CallTask.plan_id == plan.id))
        assert task is not None
        assert task.status == "queued"
        assert task.source == "manual"
        assert task.dial_number == "13800000000"

    # 通话列表页展示任务。
    resp = client.get("/calls")
    assert resp.status_code == 200


def test_plan_cron_submission(client: TestClient, webdb) -> None:
    login(client)
    ids = seed_basics(client, webdb, "客户戊")

    resp = client.post(
        "/plans",
        data={
            "customer_id": str(ids["customer_id"]),
            "script_id": str(ids["script_id"]),
            "contact_id": str(ids["contact_id"]),
            "trigger_type": "cron",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with webdb() as db:
        plan = db.scalar(
            select(CallbackPlan).where(CallbackPlan.customer_id == ids["customer_id"])
        )
        assert plan is not None
        assert plan.trigger_type == "cron"
        assert plan.cron_expr == "0 9 * * *"
        assert plan_service.as_utc(plan.next_run_at) > datetime.now(timezone.utc)


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
