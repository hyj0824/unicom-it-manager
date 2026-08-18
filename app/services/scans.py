from __future__ import annotations

"""运营支撑助手：每日扫描生成通知任务。

按 `ScanSchedule` 配置扫描台账，把到期维系、设备回收、审核卡单等待办汇总成
`CallTask` 通知任务（通知对象是运维工作人员，不是客户本人）：

- `due_renewal`（协议到期维系）：协议到期前 N 天（lead_days）提醒客户经理续签；
- `device_recycle`（退网设备回收）：退网业务下未回收的设备，提醒网络维护责任人回收；
- `review_stuck`（审核卡单提醒）：全部 submitted 待审核的变更申请，提醒审核人员处理。

调度器只负责到点调用 `run_scan_for_schedule`；本模块负责查询、话术渲染、
去重与任务入队，事务提交由调用方负责。扫描过程不访问串口/声卡，与真实
硬件无关（AGENTS.md 硬件安全）。
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    BusinessService,
    CallTask,
    ChangeItem,
    ChangeSet,
    Contact,
    CustomerContact,
    NetworkDevice,
    Permission,
    RolePermission,
    ScanSchedule,
    Script,
    SmsNotification,
    User,
    UserRole,
    utcnow,
)
from . import scripts as script_service

logger = logging.getLogger(__name__)

# 话术占位符约定：{{客户名称}} {{业务号码}} {{协议到期日}} {{负责人姓名}}
# {{设备编码}} {{审核单标题}} {{扫描类型}}。缺失占位符渲染后保留原样，便于发现模板缺键。
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

# 内置默认话术：schedule.script 为空时按 scan_type 使用。
DEFAULT_TEMPLATES: dict[str, str] = {
    "due_renewal": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务：客户{{客户名称}}的"
        "{{业务号码}}业务协议将于{{协议到期日}}到期，请{{负责人姓名}}提前联系客户办理"
        "续签维系，感谢您的配合。"
    ),
    "device_recycle": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务：客户{{客户名称}}的"
        "{{业务号码}}业务已退网，其中设备{{设备编码}}尚未回收，请{{负责人姓名}}尽快"
        "安排回收，感谢您的配合。"
    ),
    "review_stuck": (
        "您好，这里是XX运维支撑中心，通知您处理{{扫描类型}}任务：客户{{客户名称}}的"
        "{{业务号码}}业务提交的审核单「{{审核单标题}}」已长时间未审核，请{{负责人姓名}}"
        "尽快登录系统审核处理，感谢您的配合。"
    ),
}

SCAN_TYPE_LABELS: dict[str, str] = {
    "due_renewal": "协议到期维系",
    "device_recycle": "退网设备回收",
    "review_stuck": "审核卡单提醒",
}

# 通知对象职责（customer_contacts.duty）。
DUTY_ACCOUNT_MANAGER = "客户经理"
DUTY_NETWORK_MAINTENANCE = "网络维护责任人"

# 扫描任务来源（CallTask.source），与 TODO 任务来源约定一致。
SOURCE_DUE_RENEWAL = "due_renewal"
SOURCE_DEVICE_RECYCLE = "device_recycle"
SOURCE_REVIEW_STUCK = "review_stuck"


# ---------------------------------------------------------------------------
# 话术模板渲染
# ---------------------------------------------------------------------------


def render_script_template(template: str, ctx: dict[str, str]) -> str:
    """把模板中的 ``{{key}}`` 替换为 ``ctx[key]``；缺失占位符保留原样。

    key 两端的空白会被忽略（``{{ 客户名称 }}`` 与 ``{{客户名称}}`` 等价）。
    """

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return ctx.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, template)


def _template_for(schedule: ScanSchedule) -> str:
    """取扫描话术模板：优先 schedule.script 正文，为空时用内置默认模板。"""

    if schedule.script_id is not None and schedule.script is not None:
        body = (schedule.script.body or "").strip()
        if body:
            return schedule.script.body
        logger.warning("扫描 #%s：话术模板正文为空，回退内置默认模板", schedule.id)
    return DEFAULT_TEMPLATES.get(schedule.scan_type, "")


# ---------------------------------------------------------------------------
# 时间工具：数据库时间按 UTC 处理，当天零点按时区计算（与 plans 语义一致）
# ---------------------------------------------------------------------------


def _get_zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_utc(value: datetime) -> datetime:
    """naive 视为 UTC（台账导入与 SQLite 存储都是 naive UTC）。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local_date_utc(now: datetime, timezone_name: str) -> datetime:
    """``now`` 所在时区的「当天零点」对应的 UTC 时刻。"""

    zone = _get_zone(timezone_name)
    local_now = _as_utc(now).astimezone(zone)
    return datetime.combine(local_now.date(), datetime.min.time(), tzinfo=zone).astimezone(
        timezone.utc
    )


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _contact_by_duty(db: Session, customer_id: int, duty: str) -> Contact | None:
    """取客户启用的指定职责联系人。

    多个启用联系人时优先取有电话号码的（电话是通知通道本身）；
    全部无电话时取最早创建的一条（由调用方决定是否跳过）。
    """

    links = db.scalars(
        select(CustomerContact)
        .where(
            CustomerContact.customer_id == customer_id,
            CustomerContact.duty == duty,
            CustomerContact.is_active.is_(True),
        )
        .order_by(CustomerContact.id.asc())
    ).all()
    if not links:
        return None
    for link in links:
        if (link.contact.phone or "").strip():
            return link.contact
    return links[0].contact


