from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    BusinessService,
    CallRecord,
    CallTask,
    CallbackPlan,
    Contact,
    Customer,
    CustomerContact,
)

# 职责为空字符串的关联视为“客户默认联系人”。v1 演示的客户表单直接维护
# 这一条关联；正式台账中的职责使用 contact_duty 字典（含客户拓展职责）。
DEFAULT_DUTY = ""


def default_contact(db: Session, customer: Customer) -> Contact | None:
    """返回客户的默认外呼联系人（默认职责优先，其次创建时间最早）。"""

    link = db.scalars(
        select(CustomerContact)
        .where(
            CustomerContact.customer_id == customer.id,
            CustomerContact.is_active.is_(True),
        )
        .order_by(CustomerContact.duty.asc(), CustomerContact.id.asc())
    ).first()
    return link.contact if link else None


def sync_default_contact(db: Session, customer: Customer, phone: str) -> Contact:
    """把客户表单上的电话同步到默认联系人；没有默认联系人时自动创建。"""

    phone = phone.strip()
    link = db.scalars(
        select(CustomerContact)
        .where(CustomerContact.customer_id == customer.id)
        .order_by(CustomerContact.duty.asc(), CustomerContact.id.asc())
    ).first()
    if link is not None:
        link.contact.phone = phone
        if not link.contact.name:
            link.contact.name = customer.name
        return link.contact

    contact = Contact(name=customer.name, phone=phone)
    db.add(contact)
    db.add(CustomerContact(customer=customer, contact=contact, duty=DEFAULT_DUTY))
    return contact


def customer_phone_map(db: Session, customers: list[Customer]) -> dict[int, str]:
    """构建 customer_id -> 默认联系人电话 的映射，供页面展示。"""

    ids = [c.id for c in customers]
    if not ids:
        return {}
    links = db.scalars(
        select(CustomerContact).where(CustomerContact.customer_id.in_(ids))
    ).all()
    result: dict[int, str] = {}
    for link in links:
        if link.customer_id not in result and link.contact and link.contact.phone:
            result[link.customer_id] = link.contact.phone
    return result


def referencing_counts(db: Session, customer: Customer) -> dict[str, int]:
    """统计引用该客户主体、导致无法硬删除的业务对象数量。

    供删除确认与删除失败提示使用，避免只给笼统的完整性错误。
    """

    return {
        "plans": db.scalar(
            select(func.count(CallbackPlan.id)).where(CallbackPlan.customer_id == customer.id)
        )
        or 0,
        "tasks": db.scalar(
            select(func.count(CallTask.id)).where(CallTask.customer_id == customer.id)
        )
        or 0,
        "records": db.scalar(
            select(func.count(CallRecord.id)).where(CallRecord.customer_id == customer.id)
        )
        or 0,
        "services": db.scalar(
            select(func.count(BusinessService.id)).where(
                BusinessService.customer_id == customer.id, BusinessService.is_active.is_(True)
            )
        )
        or 0,
        "contacts": db.scalar(
            select(func.count(CustomerContact.id)).where(
                CustomerContact.customer_id == customer.id
            )
        )
        or 0,
    }
