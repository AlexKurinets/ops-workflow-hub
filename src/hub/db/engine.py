from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hub.db.models import Base


@lru_cache
def get_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True)


def get_session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


def init_db(url: str) -> None:
    Base.metadata.create_all(get_engine(url))


def reset_db(url: str) -> None:
    """Truncate every table. Used by the test suite and by the seed command."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