def _existing_target_ids(
    db: Session, source: str, day_start_utc: datetime, meta_key: str
) -> set[int]:
    """当天已入队扫描任务的目标 id 集合（解析 meta_json），用于幂等去重。

    「当天」按扫描时区的日界对应的 UTC 区间判断 created_at；目标 id 从
    ``meta_json`` 中按 ``meta_key``（business_service_id / device_id）解析，
    解析失败的旧数据忽略（不影响本次去重）。
    """

    tasks = db.scalars(
        select(CallTask).where(
            CallTask.source == source,
            CallTask.created_at >= day_start_utc,
            CallTask.created_at < day_start_utc + timedelta(days=1),
        )
    ).all()
    target_ids: set[int] = set()
    for task in tasks:
        try:
            meta = json.loads(task.meta_json or "{}")
        except ValueError:
            continue
        value = meta.get(meta_key)
        if value is not None:
            target_ids.add(int(value))
    return target_ids


def _make_scan_script(db: Session, schedule: ScanSchedule, body: str, scan_date: str) -> Script:
    """为本次扫描新建话术并复用话术服务的音频生成。

    TTS_PROVIDER=none 时跳过生成，保持 tts_status=not_generated，不落失败
    状态也不阻塞入队（音频可由话术页按需补生成）；其他 Provider 正常生成。
    """

    script = Script(title=f"[扫描]{schedule.name} {scan_date}", body=body)
    db.add(script)
    db.flush()
    settings = get_settings()
    if settings.tts_provider.strip().lower() != "none":
        script_service.generate_script_audio(db, script, settings)
    return script


def _base_task_meta(
    schedule: ScanSchedule,
    service: BusinessService,
    body: str,
    scan_date: str,
    **extra: object,
) -> dict:
    meta: dict = {
        "business_service_id": service.id,
        "due_date": scan_date,
        "rendered_script": body,
        "scan_schedule_id": schedule.id,
    }
    meta.update(extra)
    return meta


def _maybe_enqueue_sms(
    db: Session, settings, schedule: ScanSchedule, task: CallTask, phone: str, body: str
) -> None:
    """扫描配置与全局配置都开启短信时，把通知任务同步入队为待发短信。

    与任务同一事务：创建失败则整个扫描失败回滚（正常，调用方吞掉并写
    last_error）。content 复用渲染后的话术正文，电话即任务拨号号码。
    """
    if schedule.sms_enabled and settings.sms_enabled:
        db.add(
            SmsNotification(
                call_task_id=task.id,
                phone=phone,
                content=body,
                status="pending",
            )
        )


# ---------------------------------------------------------------------------
# 协议到期维系扫描
# ---------------------------------------------------------------------------


