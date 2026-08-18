from __future__ import annotations

"""变更申请服务（app/services/change_requests.py）与 /due-work 路由测试。

覆盖：
- 快照 patch 完整性（_apply_business 整体覆盖下其他字段保持不变）；
- 续签申请 → 审核通过应用 → agreement_expires_at 更新、version+1；
- 退网申请 → 应用后 service_status 为「主动退网(申请拆机)」；
- 回收申请 → 应用后 recovery_status 为「已回收」；
- 版本冲突：提交后再改业务 → 应用抛 ChangeApplicationError；
- 工作台列表：到期窗口、今日已通知、退网未回收设备判定；
- Web 路由：未登录 303、合法提交 303 且落库、非法日期 400。

全部使用临时 SQLite + alembic head，不接触 data/app.db，不访问真实硬件。
"""

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import (
    BusinessService,
    CallTask,
    ChangeItem,
    ChangeSet,
    Contact,
    Customer,
    CustomerContact,
    NetworkDevice,
    Script,
)
from app.services import change_requests
from app.services.change_requests import (
    RETIRE_STATUS_LABEL,
    business_snapshot_patch,
    list_due_renewal_rows,
    list_recycle_device_rows,
    submit_business_update,
    submit_device_recovery,
)
from app.services.dictionaries import resolve_or_create_item
from app.services.reviews import ChangeApplicationError, apply_change_set

BASE_DIR = Path(__file__).resolve().parent.parent
TZ = "Asia/Shanghai"
# 固定“当前时间”：2026-08-18 10:00（北京时间），避免依赖真实时钟。
NOW = datetime(2026, 8, 18, 2, 0, 0, tzinfo=timezone.utc)
ADMIN_PASSWORD = "test-admin-password"


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


def _utc_naive(value: datetime) -> datetime:
    """模拟 SQLite 存储：UTC 墙上时间、不带时区。"""

    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _make_customer(db: Session, name: str = "测试客户") -> Customer:
    customer = Customer(name=name)
    db.add(customer)
    db.flush()
    return customer


def _add_contact(
    db: Session, customer: Customer, name: str, phone: str, duty: str
) -> Contact:
    contact = Contact(name=name, phone=phone)
    db.add(contact)
    db.flush()
    db.add(CustomerContact(customer_id=customer.id, contact_id=contact.id, duty=duty))
    db.flush()
    return contact


def _make_service(
    db: Session,
    customer: Customer,
    number: str,
    *,
    expires_at: datetime | None = None,
    status_label: str = "正常开机",
    source_row: str = "导入批次 #1 第 3 行",
) -> BusinessService:
    service = BusinessService(
        service_number=number,
        customer_id=customer.id,
        county_item=resolve_or_create_item(db, "county", "汉滨"),
        grid_item=resolve_or_create_item(db, "grid", "汉滨要客"),
        service_status_item=resolve_or_create_item(db, "service_status", status_label),
        business_type_item=resolve_or_create_item(db, "business_type", "宽带业务"),
        data_quality_status_item=resolve_or_create_item(db, "data_quality_status", "完整"),
        accessed_at=_utc_naive(datetime(2022, 11, 3, 0, 0, tzinfo=ZoneInfo(TZ))),
        agreement_expires_at=_utc_naive(expires_at) if expires_at else None,
        channel_name="测试渠道",
        source_row=source_row,
    )
    db.add(service)
    db.flush()
    return service


def _make_device(
    db: Session,
    service: BusinessService,
    code: str,
    *,
    recovery_status_label: str | None = None,
) -> NetworkDevice:
    device = NetworkDevice(
        device_code=code,
        business_service_id=service.id,
        asset_class_item=resolve_or_create_item(db, "asset_class", "资产类"),
        asset_value=1200,
        device_type_item=resolve_or_create_item(db, "device_type", "光猫"),
        vendor_model="测试V1",
        location="机房",
        recovery_status_item=(
            resolve_or_create_item(db, "recovery_status", recovery_status_label)
            if recovery_status_label
            else None
        ),
        recovery_reason_item=resolve_or_create_item(db, "recovery_reason", "在用"),
    )
    db.add(device)
    db.flush()
    return device


def _approve_and_apply(db: Session, change_set: ChangeSet, user_id: int) -> int:
    change_set.status = "approved"
    db.flush()
    return apply_change_set(db, change_set, user_id)


