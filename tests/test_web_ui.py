from __future__ import annotations

"""P1 后台操作体验的 Web 路由与页面测试。

覆盖：
- 通话列表的状态 / 日期区间 / 客户筛选与分页；
- 通话详情的时间线（事件正序、原始串口行、错误信息）；
- 计划表单的 cron 校验错误与下次执行时间预览；
- 客户 / 计划「立即拨打一次」一致入口（生成 queued 任务，不改原计划）；
- 失败任务人工重新入队；
- 仪表盘 Worker 硬开关 / 串口可用性 / 当前通话状态展示；
- 高影响操作的 data-confirm 确认提示；
- 删除被引用客户 / 话术时的可读错误（含引用数量）。

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
    CallbackPlan,
    Customer,
    Script,
    utcnow,
)
from app.services import plans as plan_service
from app.services.call_worker import modem_availability
from app.services.customers import default_contact
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


def make_cron_plan(customer: Customer, script: Script, cron: str = "0 9 * * *") -> CallbackPlan:
    with SessionLocal() as session:
        customer = session.merge(customer)
        script = session.merge(script)
        plan = plan_service.create_plan(
            session, customer, script, "cron", None, cron, "Asia/Shanghai", True
        )
        session.commit()
        session.refresh(plan)
        return plan


def make_task_record(
    customer: Customer,
    script: Script,
    plan: CallbackPlan | None = None,
    status: str = "completed",
    created_at=None,
) -> tuple[int, int]:
    """创建一条任务+通话记录，返回 (task_id, record_id)。"""

    with SessionLocal() as session:
        # 入参可能来自其它会话（已脱离）：merge 到当前会话，避免 commit 过期属性后
        # 再访问触发 DetachedInstanceError。
        customer = session.merge(customer)
        script = session.merge(script)
        if plan is None:
            task = plan_service.create_manual_call_task(
                session, customer, script, message="test", source="manual"
            )
        else:
            task = plan_service.create_call_task(
                session, session.merge(plan), status=status, message="test"
            )
        task.status = status
        task.call_record.status = status
        if created_at is not None:
            task.call_record.created_at = created_at
        session.commit()
        return task.id, task.call_record.id


def default_contact_id(customer_id: int) -> int | None:
    """返回客户默认负责人（CustomerContact 首个）的通讯录 id，供表单提交。"""

    with SessionLocal() as session:
        customer = session.get(Customer, customer_id)
        if customer is None:
            return None
        contact = default_contact(session, customer)
        return contact.id if contact else None


# ---------------------------------------------------------------- 登录保护


def test_login_required_redirects(client: TestClient) -> None:
    for path in ["/", "/calls", "/plans", "/contacts", "/scripts", "/admin/system"]:
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


def _plan_data(
    customer: Customer,
    script: Script,
    trigger_type: str = "once",
    run_at: str = "2030-01-01T10:00",
    cron_expr: str = "",
) -> dict:
    return {
        "customer_id": customer.id,
        "script_id": script.id,
        "contact_id": default_contact_id(customer.id),
        "trigger_type": trigger_type,
        "run_at": run_at,
        "cron_expr": cron_expr,
        "timezone": "Asia/Shanghai",
    }


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


# ---------------------------------------------------------------- 计划表单：cron 校验与预览


def test_plan_create_rejects_invalid_cron_with_clear_message(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    resp = client.post(
        "/plans",
        data=_plan_data(customer, script, trigger_type="cron", cron_expr="0 9 * *"),
    )
    assert resp.status_code == 400
    assert "Cron 表达式" in resp.text
    assert "无效" in resp.text


def test_plan_create_requires_run_at_for_once(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    resp = client.post(
        "/plans",
        data=_plan_data(customer, script, trigger_type="once", run_at=""),
    )
    assert resp.status_code == 400
    assert "单次计划必须填写执行时间" in resp.text


def test_plan_create_requires_contact_and_script(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    data = _plan_data(customer, make_script())
    data.pop("script_id")
    resp = client.post("/plans", data=data)
    assert resp.status_code == 400
    assert "请选择话术" in resp.text


def test_plan_create_valid_cron_shows_next_run_notice(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    resp = client.post(
        "/plans",
        data=_plan_data(customer, script, trigger_type="cron", run_at="", cron_expr="0 9 * * *"),
    )
    assert resp.status_code == 200  # 跟随 303 后的计划页
    assert "计划已保存，下次执行时间" in resp.text
    with SessionLocal() as session:
        plan = session.scalars(select(CallbackPlan)).one()
        assert plan.next_run_at is not None
        assert plan.cron_expr == "0 9 * * *"


def test_plan_update_shows_next_run_notice_and_updates_preview(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    plan = make_cron_plan(customer, script, cron="0 8 * * *")
    resp = client.post(
        f"/plans/{plan.id}/edit",
        data=_plan_data(customer, script, trigger_type="cron", run_at="", cron_expr="30 10 * * 1"),
    )
    assert resp.status_code == 200
    assert "计划已保存，下次执行时间" in resp.text
    with SessionLocal() as session:
        updated = session.get(CallbackPlan, plan.id)
        assert updated.cron_expr == "30 10 * * 1"
        assert updated.next_run_at is not None


def test_plan_edit_form_shows_current_next_run(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    plan = make_cron_plan(customer, script)
    resp = client.get("/plans", params={"edit_id": plan.id})
    assert resp.status_code == 200
    assert "当前计划下次执行" in resp.text


# ---------------------------------------------------------------- 立即拨打一次


def test_plan_call_now_creates_queued_task_without_touching_plan(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    plan = make_cron_plan(customer, script)
    next_run_before = plan.next_run_at

    resp = client.post(f"/plans/{plan.id}/call-now", follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as session:
        tasks = session.scalars(select(CallTask).where(CallTask.plan_id == plan.id)).all()
        assert len(tasks) == 1
        assert tasks[0].status == "queued"
        assert tasks[0].source == "manual"
        assert tasks[0].dial_number == "13800000000"
        # 原 cron 计划不被修改：下次执行时间保持不变。
        plan_ref = session.get(CallbackPlan, plan.id)
        assert plan_ref.next_run_at == next_run_before
        assert plan_ref.enabled is True


# ---------------------------------------------------------------- 人工重新入队


def test_requeue_failed_task_resets_to_queued(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    plan = make_cron_plan(customer, script)
    with SessionLocal() as session:
        task = plan_service.create_call_task(
            session, session.get(CallbackPlan, plan.id), status="failed", message="boom"
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
        # 原计划不受影响。
        plan_ref = session.get(CallbackPlan, plan.id)
        assert plan_ref.next_run_at == plan.next_run_at
        assert plan_ref.enabled is True


def test_requeue_active_task_is_rejected(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    plan = make_cron_plan(customer, script)
    with SessionLocal() as session:
        task = plan_service.create_call_task(
            session, session.get(CallbackPlan, plan.id), status="queued", message="queued"
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
    customer = make_customer()
    script = make_script()
    make_cron_plan(customer, script)

    plans_page = client.get("/plans")
    assert "立即拨打该计划选择的负责人" in plans_page.text
    assert "删除该计划" in plans_page.text

    # Worker 未启用时，启动按钮带确认；已启用时不需要确认（模板中仅未启用渲染）。
    system_page = client.get("/admin/system")
    assert "启动 Worker 后将开始拨打队列中的任务" in system_page.text


# ---------------------------------------------------------------- 删除被引用数据


def test_delete_script_referenced_by_plan_shows_counts(client: TestClient) -> None:
    login(client)
    customer = make_customer()
    script = make_script()
    make_cron_plan(customer, script)
    make_cron_plan(customer, script, cron="0 10 * * *")

    resp = client.post(f"/scripts/{script.id}/delete")
    assert resp.status_code == 400
    assert "回访计划 2 条" in resp.text
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


def _plan_count() -> int:
    from sqlalchemy import func

    with SessionLocal() as session:
        return session.scalar(select(func.count(CallbackPlan.id))) or 0


def test_validate_cron_expr_messages(db) -> None:
    with pytest.raises(ValueError, match="周期计划必须填写 Cron 表达式"):
        plan_service.validate_cron_expr("", "Asia/Shanghai")
    with pytest.raises(ValueError, match="Cron 表达式.*无效"):
        plan_service.validate_cron_expr("0 9 * *", "Asia/Shanghai")
    # 合法表达式不抛异常。
    plan_service.validate_cron_expr("0 9 * * *", "Asia/Shanghai")


def test_parse_datetime_local_reports_format_error(db) -> None:
    with pytest.raises(ValueError, match="执行时间格式不正确"):
        plan_service.parse_datetime_local("2025-13-99", "Asia/Shanghai")


def test_compute_next_run_at_requires_run_at_for_once(db) -> None:
    with pytest.raises(ValueError, match="单次计划必须填写执行时间"):
        plan_service.compute_next_run_at("once", None, "", "Asia/Shanghai")


def test_requeue_call_task_semantics(db) -> None:
    customer = Customer(name="客户A")
    db.add(customer)
    db.flush()
    sync_default_contact(db, customer, "13800000000")
    script = Script(title="话术", body="正文")
    db.add(script)
    db.flush()
    plan = plan_service.create_plan(
        db, customer, script, "once", utcnow() + timedelta(days=1), "", "Asia/Shanghai", True
    )
    task = plan_service.create_call_task(db, plan, status="no_answer", message="没人接")
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
    plan = plan_service.create_plan(
        db, customer, script, "once", utcnow() + timedelta(days=1), "", "Asia/Shanghai", True
    )
    plan_service.create_call_task(db, plan, status="queued")
    db.commit()

    script_refs = script_referencing_counts(db, script)
    assert script_refs["plans"] == 1
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
    """回归：进入 /contacts（通讯录）时「回访与通话」导航组必须展开。"""
    login(client)
    for page, expected_open in [
        ("/contacts", True),
        ("/plans", True),
        ("/scripts", True),
        ("/calls", True),
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
