from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from app.database import check_schema_current, get_schema_head

BASE_DIR = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "app_settings",
    "audit_logs",
    "business_services",
    "call_events",
    "call_records",
    "call_tasks",
    "callback_plans",
    "change_items",
    "change_sets",
    "contacts",
    "customer_contacts",
    "customers",
    "dictionary_categories",
    "dictionary_items",
    "import_batches",
    "network_devices",
    "permissions",
    "role_permissions",
    "roles",
    "scan_schedules",
    "scripts",
    "staging_rows",
    "user_roles",
    "users",
}


def _alembic_config(db_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_upgrade_head_creates_full_schema_and_seeds(tmp_path: Path) -> None:
    db_path = tmp_path / "migrate.db"
    url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables
    assert "phone" not in {
        col["name"] for col in inspect(engine).get_columns("customers")
    }

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == get_schema_head()
        )
        assert conn.execute(text("SELECT COUNT(*) FROM permissions")).scalar() == 12
        assert conn.execute(text("SELECT COUNT(*) FROM roles")).scalar() == 5
        assert conn.execute(text("SELECT COUNT(*) FROM role_permissions")).scalar() == 29
        assert (
            conn.execute(text("SELECT COUNT(*) FROM dictionary_categories")).scalar()
            == 10
        )
        assert conn.execute(text("SELECT COUNT(*) FROM dictionary_items")).scalar() == 69
        county = conn.execute(
            text(
                "SELECT COUNT(*) FROM dictionary_items i "
                "JOIN dictionary_categories c ON i.category_id = c.id "
                "WHERE c.code = 'county'"
            )
        ).scalar()
        assert county == 10

    check_schema_current(engine)  # 当前版本不抛异常


def test_check_schema_current_rejects_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        check_schema_current(engine)
    engine.dispose()


def test_downgrade_then_upgrade_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "roundtrip.db"
    url = f"sqlite:///{db_path}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(url)
    assert "customers" not in set(inspect(engine).get_table_names())
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM roles")).scalar() == 5
    engine.dispose()
