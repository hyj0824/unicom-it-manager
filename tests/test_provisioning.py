"""自动建号与首次登录强制改密测试。

覆盖：台账导入应用时按职责自动建号（客户经理→business_maintainer、
网络维护责任人→network_maintainer）、幂等跳过、空手机不建号、登录后
302 到 /password-change、改密流程与旧密码失效、管理员重置密码后强制改密、
导入→审核→应用集成链路自动建号、用户管理页标记展示。

全部使用临时 SQLite（alembic upgrade head）+ FastAPI TestClient，不进入
lifespan，不接触真实数据库与硬件。
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import ChangeSet, ImportBatch, Role, User, UserRole
from app.services.imports import LEDGER_COLUMNS
from app.services.provisioning import (
    provision_users_from_business_payload,
    provision_users_from_device_payload,
)
from app.services.users import hash_password, verify_password

BASE_DIR = Path(__file__).resolve().parent.parent
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-password")


@pytest.fixture()
def webdb(tmp_path: Path):
    db_path = tmp_path / "provisioning.db"
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
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def web_client(webdb, monkeypatch, tmp_path: Path):
    """带导入上传落盘重定向的客户端（同 test_import_web.py）。"""
    import app.main as main_module

    def override_get_db():
        db = webdb()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(main_module, "BASE_DIR", tmp_path)
    (tmp_path / "data" / "imports").mkdir(parents=True)
    client = TestClient(app)
    yield client, webdb
    app.dependency_overrides.pop(get_db, None)


def _login(client: TestClient, username: str = "admin", password: str = ADMIN_PASSWORD) -> None:
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def _bound_role(db, user_id: int) -> str | None:
    role_id = db.scalar(select(UserRole.role_id).where(UserRole.user_id == user_id))
    if role_id is None:
        return None
    return db.scalar(select(Role.code).where(Role.id == role_id))


# ---------------------------------------------------------------- 建号规则


def test_business_payload_creates_account_manager_account(webdb) -> None:
    payload = {
        "contacts": {
            "developer": {"name": "张三", "phone": "13800000000"},
            "account_manager": {"name": "李四", "phone": "13900000001"},
        },
    }
    with webdb() as db:
        created = provision_users_from_business_payload(db, payload)
        db.commit()
        assert created == 1

        user = db.scalar(select(User).where(User.username == "13900000001"))
        assert user is not None
        assert user.real_name == "李四"
        assert user.display_name == "李四"
        assert user.phone == "13900000001"
        assert user.auto_provisioned is True
        assert user.force_password_change is True
        assert user.is_enabled is True
        assert user.is_superadmin is False
        # 初始随机密码只存 PBKDF2 哈希，无法验证为已知值。
        assert user.password_hash.startswith("pbkdf2_sha256$200000$")
        assert not verify_password("", user.password_hash)
        assert _bound_role(db, user.id) == "business_maintainer"
        # 发展人不建号（本版只建客户经理）。
        assert db.scalar(select(User).where(User.username == "13800000000")) is None


def test_device_payload_creates_network_maintainer_account(webdb) -> None:
    payload = {
        "service_number": "848DIA000001",
        "device": {
            "device_code": "21000001",
            "maintenance_name": "王五",
            "maintenance_phone": "13700000002",
        },
    }
    with webdb() as db:
        created = provision_users_from_device_payload(db, payload)
        db.commit()
        assert created == 1

        user = db.scalar(select(User).where(User.username == "13700000002"))
        assert user is not None
        assert user.real_name == "王五"
        assert user.auto_provisioned is True
        assert user.force_password_change is True
        assert _bound_role(db, user.id) == "network_maintainer"


def test_provisioning_is_idempotent(webdb) -> None:
    payload = {
        "contacts": {"account_manager": {"name": "李四", "phone": "13900000001"}},
    }
    with webdb() as db:
        assert provision_users_from_business_payload(db, payload) == 1
        db.commit()
        # 同一 payload 再次应用不重复建号。
        assert provision_users_from_business_payload(db, payload) == 0
        db.commit()
        assert db.scalar(select(func.count(User.id))) == 1

        # 同手机号但用户名不同（人工已有账号）也跳过，不撞唯一约束。
        existing = db.scalar(select(User).where(User.username == "13900000001"))
        existing.username = "ops-renamed"
        db.commit()
        assert provision_users_from_business_payload(db, payload) == 0


def test_empty_phone_skips_provisioning(webdb) -> None:
    business_payload = {
        "contacts": {"account_manager": {"name": "李四", "phone": ""}},
    }
    device_payload = {
        "device": {"device_code": "21000001", "maintenance_name": "王五", "maintenance_phone": "  "},
    }
    with webdb() as db:
        assert provision_users_from_business_payload(db, business_payload) == 0
        assert provision_users_from_device_payload(db, device_payload) == 0
        # 完全没有 phone 键也不建号。
        assert provision_users_from_business_payload(
            db, {"contacts": {"account_manager": {"name": "李四"}}}
        ) == 0
        assert provision_users_from_device_payload(db, {"device": {}}) == 0
        db.commit()
        assert db.scalar(select(func.count(User.id))) == 0


# ---------------------------------------------------------------- 登录强制改密


def _create_forced_user(webdb, username: str = "13900000001", password: str = "random-initial") -> None:
    with webdb() as db:
        db.add(
            User(
                username=username,
                real_name="李四",
                phone=username,
                display_name="李四",
                password_hash=hash_password(password),
                is_enabled=True,
                auto_provisioned=True,
                force_password_change=True,
            )
        )
        db.commit()


def test_forced_user_login_redirects_to_password_change(client: TestClient, webdb) -> None:
    _create_forced_user(webdb)

    resp = client.post(
        "/login",
        data={"username": "13900000001", "password": "random-initial"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/password-change"

    # 改密页需要登录；未登录访问重定向到登录页。
    resp = client.get("/password-change")
    assert resp.status_code == 200
    assert "首次登录修改密码" in resp.text

    client.post("/logout")
    resp = client.get("/password-change", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_password_change_validation_and_success(client: TestClient, webdb) -> None:
    _create_forced_user(webdb)
    _login(client, "13900000001", "random-initial")

    # 当前密码错误 → 400。
    resp = client.post(
        "/password-change",
        data={
            "old_password": "wrong-password",
            "new_password": "new-password-1",
            "confirm_password": "new-password-1",
        },
    )
    assert resp.status_code == 400
    assert "当前密码不正确" in resp.text

    # 新密码过短 → 400。
    resp = client.post(
        "/password-change",
        data={
            "old_password": "random-initial",
            "new_password": "short",
            "confirm_password": "short",
        },
    )
    assert resp.status_code == 400
    assert "新密码至少 8 位" in resp.text

    # 两次输入不一致 → 400。
    resp = client.post(
        "/password-change",
        data={
            "old_password": "random-initial",
            "new_password": "new-password-1",
            "confirm_password": "new-password-2",
        },
    )
    assert resp.status_code == 400
    assert "两次输入的新密码不一致" in resp.text

    # 校验失败期间状态不变。
    with webdb() as db:
        assert db.scalar(select(User).where(User.username == "13900000001")).force_password_change is True

    # 成功改密 → 302 回首页，标志清除。
    resp = client.post(
        "/password-change",
        data={
            "old_password": "random-initial",
            "new_password": "new-password-1",
            "confirm_password": "new-password-1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    with webdb() as db:
        user = db.scalar(select(User).where(User.username == "13900000001"))
        assert user.force_password_change is False
        assert verify_password("new-password-1", user.password_hash)

    # 改密后可正常访问首页。
    assert client.get("/").status_code == 200

    # 退出后旧密码失效；新密码登录直接进首页（不再跳改密）。
    client.post("/logout")
    resp = client.post(
        "/login",
        data={"username": "13900000001", "password": "random-initial"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    resp = client.post(
        "/login",
        data={"username": "13900000001", "password": "new-password-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_regular_login_without_force_flag_still_goes_home(client: TestClient, webdb) -> None:
    with webdb() as db:
        db.add(
            User(
                username="normal-user",
                real_name="普通用户",
                phone="13600000003",
                password_hash=hash_password("normal-pass-1"),
                is_enabled=True,
            )
        )
        db.commit()
    resp = client.post(
        "/login",
        data={"username": "normal-user", "password": "normal-pass-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_admin_reset_password_forces_change(client: TestClient, webdb) -> None:
    _login(client)
    resp = client.post(
        "/admin/users",
        data={
            "username": "ops-user",
            "password": "initial-pass-1",
            "real_name": "张三",
            "phone": "13800000000",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        user = db.scalar(select(User).where(User.username == "ops-user"))
        assert user is not None
        assert user.force_password_change is False  # 人工建号不带强制标记
        user_id = user.id

    # 管理员重置密码 → 被重置账号下次登录强制改密。
    resp = client.post(
        f"/admin/users/{user_id}/password",
        data={"password": "reset-pass-123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with webdb() as db:
        user = db.get(User, user_id)
        assert verify_password("reset-pass-123", user.password_hash)
        assert user.force_password_change is True

    client.post("/logout")
    resp = client.post(
        "/login",
        data={"username": "ops-user", "password": "reset-pass-123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/password-change"


def test_admin_users_page_shows_marks(client: TestClient, webdb) -> None:
    _login(client)
    client.post(
        "/admin/users",
        data={
            "username": "manual-user",
            "password": "manual-pass-1",
            "real_name": "王五",
            "phone": "13600000003",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    with webdb() as db:
        provision_users_from_business_payload(
            db, {"contacts": {"account_manager": {"name": "李四", "phone": "13900000001"}}}
        )
        db.commit()

    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert "自动建号" in resp.text
    assert "需改密" in resp.text
    assert "账号来源" in resp.text
    assert "改密状态" in resp.text


# ---------------------------------------------------------------- 导入审核应用集成


def _ledger_row(**overrides: str) -> list[str]:
    values = {
        "号码": "848DIAWEB0001",
        "户名": "Web 测试客户",
        "县分": "汉滨",
        "网格": "汉滨要客",
        "服务状态": "正常开机",
        "入网时间": "20220101",
        "协议到期时间": "20301231",
        "业务类型": "宽带业务",
        "渠道名称": "Web 测试渠道",
        "发展人": "张三",
        "发展人联系电话": "13800000000",
        "客户经理": "李四",
        "客户经理联系电话": "13900000001",
        "网络维护责任人": "王五",
        "网络维护责任人联系电话": "13700000002",
        "设备属性": "资产类",
        "设备编码": "210000WEB01",
        "资产原值或物资购置价格": "1200",
        "设备及物资类型": "光猫",
        "设备厂家+型号": "测试V1",
        "设备放置地点": "机房",
        "设备是否已回收": "否",
        "设备未回收原因": "在用",
    }
    values.update(overrides)
    return [values.get(column, "") for column in LEDGER_COLUMNS]


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["业务信息"] + [None] * 12 + ["设备信息"] + [None] * 10)
    sheet.append(LEDGER_COLUMNS)
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _upload(client: TestClient, content: bytes) -> int:
    response = client.post(
        "/imports/upload",
        files={
            "file": (
                "web-ledger.xlsx",
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_import_apply_provisions_accounts(web_client) -> None:
    client, webdb = web_client
    _login(client)
    batch_id = _upload(client, _xlsx_bytes([_ledger_row()]))

    submit = client.post(f"/imports/{batch_id}/submit", follow_redirects=False)
    assert submit.status_code == 303, submit.text
    with webdb() as db:
        change_sets = db.scalars(
            select(ChangeSet).where(ChangeSet.import_batch_id == batch_id).order_by(ChangeSet.domain)
        ).all()
        assert {change.domain for change in change_sets} == {"business", "network"}
        business_id = next(change.id for change in change_sets if change.domain == "business")
        network_id = next(change.id for change in change_sets if change.domain == "network")
        # 审核应用前（暂存/提交阶段）不建号。
        assert db.scalar(select(User).where(User.phone == "13900000001")) is None

    for change_id in (business_id, network_id):
        response = client.post(
            f"/reviews/{change_id}/decision",
            data={"decision": "approved", "self_review_confirmed": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
    for change_id in (business_id, network_id):
        response = client.post(f"/reviews/{change_id}/apply", follow_redirects=False)
        assert response.status_code == 303, response.text

    with webdb() as db:
        batch = db.get(ImportBatch, batch_id)
        assert batch.status == "applied"

        manager = db.scalar(select(User).where(User.username == "13900000001"))
        maintainer = db.scalar(select(User).where(User.username == "13700000002"))
        assert manager is not None
        assert maintainer is not None
        assert manager.auto_provisioned and maintainer.auto_provisioned
        assert manager.force_password_change and maintainer.force_password_change
        assert _bound_role(db, manager.id) == "business_maintainer"
        assert _bound_role(db, maintainer.id) == "network_maintainer"
        # 发展人不建号。
        assert db.scalar(select(User).where(User.username == "13800000000")) is None

        # 自动建号账号在用户管理页有标记。
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        assert "自动建号" in resp.text
        assert "需改密" in resp.text
