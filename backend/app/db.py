"""Database engine setup and session dependency."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings


def _make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        # SQLite 连接创建极廉价，用 NullPool 避免连接池耗尽；
        # WAL + busy_timeout 提升并发读写容忍度。
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001, ANN202
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        return engine
    return create_engine(database_url)


_engine: Engine | None = None


def init_db() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
    get_settings().data_dir.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        return init_db()
    return _engine


def engine_db_path() -> Path | None:
    """当前引擎对应的 SQLite 文件路径（非 sqlite 或内存库返回 None）。"""
    engine = get_engine()
    url = engine.url
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    database = url.database
    if database == ":memory:":
        return None
    return Path(database)


def reset_engine(engine: Engine | None) -> None:
    """Test hook: swap in an isolated engine."""
    global _engine
    _engine = engine


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
