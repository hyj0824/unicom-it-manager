from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


CALL_STATUSES = {
    "queued",
    "dialing",
    "connected",
    "no_answer",
    "rejected",
    "cancelled_or_failed",
    "busy",
    "short_call",
    "failed",
    "completed",
    "missed",
}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Script(TimestampMixin, Base):
    __tablename__ = "scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 系统通知话术角色；普通/历史话术可为空，系统话术角色唯一。
    role: Mapped[str | None] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tts_status: Mapped[str] = mapped_column(String(32), default="not_generated", nullable=False)
    # 音频生成失败原因（成功或未生成时为空），供话术页展示失败状态详情。
    tts_error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    wav_path: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    call_tasks: Mapped[list["CallTask"]] = relationship(back_populates="script")
    call_records: Mapped[list["CallRecord"]] = relationship(back_populates="script")


class ScanSchedule(TimestampMixin, Base):
    """系统级扫描通知配置：取代手动回访计划。

    调度器按 `cron_expr`（每日时段或每周几）触发对应扫描，把到期维系、
    设备回收等待办汇总成通知任务（CallTask），通知对象是运维工作人员。
    """

    __tablename__ = "scan_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # due_renewal=协议到期维系；device_recycle=退网设备回收；review_stuck=审核卡单。
    scan_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True, unique=True)
    cron_expr: Mapped[str] = mapped_column(
        String(120), default="0 9 * * *", nullable=False
    )
    timezone: Mapped[str] = mapped_column(
        String(80), default="Asia/Shanghai", nullable=False
    )
    # 提前天数：到期前 N 天进入通知范围。
    lead_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 到期维系/设备回收/审核卡单提醒生成任务时是否同步入队短信通知。
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    tasks: Mapped[list["CallTask"]] = relationship(back_populates="scan_schedule")


class CallTask(TimestampMixin, Base):
    __tablename__ = "call_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_schedule_id: Mapped[int | None] = mapped_column(ForeignKey("scan_schedules.id"))
    customer_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    # 入队时从负责人快照的拨号号码；号码变更不影响已入队任务。
    dial_number: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # 扫描任务快照：business_service_id / device_id / scan_schedule_id /
    # rendered_script / due_date 等，供去重、追踪与话术回放。
    meta_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    scan_schedule: Mapped[ScanSchedule | None] = relationship(back_populates="tasks")
    script: Mapped[Script] = relationship(back_populates="call_tasks")
    call_record: Mapped["CallRecord | None"] = relationship(back_populates="task")


class CallRecord(TimestampMixin, Base):
    __tablename__ = "call_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("call_tasks.id"), unique=True)
    customer_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    dial_number: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    dialing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    operator_feedback: Mapped[str] = mapped_column(Text, default="", nullable=False)

    task: Mapped[CallTask | None] = relationship(back_populates="call_record")
    script: Mapped[Script] = relationship(back_populates="call_records")
    events: Mapped[list["CallEvent"]] = relationship(
        back_populates="call_record", cascade="all, delete-orphan"
    )


class CallEvent(Base):
    __tablename__ = "call_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_record_id: Mapped[int] = mapped_column(ForeignKey("call_records.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    raw_line: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    def __init__(self, **kwargs):
        # 事件在创建（读到串口行）时盖章，而不是 flush 时：Worker 批量提交
        # 会把同批事件盖上同一个时间戳，掩盖真实到达顺序。
        kwargs.setdefault("created_at", utcnow())
        super().__init__(**kwargs)

    call_record: Mapped[CallRecord] = relationship(back_populates="events")


class SmsNotification(Base):
    """短信通知记录：扫描生成待发项，CallWorker 空闲时经 A7670E 串口发送。

    单通道约束：短信与语音共用同一个串口，发送由 Worker 串行处理，不与
    拨号并发；status: pending / sent / failed。
    """

    __tablename__ = "sms_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("call_tasks.id"), index=True
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    call_task: Mapped[CallTask | None] = relationship()


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# 运营商运维台账领域模型（见 docs/data-model-baseline.md、permission-workflow-baseline.md）
# ---------------------------------------------------------------------------

CHANGE_SET_STATUSES = {
    "draft",
    "submitted",
    "returned",
    "approved",
    "rejected",
    "applied",
    "cancelled",
}

CHANGE_ITEM_OPERATIONS = {"create", "update", "retire"}

IMPORT_BATCH_STATUSES = {
    "uploaded",
    "validating",
    "ready",
    "reviewing",
    "applied",
    "rejected",
}

DATA_DOMAINS = {"business", "network", "callback", "template", "system"}


