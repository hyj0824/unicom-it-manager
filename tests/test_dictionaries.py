from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.dictionaries import active_items, resolve_or_create_item

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def test_resolve_or_create_item_reuses_existing_and_adds_custom_value(db: Session) -> None:
    existing = active_items(db, "county")[0]
    assert resolve_or_create_item(db, "county", existing.label).id == existing.id

    custom = resolve_or_create_item(db, "county", "新建测试县分")
    assert custom is not None
    assert custom.label == "新建测试县分"
    assert resolve_or_create_item(db, "county", " 新建测试县分 ").id == custom.id
