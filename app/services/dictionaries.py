from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import DictionaryCategory, DictionaryItem


def active_items(db: Session, category_code: str) -> list[DictionaryItem]:
    """返回某字典分类下启用的字典项（按排序）。"""

    return db.scalars(
        select(DictionaryItem)
        .join(DictionaryCategory, DictionaryItem.category_id == DictionaryCategory.id)
        .where(
            DictionaryCategory.code == category_code,
            DictionaryCategory.is_active.is_(True),
            DictionaryItem.is_active.is_(True),
        )
        .order_by(DictionaryItem.sort_order.asc(), DictionaryItem.id.asc())
    ).all()


def categories_with_items(db: Session) -> list[DictionaryCategory]:
    return db.scalars(
        select(DictionaryCategory)
        .where(DictionaryCategory.is_active.is_(True))
        .order_by(DictionaryCategory.sort_order.asc(), DictionaryCategory.id.asc())
    ).all()


def item_label(db: Session, item_id: int | None) -> str:
    if item_id is None:
        return ""
    item = db.get(DictionaryItem, item_id)
    return item.label if item else ""


def resolve_or_create_item(
    db: Session, category_code: str, value: str
) -> DictionaryItem | None:
    """Resolve a typed dictionary value, creating a controlled item when needed."""

    label = " ".join(value.split())
    if not label:
        return None
    if len(label) > 160:
        raise ValueError("自定义选项不能超过 160 个字符。")
    category = db.scalars(
        select(DictionaryCategory).where(
            DictionaryCategory.code == category_code,
            DictionaryCategory.is_active.is_(True),
        )
    ).first()
    if category is None:
        raise ValueError("字典分类不存在或已停用。")
    items = db.scalars(
        select(DictionaryItem).where(
            DictionaryItem.category_id == category.id,
            DictionaryItem.is_active.is_(True),
        )
    ).all()
    for item in items:
        if item.label.casefold() == label.casefold():
            return item
    sort_order = db.scalar(
        select(func.max(DictionaryItem.sort_order)).where(
            DictionaryItem.category_id == category.id
        )
    ) or 0
    item = DictionaryItem(
        category_id=category.id,
        label=label,
        sort_order=sort_order + 1,
    )
    db.add(item)
    db.flush()
    return item
