from __future__ import annotations

from sqlalchemy import select
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
