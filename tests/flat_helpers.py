from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


BASE_DIR = Path(__file__).resolve().parent.parent


def make_session(path: Path) -> Session:
    url = f"sqlite:///{path}"
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return Session(create_engine(url))
