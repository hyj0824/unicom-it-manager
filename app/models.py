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


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 客户名称不是业务唯一键；电话不存客户表，由 contacts 承担（见数据模型基线）。
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    plans: Mapped[list["CallbackPlan"]] = relationship(back_populates="customer")
    call_tasks: Mapped[list["CallTask"]] = relationship(back_populates="customer")
    call_records: Mapped[list["CallRecord"]] = relationship(back_populates="customer")
    contact_links: Mapped[list["CustomerContact"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class Script(TimestampMixin, Base):
    __tablename__ = "scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tts_status: Mapped[str] = mapped_column(String(32), default="not_generated", nullable=False)
    # 音频生成失败原因（成功或未生成时为空），供话术页展示失败状态详情。
    tts_error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    wav_path: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    plans: Mapped[list["CallbackPlan"]] = relationship(back_populates="script")
    call_tasks: Mapped[list["CallTask"]] = relationship(back_populates="script")
    call_records: Mapped[list["CallRecord"]] = relationship(back_populates="script")


class CallbackPlan(TimestampMixin, Base):
    __tablename__ = "callback_plans"
    __table_args__ = (Index("ix_callback_plans_contact_id", "contact_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cron_expr: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    customer: Mapped[Customer] = relationship(back_populates="plans")
    script: Mapped[Script] = relationship(back_populates="plans")
    contact: Mapped["Contact | None"] = relationship()
    call_tasks: Mapped[list["CallTask"]] = relationship(back_populates="plan")
    call_records: Mapped[list["CallRecord"]] = relationship(back_populates="plan")


class CallTask(TimestampMixin, Base):
    __tablename__ = "call_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("callback_plans.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    # 入队时从联系人快照的拨号号码；号码变更不影响已入队任务。
    dial_number: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    plan: Mapped[CallbackPlan | None] = relationship(back_populates="call_tasks")
    customer: Mapped[Customer] = relationship(back_populates="call_tasks")
    script: Mapped[Script] = relationship(back_populates="call_tasks")
    contact: Mapped["Contact | None"] = relationship()
    call_record: Mapped["CallRecord | None"] = relationship(back_populates="task")


class CallRecord(TimestampMixin, Base):
    __tablename__ = "call_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("call_tasks.id"), unique=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("callback_plans.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    dial_number: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    dialing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    operator_feedback: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recording_path: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    transcript_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sentiment: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    follow_up_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_result_json: Mapped[str] = mapped_column(Text, default="", nullable=False)

    task: Mapped[CallTask | None] = relationship(back_populates="call_record")
    plan: Mapped[CallbackPlan | None] = relationship(back_populates="call_records")
    customer: Mapped[Customer] = relationship(back_populates="call_records")
    script: Mapped[Script] = relationship(back_populates="call_records")
    contact: Mapped["Contact | None"] = relationship()
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


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    duty: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    customer_links: Mapped[list["CustomerContact"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )


class CustomerContact(TimestampMixin, Base):
    """客户主体与负责人的关联；职责（含客户、发展人、客户经理、网络维护责任人）挂在关联上。"""

    __tablename__ = "customer_contacts"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "contact_id", "duty", name="uq_customer_contacts_link"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id"), nullable=False, index=True
    )
    duty: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="contact_links")
    contact: Mapped[Contact] = relationship(back_populates="customer_links")


class BusinessService(TimestampMixin, Base):
    """业务实例：台账 A-M 列的规范化主体，业务号码为业务唯一键。"""

    __tablename__ = "business_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
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

    customer: Mapped[Customer] = relationship()
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
    maintenance_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_contacts.id")
    )
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
    maintenance_contact: Mapped["CustomerContact | None"] = relationship()


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
    """用户账号：只能由超级管理员创建；未绑定角色与数据范围前没有业务数据权限。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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


class CustomFieldDefinition(TimestampMixin, Base):
    """预留扩展字段定义：只允许管理员配置，不动态修改数据库列。"""

    __tablename__ = "custom_field_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    field_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    domain: Mapped[str] = mapped_column(String(32), default="business", nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CustomFieldValue(Base):
    __tablename__ = "custom_field_values"
    __table_args__ = (
        UniqueConstraint(
            "definition_id", "entity_type", "entity_id", name="uq_custom_field_values_target"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition_id: Mapped[int] = mapped_column(
        ForeignKey("custom_field_definitions.id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    value_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
