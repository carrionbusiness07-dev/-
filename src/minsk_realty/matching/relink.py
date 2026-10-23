"""Пересклейка объявлений, собранных без адреса (разовая починка после ошибки адаптера).

До 23.10.2026 адаптер Kufar читал адрес не из того поля, и каждое объявление Kufar
основало свой объект без улицы и дома. Объекты не сливаются, поэтому после починки
такое объявление само не найдёт свою пару на Realt: его нужно отцепить и склеить заново.

Трогаем только объекты из одного объявления площадки, у которого есть снимок без адреса,
а последний снимок уже с адресом. Объекты из списков пользователей не трогаем.
Пересклеенное основное объявление помечается (`match_basis.relinked`), поэтому повторный
запуск его не трогает, даже если пары для него так и не нашлось.

Запускать при остановленных сборщике и боте, после полного прохода площадки на новом
адаптере (docs/setup-home-computer.md): пересклейка держит в одной транзакции тысячи
объявлений.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Event, Listing, RealtyObject, Snapshot, Watch
from minsk_realty.matching.lock import try_lock
from minsk_realty.matching.matcher import FOUNDER_BASIS
from minsk_realty.matching.pipeline import ProcessStats, process_after_run

log = logging.getLogger(__name__)


def _no_address():
    return Snapshot.address_raw.is_(None) & Snapshot.street.is_(None)


def unaddressed_objects(session: Session, source: str) -> list[int]:
    """Объекты, которые основало одно объявление `source`, собранное без адреса."""
    single = (
        select(Listing.object_id)
        .where(Listing.object_id.is_not(None))
        .group_by(Listing.object_id)
        .having(func.count() == 1)
    )
    candidates = session.execute(
        select(Listing.id, Listing.object_id, Listing.match_basis).where(
            Listing.source == source,
            Listing.object_id.in_(single),
            exists().where(Snapshot.listing_id == Listing.id, _no_address()),
            ~exists().where(Watch.object_id == Listing.object_id),
        )
    ).all()
    out = []
    for listing_id, object_id, basis in candidates:
        if (basis or {}).get("relinked"):
            continue  # уже пересклеено, пары не нашлось — это и есть ответ
        last = session.scalar(
            select(Snapshot)
            .where(Snapshot.listing_id == listing_id)
            .order_by(Snapshot.seen_at.desc(), Snapshot.id.desc())
            .limit(1)
        )
        if last is not None and (last.address_raw or last.street):
            out.append(object_id)
    return sorted(out)


def relink_unaddressed(
    session: Session, source: str = "kufar", now: datetime | None = None
) -> tuple[int, ProcessStats]:
    """Отцепить такие объявления, удалить их объекты и склеить заново. Коммит — у вызывающего.

    Если склейку сейчас выполняет сборщик, ничего не трогаем (stats.skipped).
    """
    if not try_lock(session):
        return 0, ProcessStats(skipped=True)
    object_ids = unaddressed_objects(session, source)
    relinked = list(session.scalars(select(Listing).where(Listing.object_id.in_(object_ids))))
    for item in relinked:
        item.object_id, item.match_confidence, item.match_basis = None, None, None
    session.flush()
    if object_ids:
        # События объекта однозначно следуют из снимков: склейка запишет их заново.
        session.execute(delete(Event).where(Event.object_id.in_(object_ids)))
        # Объект, который успели взять в список «Следить», не удаляем (он просто опустеет).
        session.execute(
            delete(RealtyObject).where(
                RealtyObject.id.in_(object_ids),
                ~exists().where(Watch.object_id == RealtyObject.id),
            )
        )
        session.flush()
        log.info("%s: пересклеиваю %d объявлений без адреса", source, len(object_ids))
    stats = process_after_run(session, source, now=now or datetime.now(UTC))
    for item in relinked:
        if (item.match_basis or {}).get("rule") == FOUNDER_BASIS["rule"]:
            item.match_basis = {**item.match_basis, "relinked": True}
    session.flush()
    return len(object_ids), stats
