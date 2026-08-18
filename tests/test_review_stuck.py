from __future__ import annotations

"""审核卡单提醒扫描（app/services/scans.py run_review_stuck_scan）单元测试。

覆盖：
- 阈值判定：阈值内（含边界）不通知、超阈值通知；
- app_settings review_stuck_hours 自定义阈值生效；
- 通知对象：只有启用且绑定角色拥有 review 权限、且有手机的用户被通知；
- 同一卡单对多个审核人员各生成一条任务；
- 同一天同一 change_set 去重（幂等）；
- 无业务关联的卡单跳过（无变更项 / entity_id 为空 / 非 submitted 状态）；
- run_scan_for_schedule 分发成功（last_run_at / last_error）。

使用临时 SQLite + alembic head（种子数据含 review 权限）；TTS_PROVIDER=none
（conftest）时不生成音频。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    BusinessService,
    CallTask,
    ChangeItem,
    ChangeSet,
    Customer,
    Permission,
    Role,
    RolePermission,
    ScanSchedule,
    User,
    UserRole,
)
from app.services.scans import (
    run_review_stuck_scan,
    run_scan_for_schedule,
)

BASE_DIR = Path(__file__).resolve().parent.parent

TZ = "Asia/Shanghai"
# 固定“当前时间”：2026-08-18 10:00（北京时间），避免依赖真实时钟。
NOW = datetime(2026, 8, 18, 2, 0, 0, tzinfo=timezone.utc)
# 2026-08-18 00:00（北京时间）= 2026-08-17 16:00 UTC。
DAY_START_UTC = datetime(2026, 8, 17, 16, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    url = f"sqlite:///{db_path}"
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


# ---------------------------------------------------------------- 测试数据构造


def make_customer(db: Session, name: str = "客户A") -> Customer:
    customer = Customer(name=name)
    db.add(customer)
    db.flush()
    return customer


def make_business(db: Session, customer: Customer, number: str) -> BusinessService:
    service = BusinessService(
        service_number=number,
        customer_id=customer.id,
    )
    db.add(service)
    db.flush()
    return service


def make_schedule(db: Session, scan_type: str = "review_stuck") -> ScanSchedule:
    schedule = ScanSchedule(
        name="测试扫描",
        scan_type=scan_type,
        lead_days=14,
        timezone=TZ,
    )
    db.add(schedule)
    db.flush()
    return schedule


def make_user(
    db: Session,
    username: str,
    phone: str,
    real_name: str = "审核员",
    enabled: bool = True,
) -> User:
    user = User(
        username=username,
        password_hash="x",
        real_name=real_name,
        phone=phone,
        display_name=real_name,
        is_enabled=enabled,
    )
    db.add(user)
    db.flush()
    return user


def grant_permissions(db: Session, user: User, codes: list[str]) -> None:
    """给用户建一个绑定角色，并授予指定权限码（依赖种子数据中的权限）。"""

    role = Role(code=f"role_{user.username}", name=f"角色{user.username}")
    db.add(role)
    db.flush()
    permissions = db.scalars(select(Permission).where(Permission.code.in_(codes))).all()
    for perm in permissions:
        db.add(RolePermission(role_id=role.id, permission_id=perm.id, domain="business"))
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()


def make_change_set(
    db: Session,
    title: str = "修改协议到期时间",
    status: str = "submitted",
    submitted_at: datetime | None = None,
    service: BusinessService | None = None,
) -> ChangeSet:
    change_set = ChangeSet(title=title, status=status, submitted_at=submitted_at)
    db.add(change_set)
    db.flush()
    if service is not None:
        db.add(
            ChangeItem(
                change_set_id=change_set.id,
                entity_type="BusinessService",
                entity_id=service.id,
                operation="update",
                patch_json="{}",
            )
        )
        db.flush()
    return change_set


def task_count(db: Session) -> int:
    return db.scalars(select(func.count(CallTask.id))).one()


def first_task(db: Session) -> CallTask:
    return db.scalars(select(CallTask)).one()


def _reviewer(
    db: Session,
    username: str = "auditor",
    phone: str = "13900000001",
    real_name: str = "审核员",
) -> User:
    user = make_user(db, username, phone, real_name=real_name)
    grant_permissions(db, user, ["review"])
    return user


# ---------------------------------------------------------------- 阈值判定


def test_review_stuck_within_threshold_not_notified(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    _reviewer(db)
    # 恰好 24 小时（阈值边界，submitted_at >= 阈值时刻 → 未卡单）与 23 小时都不通知。
    make_change_set(db, submitted_at=NOW - timedelta(hours=24), service=service)
    make_change_set(db, submitted_at=NOW - timedelta(hours=23), service=service)
    schedule = make_schedule(db)

    assert run_review_stuck_scan(db, schedule, now=NOW) == 0
    assert task_count(db) == 0


def test_review_stuck_over_threshold_notifies(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    _reviewer(db)
    change_set = make_change_set(
        db, title="修改协议到期时间", submitted_at=NOW - timedelta(hours=25), service=service
    )
    schedule = make_schedule(db)

    assert run_review_stuck_scan(db, schedule, now=NOW) == 1

    task = first_task(db)
    assert task.source == "review_stuck"
    assert task.scan_schedule_id == schedule.id
    assert task.customer_id == customer.id
    assert task.contact_id is None
    assert task.dial_number == "13900000001"
    assert task.plan_id is None
    assert task.status == "queued"
    assert task.script_id is not None
    assert task.script.tts_status == "not_generated"
    assert task.script.title == "[扫描]测试扫描 2026-08-18"
    body = task.script.body
    assert "客户A" in body
    assert "3001" in body
    assert "修改协议到期时间" in body
    assert "审核员" in body
    assert "{{" not in body

    meta = json.loads(task.meta_json)
    assert meta["change_set_id"] == change_set.id
    assert meta["rendered_script"] == body
    assert meta["review_stuck_hours"] == 24


def test_review_stuck_hours_setting_overrides_threshold(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    _reviewer(db)
    # 默认 24 小时不会通知 2 小时前的提交；配置为 1 小时后通知。
    make_change_set(db, submitted_at=NOW - timedelta(hours=2), service=service)
    schedule = make_schedule(db)
    assert run_review_stuck_scan(db, schedule, now=NOW) == 0

    db.add(AppSetting(key="review_stuck_hours", value="1"))
    db.flush()
    assert run_review_stuck_scan(db, schedule, now=NOW) == 1
    meta = json.loads(first_task(db).meta_json)
    assert meta["review_stuck_hours"] == 1


# ---------------------------------------------------------------- 通知对象


def test_review_stuck_only_enabled_users_with_review_permission_and_phone(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    make_change_set(db, submitted_at=NOW - timedelta(hours=25), service=service)

    _reviewer(db, username="auditor", phone="13900000001")  # 有 review 权限 + 手机 → 通知
    reader = make_user(db, "reader", "13900000002")
    grant_permissions(db, reader, ["read"])  # 只有 read 权限 → 不通知
    no_phone = make_user(db, "no_phone", "")
    grant_permissions(db, no_phone, ["review"])  # 有权限但无手机 → 不通知
    disabled = make_user(db, "disabled", "13900000003", enabled=False)
    grant_permissions(db, disabled, ["review"])  # 停用 → 不通知
    make_user(db, "no_role", "13900000004")  # 无角色 → 不通知

    schedule = make_schedule(db)
    assert run_review_stuck_scan(db, schedule, now=NOW) == 1
    tasks = db.scalars(select(CallTask)).all()
    assert len(tasks) == 1
    assert tasks[0].dial_number == "13900000001"
    assert tasks[0].contact_id is None


def test_review_stuck_notifies_each_reviewer(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    make_change_set(db, submitted_at=NOW - timedelta(hours=25), service=service)
    _reviewer(db, username="auditor_a", phone="13900000001", real_name="审核员甲")
    _reviewer(db, username="auditor_b", phone="13900000002", real_name="审核员乙")
    schedule = make_schedule(db)

    assert run_review_stuck_scan(db, schedule, now=NOW) == 2
    dial_numbers = {t.dial_number for t in db.scalars(select(CallTask)).all()}
    assert dial_numbers == {"13900000001", "13900000002"}


def test_review_stuck_no_reviewer_creates_nothing(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    make_change_set(db, submitted_at=NOW - timedelta(hours=25), service=service)
    # 库里没有 review 权限或没有绑定用户 → 直接跳过，不报错。
    make_user(db, "plain", "13900000001")
    schedule = make_schedule(db)
    assert run_review_stuck_scan(db, schedule, now=NOW) == 0
    assert task_count(db) == 0


# ---------------------------------------------------------------- 去重与跳过


def test_review_stuck_dedup_same_day(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    _reviewer(db)
    make_change_set(db, submitted_at=NOW - timedelta(hours=25), service=service)
    schedule = make_schedule(db)

    assert run_review_stuck_scan(db, schedule, now=NOW) == 1
    # 把任务挪进“当天”窗口（DAY_START_UTC），第二次扫描同日去重。
    for task in db.scalars(select(CallTask)).all():
        task.created_at = DAY_START_UTC + timedelta(hours=1)
    db.flush()
    assert run_review_stuck_scan(db, schedule, now=NOW) == 0
    assert task_count(db) == 1


def test_review_stuck_skips_change_set_without_business(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    _reviewer(db)
    # 无业务变更项 → 跳过。
    make_change_set(db, title="无业务项", submitted_at=NOW - timedelta(hours=25))
    # BusinessService 变更项但 entity_id 为空（创建类变更）→ 跳过。
    create_only = make_change_set(
        db, title="新建业务", submitted_at=NOW - timedelta(hours=25)
    )
    db.add(
        ChangeItem(
            change_set_id=create_only.id,
            entity_type="BusinessService",
            entity_id=None,
            operation="create",
            patch_json="{}",
        )
    )
    # 非 submitted 状态（draft / approved）即使超阈值也不通知。
    make_change_set(
        db, title="草稿单", status="draft", submitted_at=NOW - timedelta(hours=25), service=service
    )
    make_change_set(
        db, title="已审核单", status="approved", submitted_at=NOW - timedelta(hours=25), service=service
    )
    # submitted 但没有提交时间 → 跳过。
    make_change_set(db, title="无提交时间", service=service)
    # 关联真实业务的卡单 → 正常通知。
    stuck = make_change_set(db, submitted_at=NOW - timedelta(hours=25), service=service)
    schedule = make_schedule(db)

    assert run_review_stuck_scan(db, schedule, now=NOW) == 1
    assert task_count(db) == 1
    assert json.loads(first_task(db).meta_json)["change_set_id"] == stuck.id


# ---------------------------------------------------------------- 统一入口


def test_run_scan_for_schedule_dispatch_review_stuck(db: Session) -> None:
    customer = make_customer(db)
    service = make_business(db, customer, "3001")
    _reviewer(db)
    make_change_set(db, submitted_at=NOW - timedelta(hours=25), service=service)
    schedule = make_schedule(db)
    schedule.last_error = "上次的旧错误"

    assert run_scan_for_schedule(db, schedule, now=NOW) == 1
    assert schedule.last_run_at == NOW
    assert schedule.last_error == ""
    assert task_count(db) == 1
