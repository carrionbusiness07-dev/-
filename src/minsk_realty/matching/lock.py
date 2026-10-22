"""Блокировка прохода склейки: сборщик и ручной `python -m minsk_realty.matching process`
не должны склеивать одновременно (два процесса создали бы два объекта для одной квартиры
и задвоили события).

Блокировка берётся неблокирующе: второй процесс пропускает проход, а не ждёт и не падает.
Держится до конца транзакции сессии (коммит или откат делает вызывающий):
- PostgreSQL — `pg_try_advisory_xact_lock`, снимается сервером при COMMIT/ROLLBACK;
- SQLite — файловая блокировка `<файл БД>.matching.lock` (flock), снимается по событию
  конца транзакции сессии. На платформах без fcntl (Windows) и для БД в памяти — без
  блокировки: это режим разработки, а сам SQLite всё равно пускает одного писателя.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session, SessionTransaction

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Произвольная константа проекта для pg_advisory_*: «KI» + номер блокировки.
ADVISORY_KEY = 0x4B49_0001
_INFO_KEY = "minsk_realty.matching.lock"
_LISTENER_KEY = "minsk_realty.matching.lock.listener"


def try_lock(session: Session) -> bool:
    """Взять блокировку прохода в текущей транзакции сессии. False — её держит другой процесс."""
    if session.info.get(_INFO_KEY):
        return True  # уже взята этой сессией в этой транзакции
    connection = session.connection()  # открыть транзакцию, если её ещё нет
    dialect = connection.dialect.name
    if dialect == "postgresql":
        ok = bool(
            session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": ADVISORY_KEY})
        )
        if ok:
            _hold(session, None)
        return ok
    if dialect == "sqlite":
        return _sqlite_file_lock(session, connection.engine.url.database)
    return True


def _sqlite_file_lock(session: Session, database: str | None) -> bool:
    if fcntl is None or not database or database == ":memory:" or database.startswith("file:"):
        return True
    path = os.path.abspath(database) + ".matching.lock"
    handle = open(path, "a+")  # noqa: SIM115 — живёт до конца транзакции
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _hold(session, handle)
    return True


def _hold(session: Session, handle: Any) -> None:
    session.info[_INFO_KEY] = {"root": session.get_transaction(), "handle": handle}
    if not session.info.get(_LISTENER_KEY):
        session.info[_LISTENER_KEY] = True
        event.listen(session, "after_transaction_end", _on_transaction_end)


def _on_transaction_end(session: Session, transaction: SessionTransaction) -> None:
    held = session.info.get(_INFO_KEY)
    if not held or held["root"] is not transaction:
        return
    session.info.pop(_INFO_KEY, None)
    handle = held["handle"]
    if handle is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
