from __future__ import annotations

"""扫描服务（app/services/scans.py）单元测试。

覆盖：
- 话术模板渲染（替换/缺键保留/空白容忍）；
- 到期维系扫描：窗口内/窗口外、lead_days 边界、无客户经理跳过、话术与
  meta_json 内容、同一天去重（幂等）；
- 设备回收扫描：字典项与过期两种退网口径、未回收/已回收判定、去重；
- run_scan_for_schedule：成功状态落库、异常写 last_error 且不抛出。

使用临时 SQLite + alembic head；TTS_PROVIDER=none（conftest）时不生成音频，
音频生成路径用 monkeypatch 验证。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import BusinessService, CallTask, Contact, Customer, CustomerContact, NetworkDevice, ScanSchedule, Script
from app.services import scans
from app.services.dictionaries import resolve_or_create_item
from app.services.scans import (
    DEFAULT_TEMPLATES,
    render_script_template,
    run_device_recycle_scan,
    run_due_renewal_scan,
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


def add_contact(
    db: Session,
    customer: Customer,
    name: str,
    phone: str,
    duty: str,
    active: bool = True,
) -> Contact:
    contact = Contact(name=name, phone=phone)
    db.add(contact)
    db.flush()
    db.add(
        CustomerContact(
            customer_id=customer.id,
            contact_id=contact.id,
            duty=duty,
            is_active=active,
        )
    )
    db.flush()
    return contact


def make_business(
    db: Session,
    customer: Customer,
    number: str,
    expires_at: datetime | None = None,
    status_item=None,
) -> BusinessService:
    service = BusinessService(
        service_number=number,
        customer_id=customer.id,
        agreement_expires_at=expires_at,
        service_status_item=status_item,
    )
    db.add(service)
    db.flush()
    return service


def make_device(
    db: Session,
    service: BusinessService,
    code: str,
    recovery_status_item=None,
    active: bool = True,
) -> NetworkDevice:
    device = NetworkDevice(
        device_code=code,
        business_service_id=service.id,
        recovery_status_item=recovery_status_item,
        is_active=active,
    )
    db.add(device)
    db.flush()
    return device


def make_schedule(
    db: Session,
    scan_type: str,
    lead_days: int = 14,
    timezone_name: str = TZ,
    script: Script | None = None,
) -> ScanSchedule:
    schedule = ScanSchedule(
        name="测试扫描",
        scan_type=scan_type,
        lead_days=lead_days,
        timezone=timezone_name,
        script=script,
    )
    db.add(schedule)
    db.flush()
    return schedule


def task_count(db: Session) -> int:
    return db.scalars(select(func.count(CallTask.id))).one()


def first_task(db: Session) -> CallTask:
    return db.scalars(select(CallTask)).one()


def _today_start_utc(timezone_name: str = TZ) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_now = datetime.now(timezone.utc).astimezone(zone)
    return datetime.combine(local_now.date(), datetime.min.time(), tzinfo=zone).astimezone(
        timezone.utc
    )


# ---------------------------------------------------------------- 话术模板渲染


def test_render_script_template_replaces_known_and_keeps_missing() -> None:
    template = "客户{{客户名称}}业务{{业务号码}}将于{{协议到期日}}到期，{{负责人姓名}}请处理{{未知占位符}}"
    out = render_script_template(
        template,
        {
            "客户名称": "测试客户",
            "业务号码": "10086",
            "协议到期日": "2026-08-18",
            "负责人姓名": "张三",
        },
    )
    assert out == "客户测试客户业务10086将于2026-08-18到期，张三请处理{{未知占位符}}"


def test_render_script_template_tolerates_whitespace_in_placeholders() -> None:
    out = render_script_template(
        "{{ 客户名称 }}｜{{ 缺失键 }}", {"客户名称": "客户X"}
    )
    assert out == "客户X｜{{ 缺失键 }}"


def test_default_templates_cover_placeholder_convention() -> None:
    expected = {"客户名称", "业务号码", "协议到期日", "负责人姓名", "设备编码", "扫描类型"}
    assert set(DEFAULT_TEMPLATES) == {"due_renewal", "device_recycle"}
    for template in DEFAULT_TEMPLATES.values():
        found = set(re.findall(r"\{\{\s*([^{}]+?)\s*\}\}", template))
        assert found, "默认话术必须包含占位符"
        assert found <= expected, f"出现约定外的占位符: {found - expected}"
        assert "{{设备编码}}" in template or found == expected or "{{协议到期日}}" in template


# ---------------------------------------------------------------- 到期维系扫描


def test_due_renewal_generates_task_in_window(db: Session) -> None:
    customer = make_customer(db)
    manager = add_contact(db, customer, "张经理", "13900000001", "客户经理")
    # 到期日 = 2026-08-24（北京时间，DAY_START_UTC + 6 天），在默认提前 14 天窗口内。
    service = make_business(
        db, customer, "1001", expires_at=DAY_START_UTC + timedelta(days=6)
    )
    schedule = make_schedule(db, "due_renewal")

    assert run_due_renewal_scan(db, schedule, now=NOW) == 1

    task = first_task(db)
    assert task.source == "due_renewal"
    assert task.scan_schedule_id == schedule.id
    assert task.customer_id == customer.id
    assert task.contact_id == manager.id
    assert task.dial_number == "13900000001"
    assert task.plan_id is None
    assert task.status == "queued"
    assert task.script_id is not None
    assert task.script.tts_status == "not_generated"
    assert task.script.title == "[扫描]测试扫描 2026-08-18"
    assert "客户A" in task.script.body
    assert "张经理" in task.script.body
    assert "{{客户名称}}" not in task.script.body

    meta = json.loads(task.meta_json)
    assert meta["business_service_id"] == service.id
    assert meta["due_date"] == "2026-08-24"
    assert meta["scan_schedule_id"] == schedule.id
    assert meta["rendered_script"] == task.script.body


def test_due_renewal_window_bounds(db: Session) -> None:
    customer = make_customer(db)
    add_contact(db, customer, "张经理", "13900000001", "客户经理")
    schedule = make_schedule(db, "due_renewal", lead_days=14)
    # 到期日恰为「提前 14 天」→ 在闭区间内，生成。
    make_business(db, customer, "边界内", expires_at=DAY_START_UTC + timedelta(days=14))
    # 提前 15 天 → 窗口外，不生成。
    make_business(db, customer, "边界外", expires_at=DAY_START_UTC + timedelta(days=15))
    # 已经过期 → 不在维系窗口内（属于退网回收口径）。
    make_business(db, customer, "已过期", expires_at=DAY_START_UTC - timedelta(days=1))

    assert run_due_renewal_scan(db, schedule, now=NOW) == 1
    tasks = db.scalars(select(CallTask)).all()
    metas = [json.loads(t.meta_json)["business_service_id"] for t in tasks]
    services = db.scalars(select(BusinessService)).all()
    boundary_id = next(s.id for s in services if s.service_number == "边界内")
    assert metas == [boundary_id]


def test_due_renewal_lead_days_widens_window(db: Session) -> None:
    customer = make_customer(db)
    add_contact(db, customer, "张经理", "13900000001", "客户经理")
    # 提前 15 天到期：默认 lead_days=14 不生成，lead_days=20 生成。
    make_business(db, customer, "1001", expires_at=DAY_START_UTC + timedelta(days=15))
    schedule14 = make_schedule(db, "due_renewal", lead_days=14)
    assert run_due_renewal_scan(db, schedule14, now=NOW) == 0
    assert task_count(db) == 0

    schedule20 = make_schedule(db, "due_renewal", lead_days=20)
    assert run_due_renewal_scan(db, schedule20, now=NOW) == 1
    assert task_count(db) == 1


def test_due_renewal_skips_without_account_manager_or_phone(db: Session) -> None:
    customer = make_customer(db)
    make_business(db, customer, "无联系人", expires_at=DAY_START_UTC + timedelta(days=3))
    schedule = make_schedule(db, "due_renewal")
    assert run_due_renewal_scan(db, schedule, now=NOW) == 0
    assert task_count(db) == 0

    add_contact(db, customer, "有经理无电话", "", "客户经理")
    assert run_due_renewal_scan(db, schedule, now=NOW) == 0

    add_contact(db, customer, "停用经理", "13900000002", "客户经理", active=False)
    assert run_due_renewal_scan(db, schedule, now=NOW) == 0

    add_contact(db, customer, "在职经理", "13900000003", "客户经理")
    assert run_due_renewal_scan(db, schedule, now=NOW) == 1
    assert task_count(db) == 1


def test_due_renewal_naive_utc_expiry_is_supported(db: Session) -> None:
    customer = make_customer(db)
    add_contact(db, customer, "张经理", "13900000001", "客户经理")
    # 台账导入存储的是 naive UTC（2026-08-23 00:00 UTC）。
    make_business(
        db,
        customer,
        "naive",
        expires_at=datetime(2026, 8, 23, 0, 0, 0),
    )
    schedule = make_schedule(db, "due_renewal")
    assert run_due_renewal_scan(db, schedule, now=NOW) == 1


def test_due_renewal_dedup_same_day_and_rerun_next_day(db: Session) -> None:
    today_start = _today_start_utc()
    customer = make_customer(db)
    add_contact(db, customer, "张经理", "13900000001", "客户经理")
    make_business(db, customer, "1001", expires_at=today_start + timedelta(days=3))
    schedule = make_schedule(db, "due_renewal")

    # 当天第二次扫描：同业务不再重复生成（幂等）。
    assert run_due_renewal_scan(db, schedule) == 1
    assert run_due_renewal_scan(db, schedule) == 0
    assert task_count(db) == 1

    # 历史任务属于前一天：新的一天重新生成。
    task = first_task(db)
    task.created_at = DAY_START_UTC - timedelta(days=1)
    db.flush()
    assert run_due_renewal_scan(db, schedule, now=NOW) == 1
    assert task_count(db) == 2


def test_due_renewal_uses_schedule_script_template(db: Session) -> None:
    customer = make_customer(db)
    add_contact(db, customer, "张经理", "13900000001", "客户经理")
    make_business(db, customer, "1001", expires_at=DAY_START_UTC + timedelta(days=3))
    template = Script(title="自定义模板", body="自定义{{客户名称}}提醒，到期日{{协议到期日}}")
    db.add(template)
    db.flush()
    schedule = make_schedule(db, "due_renewal", script=template)

    assert run_due_renewal_scan(db, schedule, now=NOW) == 1
    task = first_task(db)
    assert task.script.body == f"自定义客户A提醒，到期日2026-08-21"
    assert task.script.id != template.id


def test_due_renewal_generates_audio_for_non_none_provider(db: Session, monkeypatch) -> None:
    customer = make_customer(db)
    add_contact(db, customer, "张经理", "13900000001", "客户经理")
    make_business(db, customer, "1001", expires_at=DAY_START_UTC + timedelta(days=3))
    schedule = make_schedule(db, "due_renewal")

    calls: list[str] = []

    def fake_generate(_db, script, _settings) -> str:
        calls.append(script.body)
        script.tts_status = "generated"
        return "ok"

    class _FakeSettings:
        tts_provider = "edge"
        max_call_attempts = 2

    monkeypatch.setattr(scans.script_service, "generate_script_audio", fake_generate)
    monkeypatch.setattr(scans, "get_settings", lambda: _FakeSettings())

    assert run_due_renewal_scan(db, schedule, now=NOW) == 1
    assert len(calls) == 1
    assert first_task(db).script.tts_status == "generated"


# ---------------------------------------------------------------- 设备回收扫描


def test_device_recycle_retired_via_dict_label(db: Session) -> None:
    customer = make_customer(db)
    keeper = add_contact(db, customer, "王工", "13700000001", "网络维护责任人")
    retired = resolve_or_create_item(db, "service_status", "退网")
    service = make_business(db, customer, "2001", status_item=retired)
    device = make_device(db, service, "DEV-001")

    schedule = make_schedule(db, "device_recycle")
    assert run_device_recycle_scan(db, schedule, now=NOW) == 1

    task = first_task(db)
    assert task.source == "device_recycle"
    assert task.scan_schedule_id == schedule.id
    assert task.contact_id == keeper.id
    assert task.dial_number == "13700000001"
    assert task.plan_id is None
    assert task.script.tts_status == "not_generated"
    assert "DEV-001" in task.script.body
    assert "{{设备编码}}" not in task.script.body

    meta = json.loads(task.meta_json)
    assert meta["device_id"] == device.id
    assert meta["device_code"] == "DEV-001"
    assert meta["business_service_id"] == service.id
    assert meta["due_date"] == "2026-08-18"
    assert meta["scan_schedule_id"] == schedule.id


def test_device_recycle_retired_via_seeded_label_and_expired_agreement(db: Session) -> None:
    # 种子字典标签「主动退网(申请拆机)」同样命中退网判定。
    customer = make_customer(db)
    add_contact(db, customer, "王工", "13700000001", "网络维护责任人")
    seeded_retired = resolve_or_create_item(db, "service_status", "主动退网(申请拆机)")
    service = make_business(db, customer, "2001", status_item=seeded_retired)
    make_device(db, service, "DEV-SEED")

    # 正常状态业务但协议已过期 → 时间口径退网。
    normal = resolve_or_create_item(db, "service_status", "正常开机")
    expired = make_business(
        db, customer, "2002", expires_at=DAY_START_UTC - timedelta(days=2), status_item=normal
    )
    make_device(db, expired, "DEV-EXP")

    # 协议今天才到期 → 不算已过期，不生成。
    today = make_business(db, customer, "2003", expires_at=DAY_START_UTC)
    make_device(db, today, "DEV-TODAY")

    schedule = make_schedule(db, "device_recycle")
    assert run_device_recycle_scan(db, schedule, now=NOW) == 2
    codes = {json.loads(t.meta_json)["device_code"] for t in db.scalars(select(CallTask)).all()}
    assert codes == {"DEV-SEED", "DEV-EXP"}


def test_device_recycle_recovered_vs_unrecovered(db: Session) -> None:
    customer = make_customer(db)
    add_contact(db, customer, "王工", "13700000001", "网络维护责任人")
    retired = resolve_or_create_item(db, "service_status", "退网")
    service = make_business(db, customer, "2001", status_item=retired)

    recovered = resolve_or_create_item(db, "recovery_status", "已回收")
    not_recovered = resolve_or_create_item(db, "recovery_status", "未回收")
    make_device(db, service, "DEV-REC", recovery_status_item=recovered)
    make_device(db, service, "DEV-NOT", recovery_status_item=not_recovered)
    make_device(db, service, "DEV-NONE")  # 未填写回收状态 → 视为未回收

    schedule = make_schedule(db, "device_recycle")
    assert run_device_recycle_scan(db, schedule, now=NOW) == 2
    codes = {json.loads(t.meta_json)["device_code"] for t in db.scalars(select(CallTask)).all()}
    assert codes == {"DEV-NOT", "DEV-NONE"}

    # 全部设备已回收的业务不再生成。
    another = make_business(db, customer, "2002", status_item=retired)
    make_device(db, another, "DEV-ALL-REC", recovery_status_item=recovered)
    # 把首次扫描生成的任务挪到“当天”窗口内，验证同日去重生效。
    for task in db.scalars(select(CallTask)).all():
        task.created_at = DAY_START_UTC + timedelta(hours=1)
    db.flush()
    assert run_device_recycle_scan(db, schedule, now=NOW) == 0
    assert task_count(db) == 2


def test_device_recycle_only_for_retired_services_and_inactive_devices(db: Session) -> None:
    customer = make_customer(db)
    add_contact(db, customer, "王工", "13700000001", "网络维护责任人")
    normal = resolve_or_create_item(db, "service_status", "正常开机")
    # 正常业务（未过期、未标记退网）：设备不生成回收任务。
    live = make_business(db, customer, "2001", status_item=normal)
    make_device(db, live, "DEV-LIVE")

    # 停用设备不参与回收通知。
    retired = resolve_or_create_item(db, "service_status", "退网")
    retired_svc = make_business(db, customer, "2002", status_item=retired)
    make_device(db, retired_svc, "DEV-DISABLED", active=False)

    schedule = make_schedule(db, "device_recycle")
    assert run_device_recycle_scan(db, schedule, now=NOW) == 0


def test_device_recycle_dedup_same_day(db: Session) -> None:
    today_start = _today_start_utc()
    customer = make_customer(db)
    add_contact(db, customer, "王工", "13700000001", "网络维护责任人")
    retired = resolve_or_create_item(db, "service_status", "退网")
    service = make_business(db, customer, "2001", status_item=retired)
    make_device(db, service, "DEV-001")
    expired = make_business(db, customer, "2002", expires_at=today_start - timedelta(days=1))
    make_device(db, expired, "DEV-EXP")

    schedule = make_schedule(db, "device_recycle")
    assert run_device_recycle_scan(db, schedule) == 2
    assert run_device_recycle_scan(db, schedule) == 0
    assert task_count(db) == 2


# ---------------------------------------------------------------- 统一入口


def test_run_scan_for_schedule_success_stamps_state(db: Session) -> None:
    customer = make_customer(db)
    add_contact(db, customer, "张经理", "13900000001", "客户经理")
    make_business(db, customer, "1001", expires_at=DAY_START_UTC + timedelta(days=3))
    schedule = make_schedule(db, "due_renewal")
    schedule.last_error = "上次的旧错误"

    assert run_scan_for_schedule(db, schedule, now=NOW) == 1
    assert schedule.last_run_at == NOW
    assert schedule.last_error == ""
    assert task_count(db) == 1


def test_run_scan_for_schedule_unknown_type_writes_error(db: Session) -> None:
    schedule = make_schedule(db, "no_such_type")
    db.commit()  # 调度器场景：计划先落库，扫描回滚不丢计划本身
    assert run_scan_for_schedule(db, schedule, now=NOW) == 0
    assert "no_such_type" in schedule.last_error
    # 错误落库可查（调用方提交后）。
    db.commit()
    assert db.get(ScanSchedule, schedule.id).last_error == schedule.last_error


def test_run_scan_for_schedule_swallows_runner_exception(db: Session, monkeypatch) -> None:
    def boom(_db, _schedule, now=None) -> int:
        raise RuntimeError("扫描炸了")

    monkeypatch.setitem(scans._SCAN_RUNNERS, "due_renewal", boom)
    schedule = make_schedule(db, "due_renewal")
    db.commit()  # 调度器场景：计划先落库，扫描回滚不丢计划本身

    assert run_scan_for_schedule(db, schedule, now=NOW) == 0
    assert schedule.last_error == "扫描炸了"
    assert schedule.last_run_at is None
    assert task_count(db) == 0
    # 错误已持久化，且扫描产生的半成品被回滚。
    db.commit()
    assert db.get(ScanSchedule, schedule.id).last_error == "扫描炸了"
    assert task_count(db) == 0
