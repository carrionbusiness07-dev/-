"""Один проход сборщика по одной площадке."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from minsk_realty.db.models import SourceRun
from minsk_realty.db.repository import mark_unseen_inactive, save_snapshot
from minsk_realty.sources.base import SourceAdapter, SourceBlocked

log = logging.getLogger(__name__)

COMMIT_EVERY = 100
# Если не разобралась заметная доля объявлений, площадка, скорее всего, поменяла формат.
# Такой проход не считается полным, чтобы не погасить объявления по ошибке.
MAX_PARSE_ERROR_SHARE = 0.2


def collect(
    adapter: SourceAdapter,
    session_factory: sessionmaker[Session],
    *,
    max_pages: int = 500,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SourceRun:
    """Обойти выдачу площадки, записать снимки и закрыть запись `SourceRun`.

    Возвращает итоговую запись прохода (её же читает health-мониторинг).
    """
    with session_factory() as session:
        run = SourceRun(source=adapter.source, started_at=now(), status="running")
        session.add(run)
        session.commit()

        try:
            for raw in adapter.search(max_pages=max_pages):
                run.listings_seen += 1
                try:
                    snap = adapter.parse(raw, seen_at=run.started_at)
                    save_snapshot(session, snap)
                except Exception:  # одно битое объявление не должно ронять весь проход
                    run.parse_errors += 1
                    log.exception("%s: не разобрано объявление %s", adapter.source, raw.external_id)
                if run.listings_seen % COMMIT_EVERY == 0:
                    session.commit()
        except SourceBlocked as exc:
            run.status, run.error = "blocked", str(exc)[:1024]
            log.error("%s: площадка отказывает, источник остановлен: %s", adapter.source, exc)
        except Exception as exc:
            run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"[:1024]
            log.exception("%s: проход упал", adapter.source)
        else:
            run.status = "ok"
            too_many_errors = (
                run.listings_seen == 0
                or run.parse_errors / run.listings_seen > MAX_PARSE_ERROR_SHARE
            )
            run.complete = adapter.last_search_complete and not too_many_errors
            deactivated = mark_unseen_inactive(session, run)
            log.info(
                "%s: просмотрено %d, ошибок разбора %d, полный проход: %s, погашено %d",
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
