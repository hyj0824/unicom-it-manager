from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
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
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    plans: Mapped[list["CallbackPlan"]] = relationship(back_populates="customer")
    call_tasks: Mapped[list["CallTask"]] = relationship(back_populates="customer")
    call_records: Mapped[list["CallRecord"]] = relationship(back_populates="customer")


class Script(TimestampMixin, Base):
    __tablename__ = "scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tts_status: Mapped[str] = mapped_column(String(32), default="not_generated", nullable=False)
    wav_path: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    plans: Mapped[list["CallbackPlan"]] = relationship(back_populates="script")
    call_tasks: Mapped[list["CallTask"]] = relationship(back_populates="script")
    call_records: Mapped[list["CallRecord"]] = relationship(back_populates="script")


class CallbackPlan(TimestampMixin, Base):
    __tablename__ = "callback_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cron_expr: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    customer: Mapped[Customer] = relationship(back_populates="plans")
    script: Mapped[Script] = relationship(back_populates="plans")
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
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    plan: Mapped[CallbackPlan | None] = relationship(back_populates="call_tasks")
    customer: Mapped[Customer] = relationship(back_populates="call_tasks")
    script: Mapped[Script] = relationship(back_populates="call_tasks")
    call_record: Mapped["CallRecord | None"] = relationship(back_populates="task")


class CallRecord(TimestampMixin, Base):
    __tablename__ = "call_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("call_tasks.id"), unique=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("callback_plans.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
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

    call_record: Mapped[CallRecord] = relationship(back_populates="events")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
