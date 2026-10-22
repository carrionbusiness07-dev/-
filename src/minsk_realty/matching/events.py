"""События объекта строго по определениям SKILL.md («Склейка объектов» → «События»).

- new          — объект впервые появился на рынке (первое надёжное объявление).
- price_drop / price_up — цена изменилась минимум на 0.5% в исходной валюте объявления.
                 Смена валюты — не событие цены (база сравнения начинается заново).
                 Курс (price_usd) в сравнении не участвует вовсе.
                 При перевыкладывании тем же продавцом сравнивается последняя цена старого
                 объявления с первой ценой нового.
- relisted     — новое объявление объекта, а предыдущее того же продавца снято не более
                 30 дней назад (или снято в льготный срок после появления нового).
- cross_listed — объект одновременно активен на 2+ площадках или у 2+ продавцов. Событие
                 пишется для объявления, которое «присоединилось» позже.
- removed      — все объявления объекта неактивны ≥ 3 суток. `at` — когда объект последний
                 раз видели на рынке, `payload.confirmed_at` — когда истекли 3 суток.
- returned     — объект после removed снова активен.

Объектные события (new, relisted, cross_listed, removed, returned) считаются только по
надёжно привязанным объявлениям (high/medium). Объявление со склейкой low даёт только
события своей цены, и они пишутся с notify=False: в историю идут, пользователю — нет.

Идемпотентность: у каждого события детерминированный `dedup_key`, уникальный в пределах
объекта. Повторная обработка тех же данных ничего не добавляет.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Event, Listing, Snapshot
from minsk_realty.matching.config import MatchingConfig
from minsk_realty.matching.rules import TRUSTED


def _norm(text: str | None) -> str | None:
    return " ".join(text.lower().replace("ё", "е").split()) if text else None


def same_seller(a: Listing, b: Listing) -> bool:
    if a.source == b.source and a.seller_key and a.seller_key == b.seller_key:
        return True
    return bool(_norm(a.agency_name) and _norm(a.agency_name) == _norm(b.agency_name))


def different_sellers(a: Listing, b: Listing) -> bool:
    if a.source != b.source:
        return True
    return bool(a.seller_key and b.seller_key and a.seller_key != b.seller_key)


def _priced(session: Session, listing_id: int) -> list[Snapshot]:
    snaps = session.scalars(
        select(Snapshot)
        .where(Snapshot.listing_id == listing_id)
        .order_by(Snapshot.seen_at, Snapshot.id)
    ).all()
    return [s for s in snaps if s.price_amount is not None and s.price_currency is not None]


def price_change_pct(old: Decimal, new: Decimal) -> Decimal:
    return (Decimal(new) - Decimal(old)) / Decimal(old) * 100


def _price_payload(old: Snapshot, new: Snapshot, pct: Decimal) -> dict[str, Any]:
    return {
        "old": str(old.price_amount),
        "new": str(new.price_amount),
        "currency": new.price_currency,
        "pct": float(round(pct, 2)),
        "old_price_usd": str(old.price_usd) if old.price_usd is not None else None,
        "new_price_usd": str(new.price_usd) if new.price_usd is not None else None,
    }


class _Emitter:
    def __init__(self, session: Session, object_id: int, now: datetime) -> None:
        self.session, self.object_id, self.now = session, object_id, now
        self.events = list(session.scalars(select(Event).where(Event.object_id == object_id)))
        self.keys = {e.dedup_key for e in self.events}
        self.created: Counter[str] = Counter()

    def emit(
        self,
        type_: str,
        key: str,
        at: datetime,
        listing_id: int | None,
        payload: dict[str, Any],
        *,
        notify: bool = True,
    ) -> None:
        if key in self.keys:
            return
        event = Event(
            object_id=self.object_id,
            listing_id=listing_id,
            type=type_,
            at=at,
            payload=payload,
            dedup_key=key,
            notify=notify,
            created_at=self.now,
        )
        self.session.add(event)
        self.events.append(event)
        self.keys.add(key)
        self.created[type_] += 1


def generate_events(
    session: Session, object_id: int, cfg: MatchingConfig, now: datetime
) -> Counter[str]:
    listings = session.scalars(
        select(Listing)
        .where(Listing.object_id == object_id)
        .order_by(Listing.first_seen, Listing.id)
    ).all()
    em = _Emitter(session, object_id, now)
    if not listings:
        return em.created
    trusted = [item for item in listings if item.match_confidence in TRUSTED]

    if trusted:
        first = trusted[0]
        em.emit("new", "new", first.first_seen, first.id, {"source": first.source})

    for item in listings:
        _listing_prices(session, em, item, cfg)
    _relisted(session, em, trusted, cfg)
    _cross_listed(em, trusted)
    _removed_returned(em, listings, trusted, cfg, now)
    session.flush()
    return em.created


def _listing_prices(session: Session, em: _Emitter, item: Listing, cfg: MatchingConfig) -> None:
    """Изменения цены внутри одного объявления.

    База — цена последнего события (или первая цена). Мелкие шаги копятся, пока суммарно
    не наберут порог; колебание туда-обратно в пределах порога событий не даёт.
    """
    base: Snapshot | None = None
    for snap in _priced(session, item.id):
        if base is None or snap.price_currency != base.price_currency:
            base = snap  # смена валюты — не изменение цены
            continue
        pct = price_change_pct(base.price_amount, snap.price_amount)
        if abs(pct) >= cfg.price_change_min_pct:
            em.emit(
                "price_drop" if pct < 0 else "price_up",
                f"price:{snap.id}",
                snap.seen_at,
                item.id,
                _price_payload(base, snap, pct),
                notify=item.match_confidence in TRUSTED,
            )
            base = snap


def _relisted(session: Session, em: _Emitter, trusted: list[Listing], cfg: MatchingConfig) -> None:
    for new in trusted:
        previous = [
            old
            for old in trusted
            if old.id != new.id
            and (old.first_seen, old.id) < (new.first_seen, new.id)
            and old.status == "inactive"
            and same_seller(old, new)
            and -cfg.same_seller_overlap_grace
            <= new.first_seen - old.last_seen
            <= cfg.relisted_window
        ]
        if not previous:
            continue
        old = max(previous, key=lambda item: (item.last_seen, item.id))
        gap = new.first_seen - old.last_seen
        em.emit(
            "relisted",
            f"relisted:{new.id}",
            new.first_seen,
            new.id,
            {
                "previous_listing_id": old.id,
                "previous_source": old.source,
                "source": new.source,
                "gap_days": round(gap.total_seconds() / 86400, 1),
            },
        )
        old_prices, new_prices = _priced(session, old.id), _priced(session, new.id)
        if not (old_prices and new_prices):
            continue
        before, after = old_prices[-1], new_prices[0]
        if before.price_currency != after.price_currency:
            continue
        pct = price_change_pct(before.price_amount, after.price_amount)
        if abs(pct) >= cfg.price_change_min_pct:
            payload = _price_payload(before, after, pct) | {"via": "relisted"}
            em.emit(
                "price_drop" if pct < 0 else "price_up",
                f"price-relist:{new.id}",
                after.seen_at,
                new.id,
                payload,
            )


def _cross_listed(em: _Emitter, trusted: list[Listing]) -> None:
    active = [item for item in trusted if item.status == "active"]
    for j, later in enumerate(active):
        partner = next((a for a in active[:j] if different_sellers(a, later)), None)
        if partner is None:
            continue
        em.emit(
            "cross_listed",
            f"cross:{later.id}",
            max(later.first_seen, partner.first_seen),
            later.id,
            {
                "listing_ids": [partner.id, later.id],
                "sources": sorted({partner.source, later.source}),
                "same_source": partner.source == later.source,
            },
        )


def _removed_returned(
    em: _Emitter,
    listings: list[Listing],
    trusted: list[Listing],
    cfg: MatchingConfig,
    now: datetime,
) -> None:
    """removed — только если неактивны ВСЕ объявления, включая склеенные с low: если
    сомнительное объявление ещё висит, «снято со всех площадок» может оказаться ложью.
    returned — только по надёжным объявлениям: сомнительное не доказывает возвращение."""
    if not trusted:
        return
    state = sorted(
        (e for e in em.events if e.type in ("removed", "returned")),
        key=lambda e: (e.at, e.id or 0),
    )
    last = state[-1] if state else None
    active = [item for item in trusted if item.status == "active"]

    if not any(item.status == "active" for item in listings):
        last_seen = max(item.last_seen for item in listings)
        if now - last_seen >= cfg.removed_after and (last is None or last.type != "removed"):
            em.emit(
                "removed",
                f"removed:{last_seen.isoformat()}",
                last_seen,
                None,
                {
                    "confirmed_at": now.isoformat(),
                    "listing_ids": [item.id for item in listings],
                },
            )
        return

    if active and last is not None and last.type == "removed":
        back = [item for item in active if item.last_seen > last.at]
        if not back:
            return
        when = {
            item.id: item.first_seen if item.first_seen > last.at else item.last_seen
            for item in back
        }
        first_back = min(back, key=lambda item: (when[item.id], item.id))
        em.emit(
            "returned",
            f"returned:{last.at.isoformat()}",
            when[first_back.id],
            first_back.id,
            {
                "source": first_back.source,
                "new_listing": first_back.first_seen > last.at,
                "away_days": round((when[first_back.id] - last.at).total_seconds() / 86400, 1),
            },
        )