# ---------------------------------------------------------------- 快照与申请应用


def test_business_snapshot_patch_roundtrips_through_apply(db: Session) -> None:
    """续签申请应用后，快照覆盖的其余字段必须与申请前完全一致。"""

    customer = _make_customer(db)
    _add_contact(db, customer, "张三", "13800000000", "发展人")
    _add_contact(db, customer, "李四", "13900000001", "客户经理")
    expires = datetime(2026, 8, 25, 0, 0, tzinfo=ZoneInfo(TZ))
    service = _make_service(db, customer, "848DIA100001", expires_at=expires)
    db.commit()

    patch = business_snapshot_patch(db, service)
    assert patch["service_number"] == "848DIA100001"
    assert patch["customer_name"] == "测试客户"
    assert patch["county"] == "汉滨" and patch["grid"] == "汉滨要客"
    assert patch["service_status"] == "正常开机" and patch["business_type"] == "宽带业务"
    assert patch["channel_name"] == "测试渠道"
    assert patch["accessed_at"] == "2022-11-03"
    assert patch["agreement_expires_at"] == "2026-08-25"
    assert patch["contacts"]["developer"] == {"name": "张三", "phone": "13800000000"}
    assert patch["contacts"]["account_manager"] == {"name": "李四", "phone": "13900000001"}

    before_version = service.version
    change_set = submit_business_update(
        db, service, {"agreement_expires_at": "2027-12-31"}, "客户确认续签", 11
    )
    assert change_set.status == "submitted" and change_set.domain == "business"
    item = change_set.items[0]
    assert item.entity_type == "BusinessService" and item.operation == "update"
    assert item.base_version == before_version
    db.commit()

    assert _approve_and_apply(db, change_set, 22) == 1
    db.commit()

    service = db.get(BusinessService, service.id)
    assert service.version == before_version + 1
    assert service.agreement_expires_at == _utc_naive(
        datetime(2027, 12, 31, 0, 0, tzinfo=ZoneInfo(TZ))
    )
    # 快照覆盖下其余字段保持不变（含来源行与数据质量）。
    assert service.customer.name == "测试客户"
    assert service.county_item.label == "汉滨" and service.grid_item.label == "汉滨要客"
    assert service.service_status_item.label == "正常开机"
    assert service.business_type_item.label == "宽带业务"
    assert service.channel_name == "测试渠道"
    assert service.source_row == "导入批次 #1 第 3 行"
    assert service.data_quality_status_item.label == "完整"
    assert service.accessed_at == _utc_naive(
        datetime(2022, 11, 3, 0, 0, tzinfo=ZoneInfo(TZ))
    )


def test_renew_validation_rejects_bad_dates(db: Session) -> None:
    customer = _make_customer(db)
    service = _make_service(db, customer, "848DIA100002", expires_at=None)
    db.commit()

    with pytest.raises(ValueError, match="不能为空"):
        submit_business_update(db, service, {"agreement_expires_at": ""}, "理由", 11)
    with pytest.raises(ValueError, match="格式不正确"):
        submit_business_update(db, service, {"agreement_expires_at": "not-a-date"}, "理由", 11)
    past = (datetime.now(ZoneInfo(TZ)) - timedelta(days=1)).date().isoformat()
    with pytest.raises(ValueError, match="必须晚于今天"):
        submit_business_update(db, service, {"agreement_expires_at": past}, "理由", 11)
    with pytest.raises(ValueError, match="理由不能为空"):
        submit_business_update(db, service, {"agreement_expires_at": "2099-12-31"}, "  ", 11)
    with pytest.raises(ValueError, match="不支持的变更字段"):
        submit_business_update(db, service, {"channel_name": "x"}, "理由", 11)
    db.rollback()


def test_retire_apply_sets_service_status(db: Session) -> None:
    customer = _make_customer(db)
    service = _make_service(db, customer, "848DIA100003", expires_at=None)
    db.commit()

    change_set = submit_business_update(
        db, service, {"service_status": RETIRE_STATUS_LABEL}, "客户确认不续签，申请拆机", 11
    )
    assert "退网" in change_set.title
    db.commit()
    assert _approve_and_apply(db, change_set, 22) == 1
    db.commit()

    service = db.get(BusinessService, service.id)
    assert service.service_status_item.label == "主动退网(申请拆机)"


