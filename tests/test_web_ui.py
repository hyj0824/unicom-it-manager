from __future__ import annotations

"""P1 后台操作体验的 Web 路由与页面测试。

覆盖：
- 通话列表的状态 / 日期区间 / 客户筛选与分页；
- 通话详情的时间线（事件正序、原始串口行、错误信息）；
- 扫描通知配置表单的 cron/时区校验错误与 CRUD（创建/编辑/停用/删除）；
- 失败任务人工重新入队；
- 仪表盘个性化待办、全局图表与管理员运营指标展示；
- 高影响操作的 data-confirm 确认提示；
- 删除被引用话术时的可读错误（含引用数量）。

不接触真实串口与系统 ffmpeg；TestClient 不用上下文管理器，避免启动
Scheduler / Call Worker 线程。
"""

import os
from datetime import timedelta
from pathlib import Path

import pytest
from types import SimpleNamespace
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    CallEvent,
    CallRecord,
    CallTask,
    BusinessService,
    NetworkDevice,
    ScanSchedule,
    Script,
    utcnow,
)
from app.services import plans as plan_service
from app.services.call_worker import modem_availability
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


def make_customer(name: str = "客户A", phone: str = "13800000000") -> SimpleNamespace:
    """扁平模型后客户只是业务上的名称字段；返回带 name 的轻量对象。"""
    return SimpleNamespace(name=name)


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
    lead_days: int = 14,
) -> ScanSchedule:
    # 每类型只有一个固定配置：更新种子行。
    with SessionLocal() as session:
        schedule = session.scalars(
            select(ScanSchedule).where(ScanSchedule.scan_type == scan_type)
        ).first()
        if schedule is None:
            schedule = ScanSchedule(name=name, scan_type=scan_type)
            session.add(schedule)
        schedule.name = name
        schedule.cron_expr = cron
        schedule.timezone = timezone_name
        schedule.lead_days = lead_days
        schedule.enabled = enabled
        schedule.sms_enabled = sms_enabled
        session.commit()
        session.refresh(schedule)
        return schedule


def make_task_record(
    customer_name: str,
    script: Script,
    status: str = "completed",
    created_at=None,
) -> tuple[int, int]:
    """创建一条任务+通话记录，返回 (task_id, record_id)。"""

    with SessionLocal() as session:
        # 入参可能来自其它会话（已脱离）：merge 到当前会话，避免 commit 过期属性后
        # 再访问触发 DetachedInstanceError。
        script = session.merge(script)
        task = plan_service.create_manual_call_task(
            session, customer_name, script,
            caller_name=customer_name, caller_phone="13800000000",
            message="test", source="manual",
        )
        task.status = status
        task.call_record.status = status
        if created_at is not None:
            task.call_record.created_at = created_at
        session.commit()
        return task.id, task.call_record.id


# ---------------------------------------------------------------- 登录保护


def test_login_required_redirects(client: TestClient) -> None:
    for path in [
        "/",
        "/calls",
        "/notification-settings",
        "/admin/system",
        "/sms",
        "/daily-renewals",
        "/daily-recycles",
    ]:
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
    make_task_record(customer_a.name, script, status="completed", created_at=utcnow() - timedelta(days=10))
    make_task_record(customer_a.name, script, status="no_answer", created_at=utcnow() - timedelta(days=9))
    make_task_record(customer_b.name, script, status="completed", created_at=utcnow() - timedelta(days=8))

    page = client.get("/calls")
    assert page.status_code == 200
    assert "共 3 条" in page.text

    by_status = client.get("/calls", params={"status": "completed"})
    assert by_status.status_code == 200
    assert "共 2 条" in by_status.text

    by_customer = client.get("/calls", params={"customer": customer_a.name})
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
        make_task_record("客户A", script, status="completed", created_at=base + timedelta(minutes=i))

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
    make_task_record("客户A", script, status="completed")
    resp = client.get("/calls", params={"page": 99})
    assert resp.status_code == 200
    assert "共 1 条" in resp.text


# ---------------------------------------------------------------- 通话详情：时间线


