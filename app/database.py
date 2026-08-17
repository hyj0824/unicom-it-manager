from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import BASE_DIR, get_settings


class Base(DeclarativeBase):
    pass


ALEMBIC_INI = BASE_DIR / "alembic.ini"


def _connect_args(database_url: str) -> dict[str, object]:
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    db_path = database_url.removeprefix("sqlite:///")
    path = Path(db_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)


settings = get_settings()
_ensure_sqlite_parent(settings.database_url)

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args(settings.database_url),
    future=True,
)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        # WAL、外键约束和 busy timeout 来自迁移方案对 SQLite 的运行约束。
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


def get_schema_head() -> str:
    """当前代码（迁移脚本）要求的数据库版本。"""

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def check_schema_current(target: Engine | None = None) -> None:
    """校验数据库结构已升级到当前代码对应的 Alembic 版本。

    生产启动不再依赖 create_all 自动变更结构；结构变化一律通过可审查的
    迁移脚本执行（`uv run alembic upgrade head`）。
    """

    target = target or engine
    with target.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()

    head = get_schema_head()
    if current != head:
        raise RuntimeError(
            "Database schema is not up to date "
            f"(database revision: {current or 'none'}, code requires: {head}). "
            "Stop all writers and run `uv run alembic upgrade head`."
        )