def test_retire_apply_rejects_unknown_status_label(db: Session) -> None:
    customer = _make_customer(db)
    service = _make_service(db, customer, "848DIA100004", expires_at=None)
    db.commit()

    with pytest.raises(ValueError, match="不在服务状态字典中"):
        submit_business_update(db, service, {"service_status": "不存在的状态"}, "理由", 11)
    db.rollback()


def test_device_recovery_apply_sets_recovery_status(db: Session) -> None:
    customer = _make_customer(db)
    _add_contact(db, customer, "王五", "13700000002", "网络维护责任人")
    service = _make_service(
        db, customer, "848DIA100005",
        expires_at=datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo(TZ)),  # 已过期 → 退网
    )
    device = _make_device(db, service, "210000RC01", recovery_status_label=None)
    db.commit()

    change_set = submit_device_recovery(db, device, "设备已回收入库", 11)
    assert change_set.domain == "network" and change_set.status == "submitted"
    item = change_set.items[0]
    assert item.entity_type == "NetworkDevice" and item.operation == "update"
    assert item.base_version == device.version
    payload = json.loads(item.patch_json)
    assert payload["service_number"] == "848DIA100005"
    assert payload["device"]["recovery_status"] == "已回收"
    assert payload["device"]["recovery_reason"] == ""
    db.commit()

    before_version = device.version
    assert _approve_and_apply(db, change_set, 22) == 1
    db.commit()

    device = db.get(NetworkDevice, device.id)
    assert device.recovery_status_item.label == "已回收"
    assert device.recovery_reason_item is None
    assert device.version == before_version + 1
    # 其余设备字段不被回收申请破坏。
    assert device.device_code == "210000RC01"
    assert device.asset_class_item.label == "资产类"
    assert device.vendor_model == "测试V1" and device.location == "机房"
    assert device.business_service_id == service.id


def test_device_recovery_rejects_already_recovered(db: Session) -> None:
    customer = _make_customer(db)
    service = _make_service(
        db, customer, "848DIA100006",
        expires_at=datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo(TZ)),
    )
    device = _make_device(db, service, "210000RC02", recovery_status_label="已回收")
    db.commit()

    with pytest.raises(ValueError, match="已标记回收"):
        submit_device_recovery(db, device, "重复提交", 11)
    db.rollback()


def test_apply_rejects_stale_base_version(db: Session) -> None:
    """提交后业务被其他变更修改（version+1）→ 应用时抛版本冲突。"""

    customer = _make_customer(db)
    service = _make_service(db, customer, "848DIA100007", expires_at=None)
    db.commit()

    change_set = submit_business_update(
        db, service, {"agreement_expires_at": "2099-12-31"}, "客户确认续签", 11
    )
    db.commit()
    service.version += 1  # 模拟提交后其他编辑
    db.commit()

    change_set.status = "approved"
    db.flush()
    with pytest.raises(ChangeApplicationError, match="已被其他变更修改"):
        apply_change_set(db, change_set, 22)
    db.rollback()
    assert db.get(BusinessService, service.id).agreement_expires_at is None


# ---------------------------------------------------------------- 工作台列表


def test_due_renewal_rows_window_and_notified(db: Session) -> None:
    customer = _make_customer(db)
    in_window = _make_service(
        db, customer, "848DIA200001",
        expires_at=datetime(2026, 8, 25, 0, 0, tzinfo=ZoneInfo(TZ)),
    )
    boundary = _make_service(
        db, customer, "848DIA200002",
        expires_at=datetime(2026, 8, 18, 0, 0, tzinfo=ZoneInfo(TZ)),  # 今天当天
    )
    outside = _make_service(
        db, customer, "848DIA200003",
        expires_at=datetime(2026, 9, 20, 0, 0, tzinfo=ZoneInfo(TZ)),
    )
    no_expiry = _make_service(db, customer, "848DIA200004", expires_at=None)
    script = Script(title="扫描话术", body="通知")
    db.add(script)
    db.flush()
    db.add(
        CallTask(
            customer_id=customer.id,
            script_id=script.id,
            due_at=NOW,
            created_at=NOW,  # 固定“当天”窗内，避免依赖真实时钟
            source="due_renewal",
            meta_json=json.dumps({"business_service_id": in_window.id}),
        )
    )
    db.commit()

    rows = list_due_renewal_rows(db, now=NOW)
    by_number = {row["service"].service_number: row for row in rows}
    assert set(by_number) == {"848DIA200001", "848DIA200002"}
    assert by_number["848DIA200001"]["expires_label"] == "2026-08-25"
    assert by_number["848DIA200001"]["notified_today"] is True
    assert by_number["848DIA200002"]["notified_today"] is False
    assert by_number["848DIA200002"]["expires_label"] == "2026-08-18"
    assert no_expiry.id not in {row["service"].id for row in rows}


