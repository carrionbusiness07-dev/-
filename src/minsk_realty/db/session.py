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
        event.listen(engine, "connect", _sqlite_on_connect)
        # Рецепт SQLAlchemy для pysqlite: транзакции открываем сами, иначе SAVEPOINT
        # (session.begin_nested) не работает, а сборщик держит на нём каждое объявление.
        event.listen(engine, "begin", lambda conn: conn.exec_driver_sql("BEGIN"))
    return engine


def _sqlite_on_connect(dbapi_connection, _record) -> None:
    dbapi_connection.isolation_level = None
    # В SQLite внешние ключи (и ON DELETE CASCADE) выключены по умолчанию.
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
