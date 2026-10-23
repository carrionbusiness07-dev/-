"""Один проход сборщика по одной площадке."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from minsk_realty.db.models import SourceRun
from minsk_realty.db.repository import mark_unseen_inactive, save_snapshot, touch_listing
from minsk_realty.sources.base import SourceAdapter, SourceBlocked

log = logging.getLogger(__name__)

COMMIT_EVERY = 100
# Если не разобралась заметная доля объявлений, площадка, скорее всего, поменяла формат.
# Такой проход не считается полным, чтобы не погасить объявления по ошибке.
MAX_PARSE_ERROR_SHARE = 0.2
# Проход идёт 5–20 минут. Запись «running» старше этого — от процесса, который убили
# (выключили компьютер, Docker Desktop закрылся), и закрыть её он уже не сможет.
STALE_RUN_AFTER = timedelta(hours=2)
INTERRUPTED = "interrupted"


def collect(
    adapter: SourceAdapter,
    session_factory: sessionmaker[Session],
    *,
    max_pages: int = 500,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SourceRun:
    """Обойти выдачу площадки, записать снимки и закрыть запись `SourceRun`.

    Каждое объявление пишется в своей точке сохранения (SAVEPOINT): ошибка разбора или БД
    на одном объявлении не теряет остальные и не ломает сессию.
    """
    with session_factory() as session:
        run = SourceRun(source=adapter.source, started_at=now(), status="running")
        session.add(run)
        session.commit()

        try:
            for raw in adapter.search(max_pages=max_pages):
                run.listings_seen += 1
                try:
                    with session.begin_nested():
                        save_snapshot(session, adapter.parse(raw, seen_at=run.started_at))
                except Exception as exc:  # noqa: BLE001 — одно битое объявление не роняет проход
                    run.parse_errors += 1
                    log.warning(
                        "%s: не записано объявление %s: %s",
                        adapter.source,
                        raw.external_id,
                        _short_error(exc),
                    )
                    with session.begin_nested():
                        touch_listing(session, adapter.source, raw.external_id, run.started_at)
                if run.listings_seen % COMMIT_EVERY == 0:
                    session.commit()
        except SourceBlocked as exc:
            run.status, run.error = "blocked", str(exc)[:1024]
            log.error("%s: площадка отказывает, источник остановлен: %s", adapter.source, exc)
        except Exception as exc:  # noqa: BLE001 — любой сбой закрывает проход как failed
            run.status, run.error = "failed", _short_error(exc)
            log.error("%s: проход упал: %s", adapter.source, run.error)
        except BaseException:  # SIGTERM/Ctrl+C: закрыть запись прохода и выйти
            run.status, run.error = "failed", INTERRUPTED
            raise
        else:
            run.status = "ok"
            run.error = None  # мог пометить «прерван» соседний процесс, пока проход шёл
            run.complete = adapter.last_search_complete and _few_errors(run)
            deactivated = mark_unseen_inactive(session, run)
            log.info(
                "%s: просмотрено %d, ошибок %d, полный проход: %s, погашено %d",
                adapter.source,
                run.listings_seen,
                run.parse_errors,
                run.complete,
                deactivated,
            )
        finally:
            run.finished_at = now()
            session.commit()
        return run


def close_stale_runs(
    session: Session, *, older_than: timedelta | None = STALE_RUN_AFTER, at: datetime | None = None
) -> int:
    """Закрыть проходы, оставшиеся «running» после убитого процесса, как прерванные.

    `older_than=None` — закрыть все: так делает сборщик при запуске, когда его прошлые
    проходы точно никто не ведёт.
    """
    at = at or datetime.now(UTC)
    query = update(SourceRun).where(SourceRun.status == "running")
    if older_than is not None:
        query = query.where(SourceRun.started_at < at - older_than)
    closed = session.execute(
        query.values(status="failed", error=INTERRUPTED, finished_at=at),
        execution_options={"synchronize_session": False},
    ).rowcount
    for obj in list(session.identity_map.values()):
        if isinstance(obj, SourceRun):
            session.expire(obj)  # перечитать из БД при следующем обращении
    if closed:
        log.warning("закрыто прерванных проходов: %d", closed)
    return closed or 0


def last_full_pass(session: Session, source: str) -> datetime | None:
    """Начало последнего полного успешного прохода площадки."""
    value = session.scalar(
        select(SourceRun.started_at)
        .where(SourceRun.source == source, SourceRun.status == "ok", SourceRun.complete.is_(True))
        .order_by(SourceRun.started_at.desc())
        .limit(1)
    )
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


def is_blocked(session: Session, source: str) -> bool:
    """Последний завершённый проход площадки закончился блокировкой (403/429).

    Такую площадку сборщик больше не трогает, пока человек не запустит его с `--unblock`.
    """
    status = session.scalar(
        select(SourceRun.status)
        .where(SourceRun.source == source, SourceRun.finished_at.is_not(None))
        .order_by(SourceRun.started_at.desc())
        .limit(1)
    )
    return status == "blocked"


def _few_errors(run: SourceRun) -> bool:
    return run.listings_seen > 0 and run.parse_errors / run.listings_seen <= MAX_PARSE_ERROR_SHARE


def _short_error(exc: Exception) -> str:
    # Текст ошибок SQLAlchemy содержит SQL и параметры (url, адрес) — в журнал и алерты не нужен.
    if isinstance(exc, SQLAlchemyError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"[:1024]