class DictionaryCategory(TimestampMixin, Base):
    __tablename__ = "dictionary_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    items: Mapped[list["DictionaryItem"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class DictionaryItem(TimestampMixin, Base):
    __tablename__ = "dictionary_items"
    __table_args__ = (
        UniqueConstraint("category_id", "code", name="uq_dictionary_items_category_code"),
        UniqueConstraint("category_id", "label", name="uq_dictionary_items_category_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("dictionary_categories.id"), nullable=False, index=True
    )
    # 稳定引用码；纯中文枚举项可留空，以 label 为准。
    code: Mapped[str | None] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    category: Mapped[DictionaryCategory] = relationship(back_populates="items")


class BusinessService(TimestampMixin, Base):
    """业务实例：台账 A-M 列的规范化主体，业务号码为业务唯一键。"""

    __tablename__ = "business_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    developer_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    developer_phone: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    account_manager_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    account_manager_phone: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    county_item_id: Mapped[int | None] = mapped_column(ForeignKey("dictionary_items.id"))
    grid_item_id: Mapped[int | None] = mapped_column(ForeignKey("dictionary_items.id"))
    service_status_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("dictionary_items.id")
    )
    business_type_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("dictionary_items.id")
    )
    data_quality_status_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("dictionary_items.id")
    )
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agreement_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    source_row: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    devices: Mapped[list["NetworkDevice"]] = relationship(
        back_populates="business_service", cascade="all, delete-orphan"
    )
    county_item: Mapped["DictionaryItem | None"] = relationship(foreign_keys=[county_item_id])
    grid_item: Mapped["DictionaryItem | None"] = relationship(foreign_keys=[grid_item_id])
    service_status_item: Mapped["DictionaryItem | None"] = relationship(
        foreign_keys=[service_status_item_id]
    )
    business_type_item: Mapped["DictionaryItem | None"] = relationship(
        foreign_keys=[business_type_item_id]
    )
    data_quality_status_item: Mapped["DictionaryItem | None"] = relationship(
        foreign_keys=[data_quality_status_item_id]
    )


class NetworkDevice(TimestampMixin, Base):
    """网络设备：台账 N-W 列；设备编码全局唯一，与业务实例为一对多关系。"""

    __tablename__ = "network_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_service_id: Mapped[int] = mapped_column(
        ForeignKey("business_services.id"), nullable=False, index=True
    )
    device_code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    asset_class_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("dictionary_items.id")
    )
    asset_value: Mapped[float | None] = mapped_column(Numeric(14, 2))
    device_type_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("dictionary_items.id")
    )
    vendor_model: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    location: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    recovery_status_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("dictionary_items.id")
    )
    recovery_reason_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("dictionary_items.id")
    )
    maintenance_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    maintenance_phone: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    business_service: Mapped[BusinessService] = relationship(back_populates="devices")
    asset_class_item: Mapped["DictionaryItem | None"] = relationship(
        foreign_keys=[asset_class_item_id]
    )
    device_type_item: Mapped["DictionaryItem | None"] = relationship(
        foreign_keys=[device_type_item_id]
    )
    recovery_status_item: Mapped["DictionaryItem | None"] = relationship(
        foreign_keys=[recovery_status_item_id]
    )
    recovery_reason_item: Mapped["DictionaryItem | None"] = relationship(
        foreign_keys=[recovery_reason_item_id]
    )


class ChangeSet(TimestampMixin, Base):
    """一次人工提交或一次导入批次形成的变更申请。"""

    __tablename__ = "change_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    domain: Mapped[str] = mapped_column(String(32), default="business", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="draft", nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", name="fk_change_sets_import_batch_id"), index=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    applied_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["ChangeItem"]] = relationship(
        back_populates="change_set", cascade="all, delete-orphan"
    )


class ChangeItem(Base):
    """变更项：新增/修改/作废操作及字段级 patch，base_version 用于审核时检测冲突。"""

    __tablename__ = "change_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    change_set_id: Mapped[int] = mapped_column(
        ForeignKey("change_sets.id"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    base_version: Mapped[int | None] = mapped_column(Integer)
    patch_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    change_set: Mapped[ChangeSet] = relationship(back_populates="items")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ip_address: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class ImportBatch(TimestampMixin, Base):
    """一次 Excel 导入批次；原始行保留在 staging_rows，正式应用必须经过审核。"""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_name: Mapped[str] = mapped_column(String(320), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(64), default="business_ledger", nullable=False
    )
    header_mapping_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="uploaded", nullable=False, index=True
    )
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    missing_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    applied_change_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("change_sets.id")
    )

    rows: Mapped[list["StagingRow"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class StagingRow(Base):
    """暂存行：保留原始行号、原始值和校验结果，不直接进入正式表。"""

    __tablename__ = "staging_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_staging_rows_batch_row"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id"), nullable=False, index=True
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="validating", nullable=False, index=True
    )
    error_messages: Mapped[str] = mapped_column(Text, default="", nullable=False)
    mapped_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    result_entity_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    result_entity_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")


class User(TimestampMixin, Base):
    """用户账号：由具备 system/manage_users 的账号创建并按角色授权。

    `real_name` / `phone` 用于按步骤通知对应负责人；系统管理员可不设手机。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    real_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 兼容已有数据库列；认证层不再使用它授予权限，唯一全权限主体为内置 admin。
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 台账导入应用时按职责自动创建的账号；管理员可重置密码。
    auto_provisioned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 首次登录必须修改初始随机密码。
    force_password_change: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Role(TimestampMixin, Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_preset: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(240), default="", nullable=False)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), primary_key=True, nullable=False
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id"), primary_key=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class RolePermission(Base):
    """角色在某数据域（business/network/callback/template/system）上拥有的权限。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id"), primary_key=True, nullable=False
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id"), primary_key=True, nullable=False
    )
    domain: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="system", nullable=False
    )