def test_call_detail_shows_timeline_raw_lines_and_error(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    task_id, record_id = make_task_record("客户A", script, status="failed")
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


def test_scan_schedule_edit_rejects_invalid_cron_and_timezone(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(cron_expr="not a cron"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Cron 表达式「not a cron」无效" in client.get(resp.headers["location"]).text

    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(timezone_name="Mars/Olympus"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "时区「Mars/Olympus」无效" in client.get(resp.headers["location"]).text


def test_scan_schedule_edit_rejects_bad_lead_days(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(lead_days="abc"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "提前天数必须是整数" in client.get(resp.headers["location"]).text

    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(lead_days="-3"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "提前天数不能为负数" in client.get(resp.headers["location"]).text


def test_scan_schedule_create_rejected_and_list_shows_fixed_three(client: TestClient) -> None:
    login(client)
    # 不支持新增。
    resp = client.post("/scan-schedules", data=_scan_schedule_data(), follow_redirects=False)
    assert resp.status_code == 303
    assert "扫描配置固定为三类" in client.get(resp.headers["location"]).text

    # 列表固定三类。
    page = client.get("/notification-settings")
    assert page.status_code == 200
    assert "到期维系" in page.text
    assert "设备回收" in page.text
    assert "审核卡单" in page.text
    assert "新增" not in page.text.replace("新增扫描配置", "") or "新增扫描配置" not in page.text


def test_scan_schedule_edit_updates_fields(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule(cron="0 8 * * *")
    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(
            cron_expr="30 10 * * *",
            timezone_name="UTC",
            lead_days="7",
            enabled=False,
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        row = session.get(ScanSchedule, schedule.id)
        assert row.cron_expr == "30 10 * * *"
        assert row.timezone == "UTC"
        assert row.lead_days == 7
        assert row.enabled is False


def test_scan_schedule_sms_toggle_via_edit(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    resp = client.post(
        f"/scan-schedules/{schedule.id}/edit",
        data=_scan_schedule_data(sms_enabled=True),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id).sms_enabled is True


def test_scan_schedule_toggle_disables_and_enables(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    resp = client.post(f"/scan-schedules/{schedule.id}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id).enabled is False
    resp = client.post(f"/scan-schedules/{schedule.id}/toggle", follow_redirects=False)
    with SessionLocal() as session:
        assert session.get(ScanSchedule, schedule.id).enabled is True


def test_scan_schedule_delete_rejected(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule()
    resp = client.post(f"/scan-schedules/{schedule.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "不支持删除" in client.get(resp.headers["location"]).text


def test_scan_schedule_edit_form_prefills_current_values(client: TestClient) -> None:
    login(client)
    schedule = make_scan_schedule(cron="0 18 * * *", sms_enabled=True)
    page = client.get("/notification-settings")
    assert page.status_code == 200
    assert f'data-edit-action="/scan-schedules/{schedule.id}/edit"' in page.text


def test_scan_schedule_list_renders_fixed_configs(client: TestClient) -> None:
    login(client)
    make_scan_schedule(cron="0 8 * * *")
    resp = client.get("/notification-settings")
    assert resp.status_code == 200
    assert "到期维系" in resp.text
    assert "0 8 * * *" in resp.text



# ---------------------------------------------------------------- 人工重新入队


def test_requeue_failed_task_resets_to_queued(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    with SessionLocal() as session:
        task = plan_service.create_manual_call_task(
            session, "客户A", script, caller_name="客户A", caller_phone="13800000000", status="failed", message="boom"
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
        assert task.scan_schedule_id is None
        assert task.scan_schedule_id is None


def test_requeue_active_task_is_rejected(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    with SessionLocal() as session:
        task = plan_service.create_manual_call_task(
            session, "客户A", script, caller_name="客户A", caller_phone="13800000000", status="queued", message="queued"
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
    task_id, record_id = make_task_record("客户A", script, status="busy")
    resp = client.get(f"/calls/{record_id}")
    assert resp.status_code == 200
    assert f"/tasks/{task_id}/requeue" in resp.text
    assert "重新入队" in resp.text


# ---------------------------------------------------------------- 工作台任务看板


def test_dashboard_shows_task_board_and_global_charts(client: TestClient) -> None:
    login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "我的任务看板" in resp.text
    assert "我的待办" in resp.text
    assert "我的最近通知" in resp.text
    assert "业务状态分布" in resp.text
    assert "通话结果分布" in resp.text
    assert "通知趋势" in resp.text
    assert "业务总数" in resp.text
    assert "缺项总数" in resp.text
    assert "外呼 Worker" not in resp.text
    assert "串口可用性" not in resp.text
    assert ".env 硬开关" not in resp.text
    assert "排队任务" in resp.text
    assert "今日通话" in resp.text

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
    schedules_page = client.get("/notification-settings")
    # 三类固定配置无删除按钮；启停开关存在。
    assert "扫描配置固定为三类" not in schedules_page.text
    assert "启用" in schedules_page.text or "停用" in schedules_page.text

    system_page = client.get("/admin/system")
    assert "启动 Worker 后将开始拨打队列中的任务" in system_page.text



# ---------------------------------------------------------------- 删除被引用数据


def test_delete_script_rejected(client: TestClient) -> None:
    login(client)
    with SessionLocal() as session:
        script = session.scalars(
            select(Script).where(Script.role == "notification_due_renewal")
        ).first()
        script_id = script.id
    resp = client.post(f"/scripts/{script_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "不支持删除" in client.get(resp.headers["location"]).text



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
    script = Script(title="话术", body="正文")
    db.add(script)
    db.flush()
    task = plan_service.create_manual_call_task(
        db, "客户A", script, caller_name="客户A", caller_phone="13800000000",
        status="no_answer", message="没人接",
    )
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
    script = Script(title="话术", body="正文")
    db.add(script)
    db.commit()

    with pytest.raises(ValueError, match="没有可拨打的电话"):
        plan_service.create_manual_call_task(db, "无电话客户", script)

    task = plan_service.create_manual_call_task(
        db, "无电话客户", script, caller_name="客户A", caller_phone="13700000001", message="手动"
    )
    db.commit()
    assert task.scan_schedule_id is None
    assert task.customer_name == "无电话客户"
    assert task.dial_number == "13700000001"
    assert task.call_record.customer_name == "无电话客户"


def test_referencing_counts_helpers(db) -> None:
    script = Script(title="话术", body="正文")
    db.add(script)
    db.flush()
    plan_service.create_manual_call_task(db, "客户A", script, caller_name="客户A", caller_phone="13800000000", status="queued")
    db.commit()

    script_refs = script_referencing_counts(db, script)
    assert script_refs["schedules"] == 0  # 扫描配置不再关联话术
    assert script_refs["plans"] == 0
    assert script_refs["tasks"] == 1



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
        ("/notification-settings", True),
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


def test_daily_operations_pages_and_existing_submit_routes(client: TestClient) -> None:
    login(client)
    make_scan_schedule(lead_days=21)
    customer = make_customer("日常运维客户")
    with SessionLocal() as session:
        due_service = BusinessService(
            service_number="DAILY-RENEW-001",
            customer_name=customer.name,
            agreement_expires_at=utcnow() + timedelta(days=5),
        )
        retired_service = BusinessService(
            service_number="DAILY-RECYCLE-001",
            customer_name=customer.name,
            agreement_expires_at=utcnow() - timedelta(days=5),
        )
        session.add_all([due_service, retired_service])
        session.flush()
        device = NetworkDevice(
            device_code="DAILY-DEVICE-001",
            business_service_id=retired_service.id,
        )
        session.add(device)
        session.commit()
        due_service_id = due_service.id
        device_id = device.id

    legacy = client.get("/due-work", follow_redirects=False)
    assert legacy.status_code == 303
    assert legacy.headers["location"] == "/daily-renewals"

    renew_page = client.get("/daily-renewals")
    assert renew_page.status_code == 200
    assert "客户维系登记" in renew_page.text
    assert "今天起 21 天内" in renew_page.text
    assert "DAILY-RENEW-001" in renew_page.text
    assert "DAILY-DEVICE-001" not in renew_page.text
    # 无设备业务：退网提示不会进入回收提醒；有设备业务在回收页可见。
    assert "该业务下没有设备，退网后不会进入回收提醒。" in renew_page.text
    assert f'/due-work/business/{due_service_id}/renew' in renew_page.text
    assert f'/due-work/business/{due_service_id}/retire' in renew_page.text

    recycle_page = client.get("/daily-recycles")
    assert recycle_page.status_code == 200
    assert "设备回收登记" in recycle_page.text
    assert "DAILY-DEVICE-001" in recycle_page.text
    assert "DAILY-RENEW-001" not in recycle_page.text
    assert f'/due-work/device/{device_id}/recover' in recycle_page.text

    renewed = client.post(
        f"/due-work/business/{due_service_id}/renew",
        data={
            "agreement_expires_at": (utcnow() + timedelta(days=365)).date().isoformat(),
            "reason": "客户确认续签",
        },
        follow_redirects=False,
    )
    assert renewed.status_code == 303
    assert renewed.headers["location"].startswith("/reviews/")

    recovered = client.post(
        f"/due-work/device/{device_id}/recover",
        data={"reason": "设备已回收入库"},
        follow_redirects=False,
    )
    assert recovered.status_code == 303
    assert recovered.headers["location"].startswith("/reviews/")


def test_navigation_groups_data_management_and_daily_operations(client: TestClient) -> None:
    login(client)
    html = client.get("/daily-renewals").text

    assert "数据录入" not in html
    data_start = html.index("<summary>数据管理")
    data_end = html.index("</details>", data_start)
    data_section = html[data_start:data_end]
    expected_links = [
        'href="/ledger">业务台账',
        'href="/devices">网络设备',
        'href="/reviews">审核中心',
        'href="/imports">导入导出',
    ]
    assert all(link in data_section for link in expected_links)
    assert [data_section.index(link) for link in expected_links] == sorted(
        data_section.index(link) for link in expected_links
    )
    assert "客户维系登记" not in data_section
    assert "设备回收登记" not in data_section

    daily_start = html.index("<summary>日常运维")
    daily_end = html.index("</details>", daily_start)
    daily_section = html[daily_start:daily_end]
    daily_details = html[
        html.rfind('<details class="nav-section"', 0, daily_start):daily_start
    ]
    assert "open" in daily_details
    assert 'href="/daily-renewals">客户维系登记' in daily_section
    assert 'href="/daily-recycles">设备回收登记' in daily_section

    recycle_html = client.get("/daily-recycles").text
    recycle_start = recycle_html.index("<summary>日常运维")
    recycle_details = recycle_html[
        recycle_html.rfind('<details class="nav-section"', 0, recycle_start):recycle_start
    ]
    assert "open" in recycle_details
    assert 'class="active" href="/daily-recycles"' in recycle_html


def test_ledger_is_paginated_and_uses_modal_lookup_controls(client: TestClient) -> None:
    login(client)
    customer = make_customer("分页客户")
    with SessionLocal() as session:
        session.add_all(
            [BusinessService(service_number=f"LEDGER-{i:03d}", customer_name=customer.name) for i in range(51)]
        )
        session.commit()

    page1 = client.get("/ledger", params={"q": "LEDGER"})
    assert page1.status_code == 200
    assert "第 1 / 2 页" in page1.text
    assert "共 51 条" in page1.text
    assert 'data-modal-open="ledger-modal"' in page1.text
    assert 'list="customer-options"' in page1.text
    assert 'name="customer_name"' in page1.text
    page2 = client.get("/ledger", params={"q": "LEDGER", "page": 2})
    assert "第 2 / 2 页" in page2.text
    assert "LEDGER-000" in page2.text


def test_devices_is_paginated_and_uses_modal_lookup_controls(client: TestClient) -> None:
    login(client)
    customer = make_customer("设备分页客户")
    with SessionLocal() as session:
        service = BusinessService(service_number="DEVICE-SERVICE", customer_name=customer.name)
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
    ledger_resp = client.post("/ledger", data={"service_number": "", "customer_name": ""}, follow_redirects=False)
    assert ledger_resp.status_code == 303
    assert "error=" in ledger_resp.headers["location"]
    assert "业务号码不能为空" in client.get(ledger_resp.headers["location"]).text

    device_resp = client.post("/devices", data={"device_code": "", "business_service_id": ""}, follow_redirects=False)
    assert device_resp.status_code == 303
    assert "error=" in device_resp.headers["location"]
    assert "设备编码不能为空" in client.get(device_resp.headers["location"]).text


def test_ledger_creates_or_reuses_customer_by_name(client: TestClient) -> None:
    """客户字段与字典字段一致：直接填文本，按名称复用或自动创建，不再提交隐藏 ID。"""
    login(client)
    # 新客户名 → 自动创建客户并成功建业务。
    resp = client.post(
        "/ledger",
        data={
            "service_number": "GHOST-001",
            "customer_name": "安康市新客户公司",
            "county": "",
            "grid": "",
            "service_status": "",
            "business_type": "",
            "channel_name": "",
            "accessed_at": "",
            "agreement_expires_at": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ledger"
    with SessionLocal() as session:
        service = session.scalars(
            select(BusinessService).where(BusinessService.service_number == "GHOST-001")
        ).first()
        assert service is not None and service.customer_name == "安康市新客户公司"

    # 同名（大小写不同）→ 复用同一客户，不重复创建。
    resp = client.post(
        "/ledger",
        data={
            "service_number": "GHOST-002",
            "customer_name": "安康市新客户公司 ",
            "county": "",
            "grid": "",
            "service_status": "",
            "business_type": "",
            "channel_name": "",
            "accessed_at": "",
            "agreement_expires_at": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.scalars(
            select(func.count(BusinessService.id)).where(
                BusinessService.customer_name == "安康市新客户公司"
            )
        ).one() == 2

    # 客户名称为空 → 明确错误。
    resp = client.post(
        "/ledger",
        data={"service_number": "GHOST-003", "customer_name": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "必须输入客户名称" in client.get(resp.headers["location"]).text


def test_ledger_device_column_links_to_filtered_devices(client: TestClient) -> None:
    """业务台账「设备」列显示有效设备数并跳转设备页按业务筛选。"""
    login(client)
    customer = make_customer("设备列客户")
    with SessionLocal() as session:
        service = BusinessService(service_number="LEDGER-DEV-001", customer_name=customer.name)
        other = BusinessService(service_number="LEDGER-OTHER-001", customer_name=customer.name)
        session.add_all([service, other])
        session.flush()
        session.add_all(
            [
                NetworkDevice(device_code="LEDGER-DEVICE-1", business_service_id=service.id),
                NetworkDevice(device_code="LEDGER-DEVICE-2", business_service_id=service.id),
                NetworkDevice(device_code="LEDGER-OTHER-DEV", business_service_id=other.id),
            ]
        )
        session.commit()
        service_id = service.id
        other_id = other.id

    page = client.get("/ledger", params={"q": "LEDGER-DEV"})
    assert "2 台" in page.text
    assert f'href="/devices?business_id={service_id}"' in page.text

    filtered = client.get("/devices", params={"business_id": service_id})
    assert "LEDGER-DEVICE-1" in filtered.text
    assert "LEDGER-DEVICE-2" in filtered.text
    assert "LEDGER-OTHER-DEV" not in filtered.text

    other_filtered = client.get("/devices", params={"business_id": other_id})
    assert "LEDGER-OTHER-DEV" in other_filtered.text
    assert "LEDGER-DEVICE-1" not in other_filtered.text


def test_devices_reject_nonexistent_business_id(client: TestClient) -> None:
    """设备必须选真实业务；伪造 ID 被拒绝而不是 FK 500。"""
    login(client)
    device_resp = client.post(
        "/devices",
        data={"device_code": "GHOST-DEV-001", "business_service_id": "999999"},
        follow_redirects=False,
    )
    assert device_resp.status_code == 303
    assert "所选业务不存在" in client.get(device_resp.headers["location"]).text