def test_latest_change_status_reported_on_rows(db: Session) -> None:
    customer = _make_customer(db)
    service = _make_service(
        db, customer, "848DIA200005",
        expires_at=datetime(2026, 8, 25, 0, 0, tzinfo=ZoneInfo(TZ)),
    )
    change_set = submit_business_update(
        db, service, {"agreement_expires_at": "2099-12-31"}, "客户确认续签", 11
    )
    db.commit()

    rows = list_due_renewal_rows(db, now=NOW)
    row = next(r for r in rows if r["service"].id == service.id)
    assert row["latest_change"] == ("submitted", change_set.id)


def test_recycle_rows_follow_retired_and_recovery_rules(db: Session) -> None:
    customer = _make_customer(db)
    _add_contact(db, customer, "王五", "13700000002", "网络维护责任人")
    # 字典口径退网（主动退网申请拆机）。
    retired = _make_service(db, customer, "848DIA300001", status_label="主动退网(申请拆机)")
    # 时间口径退网（协议已过期）。
    expired = _make_service(
        db, customer, "848DIA300002",
        expires_at=datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo(TZ)),
    )
    # 正常业务：不应进入回收列表。
    normal = _make_service(
        db, customer, "848DIA300003",
        expires_at=datetime(2026, 12, 31, 0, 0, tzinfo=ZoneInfo(TZ)),
    )
    pending = _make_device(db, retired, "210000RC10", recovery_status_label=None)
    unrecovered = _make_device(db, expired, "210000RC11", recovery_status_label="未回收")
    recovered = _make_device(db, expired, "210000RC12", recovery_status_label="已回收")
    _make_device(db, normal, "210000RC13", recovery_status_label=None)
    db.commit()

    rows = list_recycle_device_rows(db, now=NOW)
    codes = {row["device"].device_code for row in rows}
    assert codes == {"210000RC10", "210000RC11"}
    assert recovered.device_code not in codes
    row = next(r for r in rows if r["device"].id == pending.id)
    assert row["service"].service_number == "848DIA300001"
    assert row["maintenance_name"] == "王五"


# ---------------------------------------------------------------- Web 路由


@pytest.fixture()
def webdb(tmp_path: Path):
    db_path = tmp_path / "web-change-requests.db"
    url = f"sqlite:///{db_path}"
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield factory
    engine.dispose()


@pytest.fixture()
def web_client(webdb, monkeypatch):
    def override_get_db():
        db = webdb()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, webdb
    app.dependency_overrides.pop(get_db, None)


def _login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _web_service(webdb, number: str, *, days_from_today: int) -> int:
    """通过 webdb 会话直接造一条到期窗口内的业务，返回业务 id。"""

    with webdb() as db:
        customer = _make_customer(db, f"Web客户-{number}")
        _add_contact(db, customer, "李四", "13900000001", "客户经理")
        expires = datetime.now(ZoneInfo(TZ)) + timedelta(days=days_from_today)
        expires = datetime.combine(expires.date(), time.min, tzinfo=ZoneInfo(TZ))
        service = _make_service(db, customer, number, expires_at=expires)
        db.commit()
        return service.id


