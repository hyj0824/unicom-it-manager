from __future__ import annotations

"""P1 后台操作体验的 Web 路由与页面测试。

覆盖：
- 通话列表的状态 / 日期区间 / 客户筛选与分页；
- 通话详情的时间线（事件正序、原始串口行、错误信息）；
- 扫描通知配置表单的 cron/时区校验错误与 CRUD（创建/编辑/停用/删除）；
- 失败任务人工重新入队；
- 仪表盘 Worker 硬开关 / 串口可用性 / 当前通话状态展示；
- 高影响操作的 data-confirm 确认提示；
- 删除被引用话术时的可读错误（含引用数量）。

不接触真实串口与系统 ffmpeg；TestClient 不用上下文管理器，避免启动
Scheduler / Call Worker 线程。
"""

import os
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    CallEvent,
    CallRecord,
    CallTask,
    Customer,
    BusinessService,
    NetworkDevice,
    ScanSchedule,
    Script,
    utcnow,
)
from app.services import plans as plan_service
from app.services.call_worker import modem_availability
from app.services.customers import sync_default_contact
from app.services.scripts import referencing_counts as script_referencing_counts

BASE_DIR = Path(__file__).resolve().parent.parent
DB_URL = os.environ["DATABASE_URL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

STATUS_LABEL_COMPLETED = "已完成"
STATUS_LABEL_NO_ANSWER = "无人接听"


# ---------------------------------------------------------------- 基础设施


def _reset_db() -> None:
    engine.dispose()
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture()
def client():
    _reset_db()
    # 不用 `with`：lifespan 不执行，调度器与 Worker 线程不会启动。
    return TestClient(app)


@pytest.fixture()
def db():
    _reset_db()
    session = SessionLocal()
    yield session
    session.close()


def login(client: TestClient, password: str = ADMIN_PASSWORD) -> None:
    resp = client.post(
        "/login", data={"username": "admin", "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


def make_customer(name: str = "客户A", phone: str = "13800000000") -> Customer:
    with SessionLocal() as session:
        customer = Customer(name=name)
        session.add(customer)
        session.flush()
        if phone:
            sync_default_contact(session, customer, phone)
        session.commit()
        session.refresh(customer)
        return customer


def make_script(title: str = "话术A") -> Script:
    with SessionLocal() as session:
        script = Script(title=title, body="正文内容")
        session.add(script)
        session.commit()
        session.refresh(script)
        return script


def make_scan_schedule(
    name: str = "每日到期维系扫描",
    scan_type: str = "due_renewal",
    cron: str = "0 9 * * *",
    timezone_name: str = "Asia/Shanghai",
    script: Script | None = None,
    enabled: bool = True,
    sms_enabled: bool = False,
) -> ScanSchedule:
    with SessionLocal() as session:
        schedule = ScanSchedule(
            name=name,
            scan_type=scan_type,
            script_id=script.id if script else None,
            cron_expr=cron,
            timezone=timezone_name,
            lead_days=14,
            enabled=enabled,
            sms_enabled=sms_enabled,
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        return schedule


def make_task_record(
    customer: Customer,
    script: Script,
    status: str = "completed",
    created_at=None,
) -> tuple[int, int]:
    """创建一条任务+通话记录，返回 (task_id, record_id)。"""

    with SessionLocal() as session:
        # 入参可能来自其它会话（已脱离）：merge 到当前会话，避免 commit 过期属性后
        # 再访问触发 DetachedInstanceError。
        customer = session.merge(customer)
        script = session.merge(script)
        task = plan_service.create_manual_call_task(
            session, customer, script, message="test", source="manual"
        )
        task.status = status
        task.call_record.status = status
        if created_at is not None:
            task.call_record.created_at = created_at
        session.commit()
        return task.id, task.call_record.id


# ---------------------------------------------------------------- 登录保护


def test_login_required_redirects(client: TestClient) -> None:
    for path in ["/", "/calls", "/scan-schedules", "/contacts", "/scripts", "/admin/system", "/sms"]:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login", path


def test_login_then_access(client: TestClient) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "工作台" in resp.text


def _scan_schedule_data(
    script: Script | None = None,
    name: str = "每日到期维系扫描",
    scan_type: str = "due_renewal",
    cron_expr: str = "0 9 * * *",
    timezone_name: str = "Asia/Shanghai",
    lead_days: str = "14",
    enabled: bool = True,
    sms_enabled: bool = False,
) -> dict:
    data = {
        "name": name,
        "scan_type": scan_type,
        "script_id": script.id if script else "",
        "cron_expr": cron_expr,
        "timezone": timezone_name,
        "lead_days": lead_days,
    }
    if enabled:
        data["enabled"] = "on"
    if sms_enabled:
        data["sms_enabled"] = "on"
    return data


# ---------------------------------------------------------------- 通话列表：筛选与分页


def test_calls_filters_by_status_customer_and_date(client: TestClient) -> None:
    login(client)
    customer_a = make_customer("客户甲", "13800000001")
    customer_b = make_customer("客户乙", "13900000002")
    script = make_script()
    make_task_record(customer_a, script, status="completed", created_at=utcnow() - timedelta(days=10))
    make_task_record(customer_a, script, status="no_answer", created_at=utcnow() - timedelta(days=9))
    make_task_record(customer_b, script, status="completed", created_at=utcnow() - timedelta(days=8))

    page = client.get("/calls")
    assert page.status_code == 200
    assert "共 3 条" in page.text

    by_status = client.get("/calls", params={"status": "completed"})
    assert by_status.status_code == 200
    assert "共 2 条" in by_status.text

    by_customer = client.get("/calls", params={"customer_id": customer_a.id})
    assert "共 2 条" in by_customer.text

    # 日期区间按计划时区（Asia/Shanghai）换算为 UTC 边界。
    by_date = client.get(
        "/calls", params={"date_from": "2025-01-01", "date_to": "2025-01-31"}
    )
    assert by_date.status_code == 200
    # 记录创建时间都在「今天」附近（utcnow），不会被 2025-01 区间筛出。
    assert "共 0 条" in by_date.text


def test_calls_rejects_invalid_date_filter(client: TestClient) -> None:
    login(client)
    resp = client.get("/calls", params={"date_from": "not-a-date"})
    assert resp.status_code == 200
    assert "日期筛选格式不正确" in resp.text


def test_calls_pagination(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    base = utcnow() - timedelta(days=30)
    for i in range(30):
        make_task_record(customer, script, status="completed", created_at=base + timedelta(minutes=i))

    page1 = client.get("/calls")
    assert page1.status_code == 200
    assert "共 30 条" in page1.text
    assert "第 1 / 2 页" in page1.text

    page2 = client.get("/calls", params={"page": 2})
    assert page2.status_code == 200
    assert "第 2 / 2 页" in page2.text
    # 第一页与第二页展示的记录数加总等于总数（每页 25 条）。
    count1 = page1.text.count("/calls/")
    count2 = page2.text.count("/calls/")
    assert count1 + count2 >= 30


def test_calls_page_beyond_last_page_clamps(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    make_task_record(customer, script, status="completed")
    resp = client.get("/calls", params={"page": 99})
    assert resp.status_code == 200
    assert "共 1 条" in resp.text


# ---------------------------------------------------------------- 通话详情：时间线


def test_call_detail_shows_timeline_raw_lines_and_error(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    task_id, record_id = make_task_record(customer, script, status="failed")
    base = utcnow() - timedelta(seconds=30)
    with SessionLocal() as session:
        session.add(
            CallEvent(
                call_record=session.get(CallRecord, record_id),
                event_type="at_command",
                message="ATD13800000000;",
                raw_line="ATD13800000000;",
                created_at=base,
            )
        )
        session.add(
            CallEvent(
                call_record=session.get(CallRecord, record_id),
                event_type="voice_call_end",
                message="VOICE CALL: END: 000005",
                raw_line="VOICE CALL: END: 000005",
                created_at=base + timedelta(seconds=5),
            )
        )
        record_ref = session.get(CallRecord, record_id)
        record_ref.status = "failed"
        record_ref.error_message = "No carrier after dial."
        session.commit()

    resp = client.get(f"/calls/{record_id}")
    assert resp.status_code == 200
    assert "事件时间线" in resp.text
    assert "No carrier after dial." in resp.text
    assert "VOICE CALL: END: 000005" in resp.text
    assert "ATD13800000000;" in resp.text
    # 事件按时间正序：at_command 在 voice_call_end 之前。
    assert resp.text.index("at_command") < resp.text.index("voice_call_end")


# ---------------------------------------------------------------- 扫描通知配置：CRUD 与表单校验


def test_scan_schedule_create_rejects_invalid_cron_with_clear_message(client: TestClient) -> None:
    login(client)
    resp = client.post(
        "/scan-schedules",
        data=_scan_schedule_data(cron_expr="0 9 * *"),
    )
    assert resp.status_code == 400
    assert "Cron 表达式" in resp.text
    assert "无效" in resp.text


def test_scan_schedule_create_rejects_invalid_timezone(client: TestClient) -> None:
    login(client)
    resp = client.post(
        "/scan-schedules",
        data=_scan_schedule_data(timezone_name="Mars/Olympus"),
    )
    assert resp.status_code == 400
    assert "时区「Mars/Olympus」无效" in resp.text


def test_scan_schedule_create_rejects_missing_name_and_bad_type(client: TestClient) -> None:
    login(client)
    resp = client.post("/scan-schedules", data=_scan_schedule_data(name=" "))
    assert resp.status_code == 400
    assert "名称不能为空" in resp.text

    resp = client.post("/scan-schedules", data=_scan_schedule_data(scan_type="monthly"))
    assert resp.status_code == 400
    assert "扫描类型无效" in resp.text


def test_scan_schedule_create_rejects_bad_lead_days(client: TestClient) -> None:
    login(client)
    resp = client.post("/scan-schedules", data=_scan_schedule_data(lead_days="abc"))
    assert resp.status_code == 400
    assert "提前天数必须是整数" in resp.text

    resp = client.post("/scan-schedules", data=_scan_schedule_data(lead_days="-3"))
    assert resp.status_code == 400
    assert "提前天数不能为负数" in resp.text


def test_scan_schedule_create_valid_shows_in_list(client: TestClient) -> None:
    login(client)
    script = make_script()
    resp = client.post(
        "/scan-schedules",
        data=_scan_schedule_data(script, name="设备回收扫描", scan_type="device_recycle", sms_enabled=True),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/scan-schedules")
    assert page.status_code == 200
    assert "设备回收扫描" in page.text
    assert "设备回收" in page.text
    assert script.title in page.text
    with SessionLocal() as session:
        schedule = session.scalars(select(ScanSchedule)).one()
        assert schedule.scan_type == "device_recycle"
        assert schedule.script_id == script.id
        assert schedule.cron_expr == "0 9 * * *"
        assert schedule.timezone == "Asia/Shanghai"
        assert schedule.lead_days == 14
        assert schedule.enabled is True
        assert schedule.sms_enabled is True


def test_scan_schedule_edit_updates_fields(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule(cron="0 8 * * *")
    new_script = make_script("话术B")
    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(
            new_script,
            name="改名后的扫描",
            scan_type="device_recycle",
            cron_expr="30 10 * * *",
            timezone_name="UTC",
            lead_days="7",
            enabled=False,
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        updated = session.get(ScanSchedule, schedule.id)
        assert updated.name == "改名后的扫描"
        assert updated.scan_type == "device_recycle"
        assert updated.script_id == new_script.id
        assert updated.cron_expr == "30 10 * * *"
        assert updated.timezone == "UTC"
        assert updated.lead_days == 7
        assert updated.enabled is False
        assert updated.sms_enabled is False


def test_scan_schedule_sms_toggle_via_edit(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule(sms_enabled=True)
    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(sms_enabled=True),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id).sms_enabled is True

    # 取消勾选（不提交 sms_enabled）即关闭。
    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(sms_enabled=False),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id).sms_enabled is False


def test_scan_schedule_edit_rejects_invalid_cron(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(cron_expr="not a cron"),
    )
    assert resp.status_code == 400
    assert "Cron 表达式" in resp.text
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id).cron_expr == "0 9 * * *"


def test_scan_schedule_toggle_disables_and_enables(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()

    resp = client.post(f"/scan-schedules/{schedule.id}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id).enabled is False

    resp = client.post(f"/scan-schedules/{schedule.id}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id).enabled is True


def test_scan_schedule_delete_removes_row(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    resp = client.post(f"/scan-schedules/{schedule.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id) is None


def test_scan_schedule_delete_blocked_when_tasks_reference_it(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    customer = make_customer()
    script = make_script()
    with SessionLocal() as session:
        session.add(
            CallTask(
                scan_schedule_id=schedule.id,
                customer_id=customer.id,
                script_id=script.id,
                due_at=utcnow(),
                status="queued",
                source="due_renewal",
            )
        )
        session.commit()

    resp = client.post(f"/scan-schedules/{schedule.id}/delete")
    assert resp.status_code == 400
    assert "不能删除" in resp.text
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id) is not None


def test_scan_schedule_edit_form_prefills_current_values(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule(name="已存在的扫描", cron="30 8 * * *")
    resp = client.get("/scan-schedules", params={"edit_id": schedule.id})
    assert resp.status_code == 200
    assert "编辑扫描配置" in resp.text
    assert "已存在的扫描" in resp.text
    assert "30 8 * * *" in resp.text


def test_scan_schedule_list_shows_last_run_and_error(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    with SessionLocal() as session:
        row = session.get(ScanSchedule, schedule.id)
        row.last_run_at = utcnow() - timedelta(days=1)
        row.last_error = "ValueError: boom"
        session.commit()

    resp = client.get("/scan-schedules")
    assert resp.status_code == 200
    assert "ValueError: boom" in resp.text


# ---------------------------------------------------------------- 人工重新入队


def test_requeue_failed_task_resets_to_queued(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    with SessionLocal() as session:
        task = plan_service.create_manual_call_task(
            session, customer, script, status="failed", message="boom"
        )
        task.attempt = 2
        task.completed_at = utcnow()
        task.error_message = "boom"
        session.commit()
        task_id = task.id

    resp = client.post(f"/tasks/{task_id}/requeue", follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as session:
        task = session.get(CallTask, task_id)
        assert task.status == "queued"
        assert task.attempt == 1
        assert task.error_message == ""
        assert task.completed_at is None
        record = task.call_record
        assert record.status == "queued"
        assert record.error_message == ""
        assert any(e.event_type == "manual_requeue" for e in record.events)
        # 手动任务不关联计划 / 扫描配置。
        assert task.plan_id is None
        assert task.scan_schedule_id is None


def test_requeue_active_task_is_rejected(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    with SessionLocal() as session:
        task = plan_service.create_manual_call_task(
            session, customer, script, status="queued", message="queued"
        )
        session.commit()
        task_id = task.id

    resp = client.post(f"/tasks/{task_id}/requeue")  # 跟随重定向
    assert resp.status_code == 200
    assert "不能重复入队" in resp.text

    with SessionLocal() as session:
        assert session.get(CallTask, task_id).status == "queued"


def test_call_detail_offers_requeue_for_terminal_task(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    task_id, record_id = make_task_record(customer, script, status="busy")
    resp = client.get(f"/calls/{record_id}")
    assert resp.status_code == 200
    assert f"/tasks/{task_id}/requeue" in resp.text
    assert "重新入队" in resp.text


# ---------------------------------------------------------------- 仪表盘 Worker 状态


def test_dashboard_shows_worker_serial_and_current_call(client: TestClient) -> None:
    login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "外呼 Worker" in resp.text
    assert ".env 硬开关" in resp.text
    assert "关闭" in resp.text  # CALL_WORKER_ENABLED=0（conftest）
    assert "未启用" in resp.text
    assert "串口可用性" in resp.text
    assert "当前通话" in resp.text
    assert "空闲" in resp.text  # worker_status.working = False

    system = client.get("/admin/system")
    assert system.status_code == 200
    assert "配置未开启" in system.text
    assert "CALL_WORKER_ENABLED=1" in system.text
    assert "A7670E 串口" in system.text
    assert "不可用" in system.text or "待确认" in system.text


# ---------------------------------------------------------------- 高影响操作确认


def test_high_impact_forms_have_data_confirm(client: TestClient) -> None:
    login(client)
    make_scan_schedule(name="删除确认扫描")

    schedules_page = client.get("/scan-schedules")
    assert "删除扫描配置「删除确认扫描」" in schedules_page.text
    assert "停用" in schedules_page.text

    # Worker 未启用时，启动按钮带确认；已启用时不需要确认（模板中仅未启用渲染）。
    system_page = client.get("/admin/system")
    assert "启动 Worker 后将开始拨打队列中的任务" in system_page.text


# ---------------------------------------------------------------- 删除被引用数据


def test_delete_script_referenced_by_scan_schedule_shows_counts(client: TestClient) -> None:
    login(client)
    script = make_script()
    make_scan_schedule(script=script)
    make_scan_schedule(name="第二份配置", scan_type="device_recycle", script=script)

    resp = client.post(f"/scripts/{script.id}/delete")
    assert resp.status_code == 400
    assert "扫描配置 2 条" in resp.text
    with SessionLocal() as session:
        assert session.get(Script, script.id) is not None


def test_delete_script_without_references_succeeds(client: TestClient) -> None:
    login(client)
    script = make_script()
    resp = client.post(f"/scripts/{script.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(Script, script.id) is None


# ---------------------------------------------------------------- 服务层单元测试


def test_validate_cron_expr_messages(db) -> None:
    with pytest.raises(ValueError, match="必须填写 Cron 表达式"):
        plan_service.validate_cron_expr("", "Asia/Shanghai")
    with pytest.raises(ValueError, match="Cron 表达式.*无效"):
        plan_service.validate_cron_expr("0 9 * *", "Asia/Shanghai")
    with pytest.raises(ValueError, match="时区「Mars/Olympus」无效"):
        plan_service.validate_cron_expr("0 9 * * *", "Mars/Olympus")
    # 合法表达式不抛异常。
    plan_service.validate_cron_expr("0 9 * * *", "Asia/Shanghai")


def test_requeue_call_task_semantics(db) -> None:
    customer = Customer(name="客户A")
    db.add(customer)
    db.flush()
    sync_default_contact(db, customer, "13800000000")
    script = Script(title="话术", body="正文")
    db.add(script)
    db.flush()
    task = plan_service.create_manual_call_task(db, customer, script, status="no_answer", message="没人接")
    task.attempt = 2
    db.commit()

    plan_service.requeue_call_task(db, task, message="人工重打")
    db.commit()
    db.refresh(task)

    assert task.status == "queued"
    assert task.attempt == 1
    assert task.call_record.status == "queued"
    assert [e.event_type for e in task.call_record.events][-1] == "manual_requeue"

    with pytest.raises(ValueError, match="不能重复入队"):
        plan_service.requeue_call_task(db, task)


def test_create_manual_call_task_without_plan_and_phone(db) -> None:
    customer = Customer(name="无电话客户")
    db.add(customer)
    db.commit()
    script = Script(title="话术", body="正文")
    db.add(script)
    db.commit()

    with pytest.raises(ValueError, match="没有可拨打的电话"):
        plan_service.create_manual_call_task(db, customer, script)

    sync_default_contact(db, customer, "13700000001")
    db.commit()
    task = plan_service.create_manual_call_task(db, customer, script, message="手动")
    db.commit()
    assert task.plan_id is None
    assert task.scan_schedule_id is None
    assert task.dial_number == "13700000001"
    assert task.call_record.plan_id is None


def test_referencing_counts_helpers(db) -> None:
    customer = Customer(name="客户A")
    db.add(customer)
    db.flush()
    sync_default_contact(db, customer, "13800000000")
    script = Script(title="话术", body="正文")
    db.add(script)
    db.flush()
    schedule = ScanSchedule(
        name="扫描",
        scan_type="due_renewal",
        script=script,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        lead_days=14,
        enabled=True,
    )
    db.add(schedule)
    plan_service.create_manual_call_task(db, customer, script, status="queued")
    db.commit()

    script_refs = script_referencing_counts(db, script)
    assert script_refs["schedules"] == 1
    assert script_refs["plans"] == 0  # 历史 callback_plans 引用仍会被统计
    assert script_refs["tasks"] == 1
    assert script_refs["records"] == 1


def test_modem_availability_levels() -> None:
    settings = Settings(
        admin_password="test", session_secret="test", database_url="sqlite:///:memory:",
        modem_port="/dev/ttyUSB-DOES-NOT-EXIST", modem_baud=115200,
        audio_device="plughw:0,0", call_connect_timeout_seconds=90,
        rejected_end_seconds=20, min_connected_seconds=8, retry_delay_seconds=300,
        max_call_attempts=2, tts_provider="none", tts_api_key="", tts_voice="",
        default_timezone="Asia/Shanghai", call_worker_enabled=False, worker_poll_seconds=5,
    )
    # 设备节点不存在 → fail。
    assert modem_availability(settings, {"running": True})["level"] == "fail"
    # Worker 有错误 → fail（即使节点存在，这里节点不存在优先报节点问题，先换节点）。
    existing = settings.__dict__ | {"modem_port": "/dev/null"}
    settings2 = Settings(**existing)
    assert modem_availability(settings2, {"last_error": "boom"})["level"] == "fail"
    assert modem_availability(settings2, {"running": True})["level"] == "ok"
    assert modem_availability(settings2, {})["level"] == "warn"


def test_nav_section_open_on_contacts_page(client: TestClient) -> None:
    """回归：进入「回访与通话」组页面（通讯录/扫描通知/话术/通话）时导航组必须展开。"""
    login(client)
    for page, expected_open in [
        ("/contacts", True),
        ("/scan-schedules", True),
        ("/scripts", True),
        ("/calls", True),
        ("/sms", True),
        ("/ledger", False),
        ("/", False),
    ]:
        html = client.get(page).text
        marker = '<details class="nav-section"'
        summary = "<summary>回访与通话"
        summary_idx = html.find(summary)
        assert summary_idx != -1, page
        # 定位该 summary 所属的 details 开始标签（向前找最近的）。
        start = html.rfind(marker, 0, summary_idx)
        assert start != -1, page
        seg = html[start:summary_idx]
        assert ("open" in seg) is expected_open, page


def test_ledger_is_paginated_and_uses_modal_lookup_controls(client: TestClient) -> None:
    login(client)
    customer = make_customer("分页客户")
    with SessionLocal() as session:
        session.add_all(
            [BusinessService(service_number=f"LEDGER-{i:03d}", customer_id=customer.id) for i in range(51)]
        )
        session.commit()

    page1 = client.get("/ledger", params={"q": "LEDGER"})
    assert page1.status_code == 200
    assert "第 1 / 2 页" in page1.text
    assert "共 51 条" in page1.text
    assert 'data-modal-open="ledger-modal"' in page1.text
    assert 'list="customer-options"' in page1.text
    assert 'name="customer_id"' in page1.text
    page2 = client.get("/ledger", params={"q": "LEDGER", "page": 2})
    assert "第 2 / 2 页" in page2.text
    assert "LEDGER-000" in page2.text


def test_devices_is_paginated_and_uses_modal_lookup_controls(client: TestClient) -> None:
    login(client)
    customer = make_customer("设备分页客户")
    with SessionLocal() as session:
        service = BusinessService(service_number="DEVICE-SERVICE", customer_id=customer.id)
        session.add(service)
        session.flush()
        session.add_all(
            [NetworkDevice(device_code=f"DEVICE-{i:03d}", business_service_id=service.id) for i in range(51)]
        )
        session.commit()

    page1 = client.get("/devices", params={"q": "DEVICE"})
    assert page1.status_code == 200
    assert "第 1 / 2 页" in page1.text
    assert "共 51 条" in page1.text
    assert 'data-modal-open="device-modal"' in page1.text
    assert 'list="business-service-options"' in page1.text
    assert 'name="business_service_id"' in page1.text
    page2 = client.get("/devices", params={"q": "DEVICE", "page": 2})
    assert "第 2 / 2 页" in page2.text
    assert "DEVICE-000" in page2.text


def test_ledger_and_devices_validation_errors_redirect_to_list(client: TestClient) -> None:
    login(client)
    ledger_resp = client.post("/ledger", data={"service_number": "", "customer_id": ""}, follow_redirects=False)
    assert ledger_resp.status_code == 303
    assert "error=" in ledger_resp.headers["location"]
    assert "业务号码不能为空" in client.get(ledger_resp.headers["location"]).text

    device_resp = client.post("/devices", data={"device_code": "", "business_service_id": ""}, follow_redirects=False)
    assert device_resp.status_code == 303
    assert "error=" in device_resp.headers["location"]
    assert "设备编码不能为空" in client.get(device_resp.headers["location"]).text
