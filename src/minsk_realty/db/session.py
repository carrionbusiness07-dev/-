"""Подключение к БД. Адрес берётся из DATABASE_URL (по умолчанию локальный SQLite)."""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker

DEFAULT_URL = "sqlite:///minsk_realty.db"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None) -> Engine:
    engine = create_engine(url or database_url())
    if engine.dialect.name == "sqlite":
        # В SQLite внешние ключи (и ON DELETE CASCADE) выключены по умолчанию.
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