def test_due_work_requires_login_and_lists_window(web_client) -> None:
    client, webdb = web_client
    response = client.get("/due-work", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    service_id = _web_service(webdb, "848DIAWEB1001", days_from_today=5)
    _web_service(webdb, "848DIAWEB1002", days_from_today=100)

    _login(client)
    response = client.get("/due-work")
    assert response.status_code == 200
    assert "848DIAWEB1001" in response.text
    assert "848DIAWEB1002" not in response.text
    assert "提前 14 天" in response.text  # 无扫描配置时使用默认提前天数
    assert service_id


def test_renew_submit_requires_login_and_creates_change_set(web_client) -> None:
    client, webdb = web_client
    service_id = _web_service(webdb, "848DIAWEB2001", days_from_today=5)

    unauth = client.post(
        f"/due-work/business/{service_id}/renew",
        data={"agreement_expires_at": "2099-12-31", "reason": "续签"},
        follow_redirects=False,
    )
    assert unauth.status_code == 303
    assert unauth.headers["location"] == "/login"

    _login(client)
    with webdb() as db:
        version = db.get(BusinessService, service_id).version

    response = client.post(
        f"/due-work/business/{service_id}/renew",
        data={"agreement_expires_at": "2099-12-31", "reason": "客户确认续签一年"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with webdb() as db:
        change_set = db.scalars(
            select(ChangeSet).order_by(ChangeSet.id.desc()).limit(1)
        ).one()
        assert change_set.domain == "business"
        assert change_set.status == "submitted"
        assert change_set.reason == "客户确认续签一年"
        item = db.scalars(
            select(ChangeItem).where(ChangeItem.change_set_id == change_set.id)
        ).one()
        assert item.entity_type == "BusinessService"
        assert item.entity_id == service_id
        assert item.operation == "update"
        assert item.base_version == version
        patch = json.loads(item.patch_json)
        assert patch["agreement_expires_at"] == "2099-12-31"
        assert patch["service_status"] == "正常开机"


def test_renew_invalid_date_returns_400(web_client) -> None:
    client, webdb = web_client
    service_id = _web_service(webdb, "848DIAWEB2002", days_from_today=5)
    _login(client)

    past = (datetime.now(ZoneInfo(TZ)) - timedelta(days=1)).date().isoformat()
    for bad_date, message in (("not-a-date", "格式不正确"), (past, "必须晚于今天")):
        response = client.post(
            f"/due-work/business/{service_id}/renew",
            data={"agreement_expires_at": bad_date, "reason": "续签"},
            follow_redirects=False,
        )
        assert response.status_code == 400, response.text
        assert message in response.text

    with webdb() as db:
        assert db.scalars(select(ChangeSet)).all() == []


def test_retire_submit_requires_reason_and_creates_change_set(web_client) -> None:
    client, webdb = web_client
    service_id = _web_service(webdb, "848DIAWEB2003", days_from_today=5)
    _login(client)

    missing_reason = client.post(
        f"/due-work/business/{service_id}/retire",
        data={"reason": "  "},
        follow_redirects=False,
    )
    assert missing_reason.status_code == 400
    assert "理由不能为空" in missing_reason.text

    response = client.post(
        f"/due-work/business/{service_id}/retire",
        data={"reason": "客户确认不续签，申请拆机"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with webdb() as db:
        change_set = db.scalars(
            select(ChangeSet).order_by(ChangeSet.id.desc()).limit(1)
        ).one()
        assert change_set.status == "submitted"
        assert "退网" in change_set.title
        patch = json.loads(change_set.items[0].patch_json)
        assert patch["service_status"] == "主动退网(申请拆机)"


def test_recover_submit_creates_network_change_set(web_client) -> None:
    client, webdb = web_client
    with webdb() as db:
        customer = _make_customer(db, "Web客户-848DIAWEB3001")
        _add_contact(db, customer, "王五", "13700000002", "网络维护责任人")
        expired = datetime.combine(
            (datetime.now(ZoneInfo(TZ)) - timedelta(days=30)).date(),
            time.min,
            tzinfo=ZoneInfo(TZ),
        )
        service = _make_service(db, customer, "848DIAWEB3001", expires_at=expired)
        device = _make_device(db, service, "210000WEB01", recovery_status_label=None)
        db.commit()
        device_id = device.id

    _login(client)
    response = client.post(
        f"/due-work/device/{device_id}/recover",
        data={"reason": "设备已回收入库"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with webdb() as db:
        change_set = db.scalars(
            select(ChangeSet).order_by(ChangeSet.id.desc()).limit(1)
        ).one()
        assert change_set.domain == "network"
        assert change_set.status == "submitted"
        item = change_set.items[0]
        assert item.entity_type == "NetworkDevice"
        assert item.entity_id == device_id
        assert item.base_version == db.get(NetworkDevice, device_id).version
        patch = json.loads(item.patch_json)
        assert patch["service_number"] == "848DIAWEB3001"
        assert patch["device"]["recovery_status"] == "已回收"
