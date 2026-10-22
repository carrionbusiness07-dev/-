"""Одна точка входа после прохода сборщика: пересмотр склеек, склейка новых, события."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Listing
from minsk_realty.matching.config import DEFAULT_CONFIG, MatchingConfig
from minsk_realty.matching.events import generate_for
from minsk_realty.matching.features import FeatureCache
from minsk_realty.matching.lock import try_lock
from minsk_realty.matching.matcher import (
    FOUNDER_BASIS,
    create_object,
    detach,
    evaluate,
    is_founder,
    link,
    linked_features,
    refresh_object,
)
from minsk_realty.matching.rules import compare

log = logging.getLogger(__name__)


@dataclass
class ProcessStats:
    linked: Counter[str] = field(default_factory=Counter)  # по уверенности
    created: int = 0
    deferred: int = 0
    ambiguous: int = 0
    regraded: int = 0
    detached: int = 0
    refreshed: int = 0
    iterations: int = 0
    popular_photos: int = 0
    # Проход пропущен: склейку уже выполняет другой процесс.
    skipped: bool = False
    events: Counter[str] = field(default_factory=Counter)

    @property
    def changes(self) -> int:
        """Сколько решений склейки изменилось (0 — данные уже в неподвижной точке)."""
        return sum(self.linked.values()) + self.created + self.regraded + self.detached

    def __str__(self) -> str:
        if self.skipped:
            return "пропущено: склейку выполняет другой процесс"
        linked = ", ".join(f"{k}={v}" for k, v in sorted(self.linked.items())) or "0"
        events = ", ".join(f"{k}={v}" for k, v in sorted(self.events.items())) or "0"
        return (
            f"склеено: {linked}; новых объектов: {self.created} (неоднозначных {self.ambiguous}); "
            f"отложено: {self.deferred}; пересмотрено: {self.regraded}; "
            f"отцеплено: {self.detached}; итераций: {self.iterations}; событий: {events}"
        )


def process_after_run(
    session: Session,
    source: str | None = None,
    now: datetime | None = None,
    *,
    config: MatchingConfig = DEFAULT_CONFIG,
    full: bool = False,
) -> ProcessStats:
    """Обработать БД после прохода сборщика. Коммит — на стороне вызывающего.

    0. Взять блокировку прохода (до конца транзакции). Занята другим процессом — пропустить.
    1. Пересмотреть склейки объектов с недавно виденными объявлениями (все, кроме основного
       объявления объекта): поднять или понизить уверенность, при противоречии — отцепить.
       При нехватке данных склейку не трогаем.
    2. Склеить объявления без объекта (новые, отложенные, отцеплённые) — по всем площадкам,
       `source` нужен только для журнала.
    3. Повторять 1–2 для изменившихся объектов, пока что-то меняется: новая склейка может
       изменить вердикт для соседей по объекту. Итог — неподвижная точка: повторный проход
       на тех же данных ничего не меняет.
    4. Обновить поля объектов по их надёжным объявлениям (до шагов 1–3 — для недавно
       виденных, после — для изменившихся) и сгенерировать события для объектов, которые
       недавно были на рынке или изменились (с отзывом событий отцеплённых объявлений).

    `full=True` — пересмотреть всё, а не только объявления, виденные за
    `config.events_lookback`.
    """
    now = now or datetime.now(UTC)
    cfg = config
    stats = ProcessStats()
    if not try_lock(session):
        stats.skipped = True
        log.warning("склейка после прохода %s пропущена: её выполняет другой процесс", source)
        return stats
    cache = FeatureCache(session, cfg)
    stats.popular_photos = cache.load_all_popular()
    since = None if full else now - cfg.events_lookback

    recent = select(Listing.object_id).where(Listing.object_id.is_not(None))
    if since is not None:
        recent = recent.where(Listing.last_seen >= since)
    recent_objects = set(session.scalars(recent.distinct()))
    pending = list(
        session.scalars(
            select(Listing)
            .where(Listing.object_id.is_(None))
            .order_by(Listing.first_seen, Listing.id)
        )
    )
    cache.preload(pending)
    # Поля объектов — по свежим данным до отбора кандидатов: если продавец уточнил площадь
    # основного объявления, объект должен находиться по новой площади уже в этом проходе.
    stats.refreshed += _refresh(session, cache, recent_objects)

    touched: set[int] = set()
    check = set(recent_objects)
    for _ in range(cfg.max_iterations):
        stats.iterations += 1
        changed: set[int] = set()
        detached = _regrade(session, cache, check, cfg, now, stats, changed)
        pending = _link_pending(session, cache, [*pending, *detached], cfg, now, stats, changed)
        touched |= changed
        if not changed:
            break
        check = changed
    else:
        log.warning(
            "склейка не сошлась за %d итераций, последние изменения: объекты %s",
            cfg.max_iterations,
            sorted(changed)[:20],
        )
    stats.deferred = len(pending)
    session.flush()

    # 4. Поля объектов и события.
    scope = touched | recent_objects
    stats.refreshed += _refresh(session, cache, touched)
    stats.events.update(generate_for(session, scope, cfg, now, members=cache.members_map(scope)))
    session.flush()

    log.info("склейка после прохода %s: %s", source or "-", stats)
    return stats


def _refresh(session: Session, cache: FeatureCache, object_ids: set[int]) -> int:
    cache.preload_members(object_ids)
    cache.preload([item for oid in object_ids for item in cache.members(oid)])
    count = sum(refresh_object(session, cache, object_id) for object_id in sorted(object_ids))
    session.flush()
    return count


def _regrade(
    session: Session,
    cache: FeatureCache,
    object_ids: set[int],
    cfg: MatchingConfig,
    now: datetime,
    stats: ProcessStats,
    changed: set[int],
) -> list[Listing]:
    """Пересмотреть склейки объектов. Возвращает отцеплённые объявления."""
    cache.preload_members(object_ids)
    cache.preload([item for oid in object_ids for item in cache.members(oid)])
    detached: list[Listing] = []
    for object_id in sorted(object_ids):
        for listing in list(cache.members(object_id)):
            members = cache.members(object_id)
            if listing.object_id != object_id:
                continue
            if is_founder(listing, members):
                if listing.match_basis is None:
                    # Склейка до 0003: закрепить основное объявление, чтобы оно не сменилось,
                    # когда к объекту присоединится объявление с более ранним first_seen.
                    listing.match_basis = dict(FOUNDER_BASIS)
                continue
            f = cache.get(listing)
            trusted, weak = linked_features(session, cache, object_id, exclude=listing.id)
            verdict = compare(f, object_id, trusted, cfg, now, weak=weak)
            if verdict.insufficient or verdict.outcome == "defer":
                continue  # данных не хватает или продавец ещё решает — склейку не трогаем
            if verdict.outcome == "match":
                basis = verdict.basis()
                if listing.match_basis != basis:
                    listing.match_basis = basis
                if verdict.confidence != listing.match_confidence:
                    log.info(
                        "склейка %s → объект #%d: %s → %s",
                        f.label,
                        object_id,
                        listing.match_confidence,
                        verdict.confidence,
                    )
                    listing.match_confidence = verdict.confidence
                    changed.add(object_id)
                    stats.regraded += 1
                continue
            log.warning("отцепляю %s от объекта #%d: %s", f.label, object_id, verdict.summary())
            detach(session, listing, cache)
            changed.add(object_id)
            detached.append(listing)
            stats.detached += 1
    session.flush()
    return detached


def _link_pending(
    session: Session,
    cache: FeatureCache,
    pending: list[Listing],
    cfg: MatchingConfig,
    now: datetime,
    stats: ProcessStats,
    changed: set[int],
) -> list[Listing]:
    """Склеить объявления без объекта — по порядку появления: старые основывают объекты
    первыми. Возвращает отложенные."""
    deferred: list[Listing] = []
    cache.preload(pending)
    for listing in sorted({item.id: item for item in pending}.values(), key=_order):
        if listing.object_id is not None:
            continue
        f = cache.get(listing)
        result = evaluate(session, cache, f, cfg, now)
        if result.action == "defer":
            deferred.append(listing)
            continue
        if result.action == "link":
            basis = result.verdict.basis() if result.verdict else None
            link(
                session,
                listing,
                f,
                result.object_id,
                result.confidence,
                basis=basis,
                cache=cache,
            )
            stats.linked[result.confidence] += 1
        else:
            create_object(session, listing, f, cache)
            stats.created += 1
            stats.ambiguous += result.note.startswith("неоднозначно")
        changed.add(listing.object_id)
    session.flush()
    return deferred


def _order(listing: Listing) -> tuple[datetime, int]:
    return listing.first_seen, listing.id
