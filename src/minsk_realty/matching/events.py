"""События объекта строго по определениям SKILL.md («Склейка объектов» → «События»).

- new          — объект впервые появился на рынке (первое надёжное объявление).
- price_drop / price_up — цена изменилась минимум на 0.5% в исходной валюте объявления.
                 Смена валюты — не событие цены (база сравнения начинается заново).
                 Курс (price_usd) в сравнении не участвует вовсе.
                 При перевыкладывании тем же продавцом сравнивается последняя цена старого
                 объявления с первой ценой нового (payload.via = "relisted").
- relisted     — новое объявление объекта, а предыдущее того же продавца снято не более
                 30 дней назад (или снято в льготный срок после появления нового).
- cross_listed — объект одновременно активен на 2+ площадках или у 2+ продавцов. Событие
                 пишется для объявления, которое «присоединилось» позже.
- removed      — все объявления объекта неактивны ≥ 3 суток. `at` — когда объект последний
                 раз видели на рынке, `payload.confirmed_at` — когда истекли 3 суток.
- returned     — объект после removed снова активен.

Флаги события:
- trusted — событие из надёжной склейки (high/medium); из него строится история в отчёте.
  Объектные события (new, relisted, cross_listed, removed, returned) считаются только по
  надёжно привязанным объявлениям. Объявление со склейкой low даёт только события своей
  цены, с trusted=False: в отчёт объекта и уведомления они не идут.
- notify — событие можно разослать пользователю: trusted и свежее (на рынке произошло не
  раньше `notify_max_age` назад; для removed — считая от подтверждения через 3 суток).
  Первый запуск на накопленной БД и поздняя склейка не дают лавины старых уведомлений.
- retracted_at — событие отозвано: объявление, на которое оно опирается, отцепили от объекта
  или понизили до low. Строка не удаляется (событие могло уже уйти пользователю), notify
  сбрасывается. Если то же событие снова становится верным, оно восстанавливается без
  уведомления — а не создаётся заново с notify=True.

Идемпотентность: у каждого события детерминированный `dedup_key`, уникальный в пределах
объекта. Повторная обработка тех же данных ничего не добавляет и не меняет.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Event, Listing, Snapshot
from minsk_realty.matching.config import MatchingConfig
from minsk_realty.matching.rules import TRUSTED

PRICE_TYPES = ("price_drop", "price_up")
_CHUNK = 500


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


def _priced_query(listing_ids: list[int]):
    return (
        select(
            Snapshot.id,
            Snapshot.listing_id,
            Snapshot.seen_at,
            Snapshot.price_amount,
            Snapshot.price_currency,
            Snapshot.price_usd,
        )
        .where(
            Snapshot.listing_id.in_(listing_ids),
            Snapshot.price_amount.is_not(None),
            Snapshot.price_currency.is_not(None),
        )
        .order_by(Snapshot.listing_id, Snapshot.seen_at, Snapshot.id)
    )


def load_priced(session: Session, listing_ids: Iterable[int]) -> dict[int, list[Any]]:
    """Снимки с ценой по объявлениям, по времени (пачками)."""
    ids = sorted(set(listing_ids))
    out: dict[int, list[Any]] = defaultdict(list)
    for i in range(0, len(ids), _CHUNK):
        for row in session.execute(_priced_query(ids[i : i + _CHUNK])):
            out[row.listing_id].append(row)
    return out


def price_change_pct(old: Decimal, new: Decimal) -> Decimal:
    return (Decimal(new) - Decimal(old)) / Decimal(old) * 100


def _price_payload(old: Any, new: Any, pct: Decimal) -> dict[str, Any]:
    return {
        "old": str(old.price_amount),
        "new": str(new.price_amount),
        "currency": new.price_currency,
        "pct": float(round(pct, 2)),
        "old_price_usd": str(old.price_usd) if old.price_usd is not None else None,
        "new_price_usd": str(new.price_usd) if new.price_usd is not None else None,
    }


class _Emitter:
    def __init__(
        self,
        session: Session,
        object_id: int,
        now: datetime,
        cfg: MatchingConfig,
        events: list[Event],
    ) -> None:
        self.session, self.object_id, self.now, self.cfg = session, object_id, now, cfg
        self.events = events
        self.by_key = {e.dedup_key: e for e in events}
        self.created: Counter[str] = Counter()

    def fresh(self, type_: str, at: datetime) -> bool:
        known_at = at + self.cfg.removed_after if type_ == "removed" else at
        return self.now - known_at <= self.cfg.notify_max_age

    def emit(
        self,
        type_: str,
        key: str,
        at: datetime,
        listing_id: int | None,
        payload: dict[str, Any],
        *,
        trusted: bool = True,
    ) -> None:
        existing = self.by_key.get(key)
        if existing is not None:
            if existing.retracted_at is not None:
                # Событие снова верно. Его могли уже разослать до отзыва — не уведомляем.
                existing.retracted_at = None
                existing.notify = False
                existing.type, existing.at, existing.listing_id = type_, at, listing_id
                existing.payload, existing.trusted = payload, trusted
                self.created["restored"] += 1
            return
        event = Event(
            object_id=self.object_id,
            listing_id=listing_id,
            type=type_,
            at=at,
            payload=payload,
            dedup_key=key,
            trusted=trusted,
            notify=trusted and self.fresh(type_, at),
            created_at=self.now,
        )
        self.session.add(event)
        self.events.append(event)
        self.by_key[key] = event
        self.created[type_] += 1

    def retract(self, event: Event) -> None:
        event.retracted_at = self.now
        event.notify = False
        self.created["retracted"] += 1


def _refs(event: Event, by_key: dict[str, Event]) -> set[int]:
    """Объявления, на которые опирается событие."""
    payload = event.payload or {}
    refs = {event.listing_id, payload.get("previous_listing_id")}
    refs.update(payload.get("listing_ids") or [])
    if event.dedup_key.startswith("price-relist:") and "previous_listing_id" not in payload:
        # Строки до 0003: предыдущее объявление записано только в парном relisted.
        pair = by_key.get("relisted:" + event.dedup_key.split(":", 1)[1])
        if pair is not None:
            refs.add((pair.payload or {}).get("previous_listing_id"))
    refs.discard(None)
    return refs


def _reconcile(em: _Emitter, listings: list[Listing]) -> None:
    """Отозвать события, опирающиеся на объявления, которых в объекте больше нет (или они
    теперь low), и синхронизировать trusted у событий цены с уверенностью склейки."""
    members = {item.id: item for item in listings}
    trusted = {item.id for item in listings if item.match_confidence in TRUSTED}
    for event in em.events:
        if event.retracted_at is not None:
            continue
        refs = _refs(event, em.by_key)
        own_price = event.type in PRICE_TYPES and not event.dedup_key.startswith("price-relist:")
        if own_price:
            item = members.get(event.listing_id)
            if item is None:
                em.retract(event)
            elif event.trusted != (item.match_confidence in TRUSTED):
                event.trusted = item.match_confidence in TRUSTED
        elif event.type == "removed":
            # «Сняты все объявления» проверялось и по слабым: им достаточно быть в объекте.
            if not refs <= members.keys():
                em.retract(event)
        elif not refs <= trusted:
            em.retract(event)


def generate_events(
    session: Session, object_id: int, cfg: MatchingConfig, now: datetime
) -> Counter[str]:
    """События одного объекта (для разбора и тестов). Проход использует `generate_for`."""
    return generate_for(session, [object_id], cfg, now)


def generate_for(
    session: Session,
    object_ids: Iterable[int],
    cfg: MatchingConfig,
    now: datetime,
    *,
    members: dict[int, list[Listing]] | None = None,
) -> Counter[str]:
    """События многих объектов: объявления, цены и события читаются пачками.

    `members` — уже известный состав объектов (кеш прохода), чтобы не читать его заново.
    """
    total: Counter[str] = Counter()
    ids = sorted(set(object_ids))
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        listings: dict[int, list[Listing]] = defaultdict(list)
        if members is not None and all(oid in members for oid in chunk):
            for oid in chunk:
                listings[oid] = list(members[oid])
        else:
            for item in session.scalars(
                select(Listing)
                .where(Listing.object_id.in_(chunk))
                .order_by(Listing.first_seen, Listing.id)
            ):
                listings[item.object_id].append(item)
        events: dict[int, list[Event]] = defaultdict(list)
        for event in session.scalars(
            select(Event).where(Event.object_id.in_(chunk)).order_by(Event.id)
        ):
            events[event.object_id].append(event)
        priced = load_priced(session, (item.id for rows in listings.values() for item in rows))
        for oid in chunk:
            em = _Emitter(session, oid, now, cfg, events[oid])
            _object_events(em, listings[oid], priced, cfg, now)
            total.update(em.created)
    session.flush()
    return total


def _object_events(
    em: _Emitter,
    listings: list[Listing],
    priced: dict[int, list[Any]],
    cfg: MatchingConfig,
    now: datetime,
) -> None:
    _reconcile(em, listings)
    if not listings:
        return
    trusted = [item for item in listings if item.match_confidence in TRUSTED]
    if trusted:
        first = trusted[0]
        em.emit("new", "new", first.first_seen, first.id, {"source": first.source})
    for item in listings:
        _listing_prices(em, item, priced.get(item.id, []), cfg)
    _relisted(em, trusted, priced, cfg)
    _cross_listed(em, trusted)
    _removed_returned(em, listings, trusted, cfg, now)


def _listing_prices(em: _Emitter, item: Listing, snaps: list[Any], cfg: MatchingConfig) -> None:
    """Изменения цены внутри одного объявления.

    База — цена последнего события (или первая цена). Мелкие шаги копятся, пока суммарно
    не наберут порог; колебание туда-обратно в пределах порога событий не даёт.
    """
    base: Any = None
    for snap in snaps:
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
                trusted=item.match_confidence in TRUSTED,
            )
            base = snap


def _relisted(
    em: _Emitter, trusted: list[Listing], priced: dict[int, list[Any]], cfg: MatchingConfig
) -> None:
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
        old_prices, new_prices = priced.get(old.id, []), priced.get(new.id, [])
        if not (old_prices and new_prices):
            continue
        before, after = old_prices[-1], new_prices[0]
        if before.price_currency != after.price_currency:
            continue
        pct = price_change_pct(before.price_amount, after.price_amount)
        if abs(pct) >= cfg.price_change_min_pct:
            payload = _price_payload(before, after, pct) | {
                "via": "relisted",
                "previous_listing_id": old.id,
            }
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
        (e for e in em.events if e.type in ("removed", "returned") and e.retracted_at is None),
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