def run_due_renewal_scan(
    db: Session, schedule: ScanSchedule, now: datetime | None = None
) -> int:
    """扫描到期维系：为窗口内的业务生成客户经理通知任务，返回生成任务数。

    窗口为 [当天零点, 当天零点+lead_days 天]（闭区间：到期日恰为「提前
    lead_days 天」的业务也纳入提醒，用 +1 天的开区间实现右端点），按
    schedule.timezone 计算当天零点，UTC 比较。
    """

    now_utc = _as_utc(now) if now is not None else utcnow()
    zone = _get_zone(schedule.timezone)
    day_start_utc = _local_date_utc(now_utc, schedule.timezone)
    window_end_utc = day_start_utc + timedelta(days=schedule.lead_days + 1)
    scan_date = now_utc.astimezone(zone).strftime("%Y-%m-%d")

    template = _template_for(schedule)
    existing = _existing_target_ids(db, SOURCE_DUE_RENEWAL, day_start_utc, "business_service_id")
    settings = get_settings()

    services = db.scalars(
        select(BusinessService)
        .where(
            BusinessService.is_active.is_(True),
            BusinessService.agreement_expires_at.is_not(None),
        )
        .order_by(BusinessService.id.asc())
    ).all()

    created = 0
    for service in services:
        expires_utc = _as_utc(service.agreement_expires_at)
        if not (day_start_utc <= expires_utc < window_end_utc):
            continue
        if service.id in existing:
            logger.info(
                "扫描 #%s：业务 %s 当天已有到期维系任务，跳过",
                schedule.id,
                service.service_number,
            )
            continue
        contact = _contact_by_duty(db, service.customer_id, DUTY_ACCOUNT_MANAGER)
        phone = (contact.phone or "").strip() if contact else ""
        if contact is None or not phone:
            logger.info(
                "扫描 #%s：业务 %s 缺少%s或电话为空，跳过",
                schedule.id,
                service.service_number,
                DUTY_ACCOUNT_MANAGER,
            )
            continue
        due_date = expires_utc.astimezone(zone).strftime("%Y-%m-%d")
        ctx = {
            "客户名称": service.customer.name if service.customer else "",
            "业务号码": service.service_number,
            "协议到期日": due_date,
            "负责人姓名": (contact.name or "").strip(),
            "扫描类型": SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
        }
        body = render_script_template(template, ctx)
        script = _make_scan_script(db, schedule, body, scan_date)
        task = CallTask(
            plan=None,
            scan_schedule=schedule,
            customer_id=service.customer_id,
            script=script,
            contact=contact,
            dial_number=phone,
            due_at=utcnow(),
            status="queued",
            source=SOURCE_DUE_RENEWAL,
            max_attempts=settings.max_call_attempts,
            meta_json=json.dumps(
                _base_task_meta(
                    schedule,
                    service,
                    body,
                    due_date,
                ),
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.flush()  # 拿到任务 id 供短信通知关联。
        _maybe_enqueue_sms(db, settings, schedule, task, phone, body)
        created += 1
    db.flush()
    logger.info(
        "扫描 #%s「%s」：%s 生成 %d 条通知任务",
        schedule.id,
        schedule.name,
        SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
        created,
    )
    return created


# ---------------------------------------------------------------------------
# 退网设备回收扫描
# ---------------------------------------------------------------------------


def _is_retired_service(service: BusinessService, day_start_utc: datetime) -> bool:
    """退网业务判定，两种口径都查：

    1. 字典项口径：service_status_item 的 label 含「退网」。种子枚举为
       「正常开机 / 主动退网(申请拆机)」，用包含匹配而不是 ==「退网」，
       保证种子标签与自定义“退网”标签都能命中；
    2. 时间口径：协议已过期（agreement_expires_at < 当天零点），到期不续
       视为退网。
    """

    status_label = service.service_status_item.label if service.service_status_item else ""
    if "退网" in status_label:
        return True
    if service.agreement_expires_at is not None:
        if _as_utc(service.agreement_expires_at) < day_start_utc:
            return True
    return False


def _device_recovered(device: NetworkDevice) -> bool:
    """回收完成判定：recovery_status_item 存在且标签表示已完成回收。

    种子字典是「已回收 / 未回收」——两者都含“回收”二字，不能简单子串
    匹配；以「未/否」开头的否定标签一律视为未回收，其余含「回收」或
    「完成」的标签（已回收、已完成回收、回收完成…）视为已回收。
    recovery_status_item 为空视为未回收（台账未填写回收状态）。
    """

    item = device.recovery_status_item
    if item is None:
        return False
    label = (item.label or "").strip()
    if label.startswith(("未", "否")):
        return False
    return "回收" in label or "完成" in label


def run_device_recycle_scan(
    db: Session, schedule: ScanSchedule, now: datetime | None = None
) -> int:
    """扫描退网设备回收：退网业务下每台未回收设备生成一条通知任务。

    业务退网判定见 `_is_retired_service`；设备是否已回收见 `_device_recovered`；
    通知对象为 customer_contacts 中职责「网络维护责任人」的启用联系人。
    """

    now_utc = _as_utc(now) if now is not None else utcnow()
    zone = _get_zone(schedule.timezone)
    day_start_utc = _local_date_utc(now_utc, schedule.timezone)
    scan_date = now_utc.astimezone(zone).strftime("%Y-%m-%d")

    template = _template_for(schedule)
    existing = _existing_target_ids(db, SOURCE_DEVICE_RECYCLE, day_start_utc, "device_id")
    settings = get_settings()

    services = db.scalars(
        select(BusinessService)
        .where(BusinessService.is_active.is_(True))
        .order_by(BusinessService.id.asc())
    ).all()

    created = 0
    for service in services:
        if not _is_retired_service(service, day_start_utc):
            continue
        contact = _contact_by_duty(db, service.customer_id, DUTY_NETWORK_MAINTENANCE)
        phone = (contact.phone or "").strip() if contact else ""
        if contact is None or not phone:
            logger.info(
                "扫描 #%s：退网业务 %s 缺少%s或电话为空，跳过",
                schedule.id,
                service.service_number,
                DUTY_NETWORK_MAINTENANCE,
            )
            continue
        for device in service.devices:
            if not device.is_active:
                logger.info(
                    "扫描 #%s：设备 %s 已停用，跳过",
                    schedule.id,
                    device.device_code,
                )
                continue
            if _device_recovered(device):
                continue
            if device.id in existing:
                logger.info(
                    "扫描 #%s：设备 %s 当天已有回收任务，跳过",
                    schedule.id,
                    device.device_code,
                )
                continue
            ctx = {
                "客户名称": service.customer.name if service.customer else "",
                "业务号码": service.service_number,
                "协议到期日": (
                    _as_utc(service.agreement_expires_at).astimezone(zone).strftime("%Y-%m-%d")
                    if service.agreement_expires_at is not None
                    else ""
                ),
                "负责人姓名": (contact.name or "").strip(),
                "设备编码": device.device_code,
                "扫描类型": SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
            }
            body = render_script_template(template, ctx)
            script = _make_scan_script(db, schedule, body, scan_date)
            task = CallTask(
                plan=None,
                scan_schedule=schedule,
                customer_id=service.customer_id,
                script=script,
                contact=contact,
                dial_number=phone,
                due_at=utcnow(),
                status="queued",
                source=SOURCE_DEVICE_RECYCLE,
                max_attempts=settings.max_call_attempts,
                meta_json=json.dumps(
                    _base_task_meta(
                        schedule,
                        service,
                        body,
                        scan_date,
                        device_id=device.id,
                        device_code=device.device_code,
                    ),
                    ensure_ascii=False,
                ),
            )
            db.add(task)
            db.flush()  # 拿到任务 id 供短信通知关联。
            _maybe_enqueue_sms(db, settings, schedule, task, phone, body)
            created += 1
    db.flush()
    logger.info(
        "扫描 #%s「%s」：%s 生成 %d 条通知任务",
        schedule.id,
        schedule.name,
        SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
        created,
    )
    return created


# ---------------------------------------------------------------------------
# 审核卡单提醒扫描
# ---------------------------------------------------------------------------


def _reviewer_users(db: Session) -> list[User]:
    """审核人员：is_enabled 且绑定角色（user_roles→roles→role_permissions→
    permissions）拥有 code='review' 权限的用户，按 id 升序。

    权限/角色引用数据缺失时返回空列表（扫描自然跳过，不视为异常）。
    """

    permission_id = db.scalar(select(Permission.id).where(Permission.code == "review"))
    if permission_id is None:
        return []
    role_ids = db.scalars(
        select(RolePermission.role_id).where(RolePermission.permission_id == permission_id)
    ).all()
    if not role_ids:
        return []
    return db.scalars(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .where(User.is_enabled.is_(True), UserRole.role_id.in_(role_ids))
        .distinct()
        .order_by(User.id.asc())
    ).all()


def _change_set_business(db: Session, change_set: ChangeSet) -> BusinessService | None:
    """变更单关联的业务：取第一条 BusinessService 变更项对应的业务。

    审核卡单提醒话术需要客户名称/业务号码；创建类变更（entity_id 为空）或
    业务已不存在时无法定位客户，返回 None 由调用方跳过。
    """

    for item in change_set.items:
        if item.entity_type != "BusinessService" or item.entity_id is None:
            continue
        service = db.get(BusinessService, item.entity_id)
        if service is not None:
            return service
    return None


def run_review_stuck_scan(
    db: Session, schedule: ScanSchedule, now: datetime | None = None
) -> int:
    """扫描审核卡单：把全部 status=submitted 的变更申请扫出来提醒审核人员。

    不做“卡单时长阈值”配置：cron 到点即提醒全部待审核申请，同一天同一
    change_set 不重复通知（meta_json change_set_id 去重），审核通过/驳回
    后状态不再是 submitted，自然不再提醒。通知对象为绑定角色拥有 review
    权限的启用用户（user.phone 为空跳过）。
    """

    now_utc = _as_utc(now) if now is not None else utcnow()
    zone = _get_zone(schedule.timezone)
    day_start_utc = _local_date_utc(now_utc, schedule.timezone)
    scan_date = now_utc.astimezone(zone).strftime("%Y-%m-%d")

    template = _template_for(schedule)
    existing = _existing_target_ids(db, SOURCE_REVIEW_STUCK, day_start_utc, "change_set_id")
    reviewers = [u for u in _reviewer_users(db) if (u.phone or "").strip()]
    if not reviewers:
        logger.info(
            "扫描 #%s「%s」：没有可通知的审核人员（拥有 review 权限且有手机的启用用户），跳过",
            schedule.id,
            schedule.name,
        )
        return 0
    settings = get_settings()

    change_sets = db.scalars(
        select(ChangeSet)
        .where(ChangeSet.status == "submitted")
        .order_by(ChangeSet.id.asc())
    ).all()

    created = 0
    for change_set in change_sets:
        if change_set.id in existing:
            logger.info(
                "扫描 #%s：变更申请 %s 当天已有卡单提醒任务，跳过",
                schedule.id,
                change_set.id,
            )
            continue
        service = _change_set_business(db, change_set)
        if service is None:
            logger.info(
                "扫描 #%s：变更申请 %s（%s）无关联业务客户，跳过",
                schedule.id,
                change_set.id,
                change_set.title,
            )
            continue
        for user in reviewers:
            ctx = {
                "客户名称": service.customer.name if service.customer else "",
                "业务号码": service.service_number,
                "审核单标题": change_set.title,
                "负责人姓名": (user.real_name or "").strip() or user.username,
                "扫描类型": SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
            }
            body = render_script_template(template, ctx)
            script = _make_scan_script(db, schedule, body, scan_date)
            task = CallTask(
                plan=None,
                scan_schedule=schedule,
                customer_id=service.customer_id,
                script=script,
                contact=None,
                dial_number=user.phone.strip(),
                due_at=utcnow(),
                status="queued",
                source=SOURCE_REVIEW_STUCK,
                max_attempts=settings.max_call_attempts,
                meta_json=json.dumps(
                    {
                        "change_set_id": change_set.id,
                        "rendered_script": body,
                    },
                    ensure_ascii=False,
                ),
            )
            db.add(task)
            db.flush()  # 拿到任务 id 供短信通知关联。
            _maybe_enqueue_sms(db, settings, schedule, task, user.phone.strip(), body)
            created += 1
    db.flush()
    logger.info(
        "扫描 #%s「%s」：%s 生成 %d 条通知任务",
        schedule.id,
        schedule.name,
        SCAN_TYPE_LABELS.get(schedule.scan_type, schedule.scan_type),
        created,
    )
    return created


# ---------------------------------------------------------------------------
# 统一入口：调度器契约 run_scan_for_schedule(db, schedule) -> int
# ---------------------------------------------------------------------------


_SCAN_RUNNERS: dict[str, Callable[..., int]] = {
    SOURCE_DUE_RENEWAL: run_due_renewal_scan,
    SOURCE_DEVICE_RECYCLE: run_device_recycle_scan,
    SOURCE_REVIEW_STUCK: run_review_stuck_scan,
}


def run_scan_for_schedule(
    db: Session, schedule: ScanSchedule, now: datetime | None = None
) -> int:
    """按计划执行一次扫描，返回生成的任务数。

    成功时置 ``last_run_at`` 并清空 ``last_error``；任何异常写
    ``last_error`` 后吞掉（不能让调度器崩）。事务提交由调用方负责；
    异常时先回滚扫描中途的半成品数据，再单独落错误原因。
    """

    runner = _SCAN_RUNNERS.get(schedule.scan_type)
    try:
        if runner is None:
            raise ValueError(f"未知扫描类型：{schedule.scan_type}")
        count = runner(db, schedule, now=now)
    except Exception as exc:  # noqa: BLE001 - 扫描异常必须落库吞掉，不能抛给调度器
        logger.exception(
            "扫描计划 #%s「%s」执行失败：%s", schedule.id, schedule.name, exc
        )
        # 丢弃扫描中途产生的半成品数据，保证下次重扫干净；错误原因直接写回
        # 传入的 schedule 实例（回滚后实例已过期，但属性赋值仍可正常持久化）。
        db.rollback()
        schedule.last_error = str(exc)
        return 0
    schedule.last_run_at = _as_utc(now) if now is not None else utcnow()
    schedule.last_error = ""
    return count
