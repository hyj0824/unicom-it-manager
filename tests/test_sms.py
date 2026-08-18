from __future__ import annotations

"""短信通知链路测试。

覆盖：
- send_sms_text 协议：成功（+CMGS: 1）、CMS ERROR 抛错、> 提示符超时、
  最终响应超时、非 ASCII 内容转 UCS2、SIM 未就绪、弱信号警告不阻断；
- CallWorker 空闲时处理 pending 短信：成功置 sent、失败置 failed 且不阻塞
  后续语音任务、SMS_ENABLED=0 不处理、串口打不开记录日志不崩溃、批次上限；
- 三个扫描生成任务时按配置同步入队 SmsNotification（两个开关任一关闭不创建）；
- /sms 页面：未登录 303、登录后 200 且号码脱敏展示。

fake serial 参考 tests/test_call_worker.py 的 FakeModem 模式：只与内存假串口
交互，不打开真实串口、不发送短信、不拨号；TTS_PROVIDER=none（conftest）。
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.audio import PlaybackResult
from app.config import Settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    BusinessService,
    CallRecord,
    CallTask,
    ChangeItem,
    ChangeSet,
    Contact,
    Customer,
    CustomerContact,
    NetworkDevice,
    Permission,
    Role,
    RolePermission,
    ScanSchedule,
    Script,
    SmsNotification,
    User,
    UserRole,
    utcnow,
)
from app.services import scans
from app.services.call_worker import CallWorker, CallWorkerService
from app.services.sms import SmsError, encode_ucs2_hex, is_ascii, send_sms_text

BASE_DIR = Path(__file__).resolve().parent.parent
DB_URL = os.environ["DATABASE_URL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

# 固定“当前时间”：2026-08-18 10:00（北京时间），避免扫描测试依赖真实时钟。
NOW = datetime(2026, 8, 18, 2, 0, 0, tzinfo=timezone.utc)
# 2026-08-18 00:00（北京时间）= 2026-08-17 16:00 UTC。
DAY_START_UTC = datetime(2026, 8, 17, 16, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 基础设施


@pytest.fixture()
def db(tmp_path: Path):
    db_path = tmp_path / "sms.db"
    url = f"sqlite:///{db_path}"
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine_ = create_engine(url)
    session = Session(engine_)
    yield session
    session.close()
    engine_.dispose()


def _settings(**overrides) -> Settings:
    base = dict(
        admin_password="test",
        session_secret="test",
        database_url="sqlite:///:memory:",
        modem_port="/dev/ttyFAKE",
        modem_baud=115200,
        audio_device="plughw:0,0",
        call_connect_timeout_seconds=1,
        rejected_end_seconds=20,
        min_connected_seconds=3,
        retry_delay_seconds=60,
        max_call_attempts=2,
        tts_provider="none",
        tts_api_key="",
        tts_voice="",
        default_timezone="Asia/Shanghai",
        call_worker_enabled=False,
        worker_poll_seconds=1,
        sms_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


class ScriptedModem:
    """按脚本回放串口行的假 ModemClient（send_sms_text 协议测试用）。"""

    def __init__(self, lines=()):
        self.lines = list(lines)
        self.commands: list[str] = []
        self.writes: list[bytes] = []

    def send_command(self, command: str) -> None:
        self.commands.append(command)

    def write_bytes(self, data: bytes) -> None:
        self.writes.append(data)

    def read_line(self) -> str:
        if self.lines:
            return self.lines.pop(0)
        time.sleep(0.002)
        return ""


# 成功发送一条 ASCII 短信所需的串口行序列（见 app/services/sms.py 注释的流程）。
SMS_LINES_OK = [
    "OK",  # AT+CMEE=1
    "+CPIN: READY",
    "OK",
    "+CSQ: 24,0",
    "OK",  # AT+CMGF=1
    "OK",  # AT+CSCS="IRA"
    "OK",
    ">",  # AT+CMGS 输入提示符
    "+CMGS: 1",
]
SMS_PHONE = "13800000000"
SMS_ASCII_CONTENT = "Renew contract reminder"
SMS_UCS2_CONTENT = "协议到期提醒，请及时处理"


@pytest.fixture()
def fake_modem(monkeypatch):
    """替换串口与系统 ffmpeg：Worker 只与内存假串口交互（共享行队列=单通道）。"""

    from app.services import call_worker as worker_module

    holder = {"lines": [], "play_success": True, "instances": []}

    class FakeModem:
        def __init__(self, *args, **kwargs):
            self.commands: list[str] = []
            self.writes: list[bytes] = []
            holder["instances"].append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_command(self, command: str) -> None:
            self.commands.append(command)

        def write_bytes(self, data: bytes) -> None:
            self.writes.append(data)

        def dial(self, phone: str) -> None:
            self.send_command(f"ATD{phone};")

        def hangup(self) -> None:
            self.send_command("AT+CHUP")

        def read_line(self) -> str:
            if holder["lines"]:
                return holder["lines"].pop(0)
            time.sleep(0.003)
            return ""

    monkeypatch.setattr(worker_module, "ModemClient", FakeModem)

    def configure(lines=(), play_success: bool = True) -> None:
        holder["lines"] = list(lines)
        holder["play_success"] = play_success
        monkeypatch.setattr(
            worker_module,
            "play_audio",
            lambda path, dev: PlaybackResult(
                play_success, 0 if play_success else 1, "" if play_success else "playback failed"
            ),
        )

    configure()

    class Context:
        @property
        def instances(self):
            return holder["instances"]

        def all_commands(self) -> list[str]:
            return [c for modem in holder["instances"] for c in modem.commands]

        def all_writes(self) -> list[bytes]:
            return [w for modem in holder["instances"] for w in modem.writes]

    return Context(), configure


def _make_voice_task(
    db: Session, tmp_path: Path, phone: str = "13800000000"
) -> CallTask:
    customer = Customer(name="测试客户")
    db.add(customer)
    script = Script(title="话术", body="正文")
    wav_path = tmp_path / "audio.wav"
    wav_path.write_bytes(b"RIFF")
    script.wav_path = str(wav_path)
    db.add(script)
    db.flush()
    task = CallTask(
        customer=customer,
        script=script,
        dial_number=phone,
        due_at=utcnow(),
        status="queued",
        max_attempts=2,
    )
    db.add(task)
    db.add(
        CallRecord(
            task=task,
            customer=customer,
            script=script,
            dial_number=phone,
            status="queued",
        )
    )
    db.flush()
    return task


# ---------------------------------------------------------------- send_sms_text 协议


def test_send_sms_text_success_flow() -> None:
    modem = ScriptedModem(SMS_LINES_OK)
    send_sms_text(modem, SMS_PHONE, SMS_ASCII_CONTENT)
    assert modem.commands == [
        "AT+CMEE=1",
        "AT+CPIN?",
        "AT+CSQ",
        "AT+CMGF=1",
        'AT+CSCS="IRA"',
        f'AT+CMGS="{SMS_PHONE}"',
    ]
    # 文本内容 + Ctrl+Z(0x1A)，不带回车。
    assert modem.writes == [SMS_ASCII_CONTENT.encode("ascii") + b"\x1a"]


def test_send_sms_text_cms_error_raises_readable_sms_error() -> None:
    lines = SMS_LINES_OK[:-1] + ["+CMS ERROR: 331"]
    modem = ScriptedModem(lines)
    with pytest.raises(SmsError, match="331"):
        send_sms_text(modem, SMS_PHONE, SMS_ASCII_CONTENT)
    error = None
    try:
        send_sms_text(ScriptedModem(lines), SMS_PHONE, SMS_ASCII_CONTENT)
    except SmsError as exc:
        error = str(exc)
    assert "无网络服务" in error  # CMS 错误码映射为可读文案


def test_send_sms_text_prompt_timeout_raises(monkeypatch) -> None:
    monkeypatch.setattr("app.services.sms.PROMPT_TIMEOUT_S", 0.05)
    lines = SMS_LINES_OK[:-2]  # 不出现 ">" 提示符
    modem = ScriptedModem(lines)
    with pytest.raises(SmsError, match="提示符"):
        send_sms_text(modem, SMS_PHONE, SMS_ASCII_CONTENT)
    # 超时后不应写入报文。
    assert modem.writes == []


def test_send_sms_text_final_response_timeout_raises() -> None:
    lines = SMS_LINES_OK[:-1]  # 有 ">" 提示符，但没有最终响应
    modem = ScriptedModem(lines)
    with pytest.raises(SmsError, match="超时"):
        send_sms_text(modem, SMS_PHONE, SMS_ASCII_CONTENT, timeout_s=0.05)
    assert modem.writes == [SMS_ASCII_CONTENT.encode("ascii") + b"\x1a"]


def test_send_sms_text_non_ascii_content_uses_ucs2() -> None:
    assert not is_ascii(SMS_UCS2_CONTENT)
    modem = ScriptedModem(SMS_LINES_OK)
    send_sms_text(modem, SMS_PHONE, SMS_UCS2_CONTENT)
    assert modem.commands == [
        "AT+CMEE=1",
        "AT+CPIN?",
        "AT+CSQ",
        "AT+CMGF=1",
        'AT+CSCS="UCS2"',
        # UCS2 文本模式下号码也按 UCS2 十六进制编码（27.005 约定）。
        f'AT+CMGS="{encode_ucs2_hex(SMS_PHONE)}"',
    ]
    assert modem.writes == [encode_ucs2_hex(SMS_UCS2_CONTENT).encode("ascii") + b"\x1a"]


def test_encode_ucs2_hex_roundtrip() -> None:
    assert encode_ucs2_hex("你好") == "4F60597D"
    assert bytes.fromhex(encode_ucs2_hex("你好")).decode("utf-16-be") == "你好"


def test_send_sms_text_sim_not_ready_raises() -> None:
    modem = ScriptedModem(["OK", "+CPIN: SIM PIN"])
    with pytest.raises(SmsError, match="SIM 卡未就绪"):
        send_sms_text(modem, SMS_PHONE, SMS_ASCII_CONTENT)


def test_send_sms_text_weak_signal_warns_but_continues(caplog) -> None:
    lines = [
        "OK",
        "+CPIN: READY",
        "OK",
        "+CSQ: 8,0",  # RSSI 8 < 10：弱信号
        "OK",
        "OK",
        "OK",
        ">",
        "+CMGS: 2",
    ]
    modem = ScriptedModem(lines)
    with caplog.at_level(logging.WARNING, logger="app.services.sms"):
        send_sms_text(modem, SMS_PHONE, SMS_ASCII_CONTENT)  # 不抛错
    assert any("信号较弱" in record.message for record in caplog.records)


def test_send_sms_text_rejects_empty_phone_or_content() -> None:
    modem = ScriptedModem([])
    with pytest.raises(SmsError, match="号码为空"):
        send_sms_text(modem, "  ", SMS_ASCII_CONTENT)
    with pytest.raises(SmsError, match="内容为空"):
        send_sms_text(modem, SMS_PHONE, "")
    assert modem.commands == []  # 校验在任何 AT 命令之前


# ---------------------------------------------------------------- Worker 空闲发送


def test_worker_tick_sends_pending_sms_before_voice_task(fake_modem, db, tmp_path) -> None:
    ctx, configure = fake_modem
    service = CallWorkerService(_settings(sms_enabled=True))
    task = _make_voice_task(db, tmp_path)
    sms = SmsNotification(phone=SMS_PHONE, content=SMS_ASCII_CONTENT, status="pending")
    db.add(sms)
    db.commit()

    configure(lines=SMS_LINES_OK + ["VOICE CALL: BEGIN", "VOICE CALL: END: 000012"])

    service._tick(db)
    db.commit()
    db.refresh(sms)
    db.refresh(task)

    assert sms.status == "sent"
    assert sms.sent_at is not None
    assert sms.error_message == ""
    assert sms.attempt == 1
    assert task.status == "completed"
    # 单通道串行：短信命令必须先于拨号命令。
    commands = ctx.all_commands()
    assert commands.index(f'AT+CMGS="{SMS_PHONE}"') < commands.index("ATD13800000000;")
    assert ctx.all_writes() == [SMS_ASCII_CONTENT.encode("ascii") + b"\x1a"]


def test_failed_sms_marks_failed_and_does_not_block_voice_task(
    fake_modem, db, tmp_path
) -> None:
    ctx, configure = fake_modem
    service = CallWorkerService(_settings(sms_enabled=True))
    task = _make_voice_task(db, tmp_path)
    sms = SmsNotification(phone=SMS_PHONE, content=SMS_ASCII_CONTENT, status="pending")
    db.add(sms)
    db.commit()

    configure(
        lines=SMS_LINES_OK[:-1]
        + ["+CMS ERROR: 331"]
        + ["VOICE CALL: BEGIN", "VOICE CALL: END: 000012"]
    )

    service._tick(db)
    db.commit()
    db.refresh(sms)
    db.refresh(task)

    assert sms.status == "failed"
    assert "无网络服务" in sms.error_message
    assert sms.attempt == 1
    assert sms.sent_at is None
    # 短信失败不阻塞语音任务领取与执行。
    assert task.status == "completed"


def test_sms_disabled_by_settings_skips_processing(fake_modem, db) -> None:
    ctx, configure = fake_modem
    worker = CallWorker(_settings(sms_enabled=False))
    sms = SmsNotification(phone=SMS_PHONE, content=SMS_ASCII_CONTENT, status="pending")
    db.add(sms)
    db.commit()

    assert worker.process_pending_sms(db) == 0
    db.commit()
    db.refresh(sms)
    assert sms.status == "pending"
    assert ctx.instances == []  # 未打开串口


def test_no_pending_sms_does_not_open_serial(fake_modem, db) -> None:
    ctx, configure = fake_modem
    worker = CallWorker(_settings(sms_enabled=True))
    assert worker.process_pending_sms(db) == 0
    assert ctx.instances == []


def test_serial_open_failure_logs_and_does_not_crash(fake_modem, db, caplog, monkeypatch) -> None:
    from app.services import call_worker as worker_module

    class BrokenModem:
        def __init__(self, *args, **kwargs):
            raise OSError("serial port missing")

    monkeypatch.setattr(worker_module, "ModemClient", BrokenModem)
    worker = CallWorker(_settings(sms_enabled=True))
    sms = SmsNotification(phone=SMS_PHONE, content=SMS_ASCII_CONTENT, status="pending")
    db.add(sms)
    db.commit()

    # alembic fileConfig 的 disable_existing_loggers 会在 db fixture 迁移时禁用
    # 预先存在的应用 logger，这里显式恢复后再断言日志（见 alembic/env.py）。
    logging.getLogger("app.services.call_worker").disabled = False
    with caplog.at_level(logging.WARNING, logger="app.services.call_worker"):
        processed = worker.process_pending_sms(db)

    assert processed == 0
    db.commit()
    db.refresh(sms)
    assert sms.status == "pending"  # 打不开串口不误标记失败
    assert any("短信批量发送中止" in record.message for record in caplog.records)


def test_process_pending_sms_limits_batch_and_orders_by_id(fake_modem, db) -> None:
    ctx, configure = fake_modem
    worker = CallWorker(_settings(sms_enabled=True))
    for i in range(12):
        db.add(SmsNotification(phone=f"1380000000{i}", content="m", status="pending"))
    db.commit()
    configure(lines=SMS_LINES_OK * 12)

    assert worker.process_pending_sms(db, limit=10) == 10
    db.commit()

    statuses = db.scalars(
        select(SmsNotification.status).order_by(SmsNotification.id.asc())
    ).all()
    assert statuses[:10] == ["sent"] * 10
    assert statuses[10:] == ["pending"] * 2


# ---------------------------------------------------------------- 扫描入队短信


def _make_scan_data(
    db: Session,
    *,
    duty: str = "客户经理",
    phone: str = "13900000001",
    scan_type: str = "due_renewal",
    schedule_sms: bool = True,
    expires_at=None,
    device_code: str | None = None,
):
    """构造一次扫描所需的最小台账数据，返回 schedule。"""

    customer = Customer(name="客户A")
    db.add(customer)
    db.flush()
    contact = Contact(name="负责人", phone=phone)
    db.add(contact)
    db.flush()
    db.add(
        CustomerContact(
            customer_id=customer.id,
            contact_id=contact.id,
            duty=duty,
            is_active=True,
        )
    )
    service = BusinessService(
        service_number="1001",
        customer_id=customer.id,
        agreement_expires_at=expires_at or DAY_START_UTC + timedelta(days=6),
    )
    db.add(service)
    db.flush()
    if device_code:
        db.add(
            NetworkDevice(
                device_code=device_code,
                business_service_id=service.id,
            )
        )
    schedule = ScanSchedule(
        name="测试扫描",
        scan_type=scan_type,
        lead_days=14,
        timezone="Asia/Shanghai",
        sms_enabled=schedule_sms,
    )
    db.add(schedule)
    db.flush()
    return schedule


def test_due_renewal_creates_sms_notification_when_enabled(db, monkeypatch) -> None:
    monkeypatch.setattr(scans, "get_settings", lambda: _settings(sms_enabled=True))
    schedule = _make_scan_data(db)

    assert scans.run_due_renewal_scan(db, schedule, now=NOW) == 1
    db.flush()

    task = db.scalars(select(CallTask)).one()
    sms = db.scalars(select(SmsNotification)).one()
    assert sms.call_task_id == task.id
    assert sms.phone == "13900000001"
    assert sms.status == "pending"
    assert sms.attempt == 0
    meta = json.loads(task.meta_json)
    assert sms.content == meta["rendered_script"]
    assert "负责人" in sms.content


def test_device_recycle_creates_sms_notification_when_enabled(db, monkeypatch) -> None:
    monkeypatch.setattr(scans, "get_settings", lambda: _settings(sms_enabled=True))
    # 协议已过期 → 时间口径退网；设备未填回收状态 → 未回收。
    schedule = _make_scan_data(
        db,
        duty="网络维护责任人",
        scan_type="device_recycle",
        expires_at=DAY_START_UTC - timedelta(days=1),
        device_code="DEV-001",
    )

    assert scans.run_device_recycle_scan(db, schedule, now=NOW) == 1
    db.flush()

    task = db.scalars(select(CallTask)).one()
    sms = db.scalars(select(SmsNotification)).one()
    assert sms.call_task_id == task.id
    assert sms.phone == "13900000001"
    assert sms.status == "pending"
    assert "DEV-001" in sms.content


def test_review_stuck_creates_sms_notification_when_enabled(db, monkeypatch) -> None:
    monkeypatch.setattr(scans, "get_settings", lambda: _settings(sms_enabled=True))
    schedule = _make_scan_data(db, scan_type="review_stuck")
    user = User(
        username="auditor",
        password_hash="x",
        real_name="审核员",
        phone="13811112222",
        display_name="审核员",
        is_enabled=True,
    )
    db.add(user)
    db.flush()
    role = Role(code="role_auditor", name="审核角色")
    db.add(role)
    db.flush()
    perm = db.scalars(select(Permission).where(Permission.code == "review")).one()
    db.add(RolePermission(role_id=role.id, permission_id=perm.id, domain="business"))
    db.add(UserRole(user_id=user.id, role_id=role.id))
    service = db.scalars(select(BusinessService)).one()
    change_set = ChangeSet(title="修改协议到期时间", status="submitted")
    db.add(change_set)
    db.flush()
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

    assert scans.run_review_stuck_scan(db, schedule, now=NOW) == 1
    db.flush()

    task = db.scalars(select(CallTask)).one()
    sms = db.scalars(select(SmsNotification)).one()
    assert sms.call_task_id == task.id
    assert sms.phone == "13811112222"  # review_stuck 的拨号号码是 user.phone
    assert sms.status == "pending"
    assert "审核员" in sms.content


@pytest.mark.parametrize(
    "schedule_on,config_on",
    [(False, True), (True, False), (False, False)],
)
def test_scan_sms_not_created_when_either_switch_off(
    db, monkeypatch, schedule_on, config_on
) -> None:
    monkeypatch.setattr(scans, "get_settings", lambda: _settings(sms_enabled=config_on))
    schedule = _make_scan_data(db, schedule_sms=schedule_on)

    assert scans.run_due_renewal_scan(db, schedule, now=NOW) == 1
    db.flush()

    assert db.scalars(select(CallTask)).all()  # 语音任务照常生成
    assert db.scalars(select(SmsNotification)).all() == []


# ---------------------------------------------------------------- /sms 页面


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


def login(client: TestClient, password: str = ADMIN_PASSWORD) -> None:
    resp = client.post(
        "/login", data={"username": "admin", "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


def test_sms_page_requires_login(client: TestClient) -> None:
    resp = client.get("/sms", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_sms_page_shows_records_with_masked_phone(client: TestClient) -> None:
    login(client)
    with SessionLocal() as session:
        session.add(
            SmsNotification(
                phone="13812345678", content="协议到期提醒内容" * 10, status="sent"
            )
        )
        session.add(
            SmsNotification(
                phone="13900000000",
                content="第二条短信",
                status="failed",
                error_message="短信中心拒绝发送（+CMS ERROR: 331 无网络服务）",
            )
        )
        session.add(
            SmsNotification(phone="13711110000", content="第三条", status="pending")
        )
        session.commit()

    page = client.get("/sms")
    assert page.status_code == 200
    assert "短信通知记录" in page.text
    # 手机号脱敏：138****5678，原文不出现。
    assert "138****5678" in page.text
    assert "13812345678" not in page.text
    assert "139****0000" in page.text
    assert "137****0000" in page.text
    assert "已发送" in page.text
    assert "失败" in page.text
    assert "待发送" in page.text
    assert "无网络服务" in page.text


def test_sms_page_shows_only_recent_200(client: TestClient) -> None:
    login(client)
    with SessionLocal() as session:
        session.add(SmsNotification(phone="13800000000", content="OLDEST", status="sent"))
        for _ in range(204):
            session.add(
                SmsNotification(phone="13800000000", content="middle", status="sent")
            )
        session.add(SmsNotification(phone="13800000000", content="NEWEST", status="sent"))
        session.commit()

    page = client.get("/sms")
    assert page.status_code == 200
    assert "NEWEST" in page.text
    assert "OLDEST" not in page.text
