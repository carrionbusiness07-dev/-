"""Отчёт /check как структура данных. Текст — отдельно, в `render.py`.

    report = build_check_report(session, "https://re.kufar.by/vi/1000001", now)
    text = render_check_text(report)

Чьи данные в отчёте (`scope`):
- "object"  — объявление надёжно (high/medium) привязано к объекту: срок, история цены,
              продавцы — по всем надёжным объявлениям объекта;
- "listing" — склейка low или объекта ещё нет: всё считаем по самому объявлению, а объект
              показываем отдельно как «возможно, та же квартира» (`possible_object`).
Объявления со склейкой low в выводах (дешевле на другой площадке, продавцы, срок) не
участвуют, в списке «на других площадках» помечаются как «возможно».

Медианы рынка считаются по всей БД — передайте `baseline` (кэш `compute_baseline`), чтобы
отчёт строился быстро.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from typing import Literal

from sqlalchemy.orm import Session

from minsk_realty.analytics.assessment import Assessment, assess, same_house
from minsk_realty.analytics.bargaining import (
    Bargaining,
    BargainInputs,
    CheaperOffer,
    bargaining_index,
)
from minsk_realty.analytics.comparables import Comparison, find_comparison, house_key
from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import ListingRow, MarketData, load_market, source_starts
from minsk_realty.analytics.exposure import Exposure, compute_exposure
from minsk_realty.analytics.fmt import normalize_district, plural, street_label
from minsk_realty.analytics.geo import Location, locate
from minsk_realty.analytics.history import PriceMoves, PricePoint, build_price_history, price_moves
from minsk_realty.analytics.medians import MarketBaseline, MedianCell, compute_baseline
from minsk_realty.db.models import Listing
from minsk_realty.db.repository import find_listing
from minsk_realty.matching.address import normalize_street
from minsk_realty.matching.config import DEFAULT_CONFIG
from minsk_realty.matching.features import FeatureCache
from minsk_realty.matching.matcher import linked_features
from minsk_realty.matching.rules import (
    CONFIDENCE_RANK,
    LOC_ADDRESS,
    LOC_FINGERPRINT,
    LOC_GEO,
    compare,
)
from minsk_realty.sources import ADAPTERS, url_adapters

ReportStatus = Literal["ok", "not_found", "unsupported_url"]
Scope = Literal["object", "listing"]
DataLevel = Literal["first_time", "short", "full"]

SUPPORTED_SOURCES = tuple(ADAPTERS)
# Ниже этого расхождения в USD цены в разных валютах не сравниваем: курсы площадок разные.
CROSS_CURRENCY_MIN_PCT = Decimal(2)
# История «короткая», если площадку наблюдаем меньше этого и объект застали уже висящим.
SHORT_OBSERVATION_DAYS = 14


@dataclass(frozen=True)
class CurrentPrice:
    amount: Decimal
    currency: str
    usd: Decimal | None
    usd_per_m2: float | None
    seen_at: datetime


@dataclass(frozen=True)
class ListingInfo:
    listing_id: int
    source: str
    external_id: str
    url: str
    status: str
    seller_type: str
    agency_name: str | None
    first_seen: datetime
    last_seen: datetime
    posted_at: datetime | None
    confidence: str | None
    price: CurrentPrice | None


@dataclass(frozen=True)
class FlatInfo:
    rooms: int | None
    area: float | None
    floor: int | None
    floors_total: int | None
    street: str | None  # для показа: «ул. Притыцкого» (без номера дома)
    district: str | None  # «Фрунзенский»
    year: int | None = None  # год постройки; для scope="listing" не подставляем
    house_key: str | None = None  # «улица|дом» как у ComparePoint.house_key; для "listing" None


@dataclass(frozen=True)
class MatchInfo:
    # Уверенность склейки, которая стоит за историей: для scope="object" — самая слабая из
    # надёжных связей объекта; для "listing" — уверенность самого объявления (low или None).
    level: str | None
    single: bool  # у объекта одно надёжное объявление: склеек нет (high ≠ «подтверждено фото»)
    basis: str | None  # «адрес + площадь + 4 совпавших фото»
    target_confidence: str | None
    low_listings: int  # объявлений объекта со склейкой low (возможные дубли)


@dataclass(frozen=True)
class OtherListing:
    listing_id: int
    source: str
    url: str
    seller_type: str
    agency_name: str | None
    amount: Decimal | None
    currency: str | None
    usd: Decimal | None
    confidence: str | None
    other_seller: bool
    # Дешевле текущего объявления на столько (>0) или дороже (<0); None — не сравнить.
    cheaper_by: Decimal | None
    cheaper_currency: str | None


@dataclass(frozen=True)
class SellersInfo:
    count: int  # разных продавцов за всю историю объекта
    owners: int
    agencies: int


@dataclass(frozen=True)
class MedianComparison:
    cell: MedianCell
    usd_per_m2: float | None
    diff_pct: float | None  # (цена за м² / медиана − 1) · 100


@dataclass(frozen=True)
class PossibleObject:
    """Объект, к которому объявление склеено слабо (low): «возможно, та же квартира»."""

    object_id: int
    basis: str | None
    exposure: Exposure | None
    drops: int
    sources: tuple[str, ...]
    min_price: Decimal | None
    min_price_currency: str | None


@dataclass(frozen=True)
class CheckReport:
    status: ReportStatus
    query: str
    generated_at: datetime
    observation_start: datetime | None
    source: str | None = None  # площадка, если ссылку удалось разобрать
    listing: ListingInfo | None = None
    object_id: int | None = None
    scope: Scope = "listing"
    data_level: DataLevel = "first_time"
    flat: FlatInfo | None = None
    match: MatchInfo | None = None
    exposure: Exposure | None = None
    history: tuple[PricePoint, ...] = ()
    moves: PriceMoves | None = None
    others: tuple[OtherListing, ...] = ()
    possible_object: PossibleObject | None = None
    sellers: SellersInfo | None = None
    median: MedianComparison | None = None
    exposure_median: MedianCell | None = None
    bargaining: Bargaining | None = None
    location: Location | None = None
    comparison: Comparison | None = None
    assessment: Assessment | None = None
    same_house: tuple[float, ...] = ()  # $/м² других объектов в том же доме, по возрастанию
    # Подчеркнуть кнопку «Следить»: истории мало или объявление ещё не в базе.
    suggest_watch: bool = True


# --- Разбор ссылки ------------------------------------------------------------------


@cache
def _adapters() -> tuple:
    # Адаптеры создают HTTP-клиент, но resolve_url в сеть не ходит. Один экземпляр на процесс.
    return url_adapters()


def resolve_ref(session: Session, ref: int | str) -> tuple[Listing | None, str | None, bool]:
    """→ (объявление или None, площадка, ссылка понята). `ref`: id объявления (int или строка
    из цифр), "listing:<id>" или ссылка на объявление любой собираемой площадки."""
    if isinstance(ref, int):
        listing = session.get(Listing, ref)
        return listing, listing.source if listing else None, True
    text = ref.strip()
    number = text.removeprefix("listing:")
    if number.isdigit():
        listing = session.get(Listing, int(number))
        return listing, listing.source if listing else None, True
    for adapter in _adapters():
        external_id = adapter.resolve_url(text)
        if external_id:
            return find_listing(session, adapter.source, external_id), adapter.source, True
    return None, None, False


# --- Сборка отчёта ----------------------------------------------------------------------


def build_check_report(
    session: Session,
    url_or_listing_id: int | str,
    now: datetime | None = None,
    *,
    baseline: MarketBaseline | None = None,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
) -> CheckReport:
    now = now or datetime.now(UTC)
    listing, source, understood = resolve_ref(session, url_or_listing_id)
    query = str(url_or_listing_id)
    if listing is None:
        starts = source_starts(session)
        return CheckReport(
            status="not_found" if understood else "unsupported_url",
            query=query,
            generated_at=now,
            observation_start=min(starts.values()) if starts else None,
            source=source,
        )

    oid = listing.object_id
    data = load_market(session, object_ids=[oid] if oid else [], listing_ids=[listing.id])
    if baseline is None:
        baseline = compute_baseline(load_market(session), now, cfg=cfg)
    target = data.listings[listing.id]
    trusted = data.object_listings(oid) if oid else []
    scope: Scope = "object" if oid and target.trusted else "listing"
    included = trusted if scope == "object" else [target]
    all_events = data.object_events(oid, trusted_only=False) if oid else []
    ids = {r.id for r in included}
    if scope == "object":
        events = [e for e in all_events if e.trusted]
    else:
        events = [e for e in all_events if e.listing_id == target.id]

    exposure = compute_exposure(
        included,
        events if scope == "object" else [],
        now,
        data.source_starts,
        current=target,
        cfg=cfg,
    )
    history = build_price_history(included, data.snapshots, events, current_listing_id=target.id)
    moves = price_moves(events, ids, history)

    current = _current_price(data, target)
    # При склейке low объект может оказаться другой квартирой: его поля не подставляем.
    flat = _flat_info(data, oid if scope == "object" else None, target)
    others = _others(data, oid, target, current) if scope == "object" else []
    cheaper = _cheapest_other(others, current, cfg)

    median_cell = baseline.price_for(flat.district, flat.rooms)
    median = None
    if median_cell is not None:
        ppm = current.usd_per_m2 if current else None
        diff = (
            (ppm / median_cell.median - 1) * 100 if ppm is not None and median_cell.median else None
        )
        median = MedianComparison(median_cell, ppm, diff)
    exposure_cell = baseline.exposure_for(flat.district)

    point = (
        data.object_point(oid, cfg.approx_coord_sources)
        if scope == "object"
        else data.listing_point(target.id, cfg.approx_coord_sources)
    )
    location = locate(point[0], point[1], approx=point[2]) if point else None
    # Объект исключается из своей выборки и при склейке low (год и дом для него не берём).
    comparison = find_comparison(
        baseline.points,
        object_id=oid,
        point=point,
        rooms=flat.rooms,
        year=flat.year,
        usd_per_m2=current.usd_per_m2 if current else None,
        district_cell=median_cell,
        now_year=now.year,
        cfg=cfg,
    )
    similar = comparison is not None and comparison.step in ("near", "wide")
    if comparison is not None:
        vs_median = comparison.diff_pct
    else:
        vs_median = median.diff_pct if median and median.cell.enough else None

    data_level = _data_level(exposure, moves, included, others, now)
    has_trusted_others = any(o.confidence in ("high", "medium") for o in others)
    bargaining = bargaining_index(
        BargainInputs(
            has_history=data_level != "first_time" or has_trusted_others,
            days_on_market=exposure.days if exposure else None,
            at_least=exposure.at_least if exposure else False,
            exposure_median_days=exposure_cell.median if exposure_cell else None,
            exposure_scope=_scope_word(exposure_cell, "по району", "по Минску"),
            drops=moves.drops,
            ups=moves.ups,
            relists=(exposure.relist_count + exposure.returned_new_listing) if exposure else 0,
            cheaper=cheaper,
            vs_median_pct=vs_median,
            median_scope=(
                "похожих квартир" if similar else _scope_word(median_cell, "района", "по Минску")
            ),
            current_amount=current.amount if current else None,
            currency=current.currency if current else None,
        ),
        cfg,
    )
    assessment = assess(
        comparison=comparison,
        bargaining_level=bargaining.level,
        rooms=flat.rooms,
        area=flat.area,
        floor=flat.floor,
        floors_total=flat.floors_total,
        year=flat.year,
        now_year=now.year,
        location=location,
    )

    possible = None
    if scope == "listing" and oid and trusted:
        possible = _possible_object(session, data, oid, target, now, cfg)

    return CheckReport(
        status="ok",
        query=query,
        generated_at=now,
        observation_start=data.observation_start,
        source=target.source,
        listing=ListingInfo(
            listing_id=target.id,
            source=target.source,
            external_id=target.external_id,
            url=target.url,
            status=target.status,
            seller_type=target.seller_type,
            agency_name=target.agency_name,
            first_seen=target.first_seen,
            last_seen=target.last_seen,
            posted_at=target.posted_at,
            confidence=target.confidence,
            price=current,
        ),
        object_id=oid,
        scope=scope,
        data_level=data_level,
        flat=flat,
        match=_match_info(session, data, oid, target, trusted, now),
        exposure=exposure,
        history=tuple(history),
        moves=moves,
        others=tuple(others),
        possible_object=possible,
        sellers=_sellers(included),
        median=median,
        exposure_median=exposure_cell,
        bargaining=bargaining,
        location=location,
        comparison=comparison,
        assessment=assessment,
        same_house=same_house(baseline.points, object_id=oid, house_key=flat.house_key),
        suggest_watch=data_level != "full" or bargaining.level == "unknown",
    )


def _scope_word(cell: MedianCell | None, district_word: str, city_word: str) -> str:
    return district_word if cell is not None and cell.district else city_word


def _current_price(data: MarketData, target: ListingRow) -> CurrentPrice | None:
    snap = data.last_snapshot(target.id, priced=True)
    if snap is None:
        return None
    area = snap.area
    if area is None and target.object_id in data.objects:
        area = data.objects[target.object_id].area
    usd = snap.price_usd
    return CurrentPrice(
        amount=snap.amount,
        currency=snap.currency,
        usd=usd,
        usd_per_m2=float(usd) / area if usd is not None and area else None,
        seen_at=snap.seen_at,
    )


def _flat_info(data: MarketData, oid: int | None, target: ListingRow) -> FlatInfo:
    obj = data.objects.get(oid) if oid else None
    snap = data.last_snapshot(target.id)
    district = normalize_district(snap.district) if snap else None
    if district is None and oid:
        district = data.object_district(oid)
    street = street_label(obj.street_norm) if obj else None
    if street is None and snap and snap.street:
        parsed = normalize_street(snap.street)
        street = street_label(str(parsed)) if parsed else snap.street
    return FlatInfo(
        rooms=(snap.rooms if snap and snap.rooms else obj.rooms if obj else None),
        area=(snap.area if snap and snap.area else obj.area if obj else None),
        floor=(snap.floor if snap and snap.floor is not None else obj.floor if obj else None),
        floors_total=(
            snap.floors_total if snap and snap.floors_total else obj.floors_total if obj else None
        ),
        street=street,
        district=district,
        year=data.building_years.get(oid) if oid else None,
        house_key=house_key(obj.street_norm, obj.house) if obj else None,
    )


def _others(
    data: MarketData, oid: int | None, target: ListingRow, current: CurrentPrice | None
) -> list[OtherListing]:
    if not oid:
        return []
    out = []
    for row in data.object_listings(oid, trusted_only=False):
        if row.id == target.id or not row.active:
            continue
        snap = data.last_snapshot(row.id, priced=True)
        cheaper_by = cheaper_currency = None
        if snap is not None and current is not None:
            if snap.currency == current.currency:
                cheaper_by, cheaper_currency = current.amount - snap.amount, current.currency
            elif snap.price_usd is not None and current.usd is not None:
                diff = current.usd - snap.price_usd
                if abs(diff) / current.usd * 100 >= CROSS_CURRENCY_MIN_PCT:
                    cheaper_by, cheaper_currency = diff, "USD"
        out.append(
            OtherListing(
                listing_id=row.id,
                source=row.source,
                url=row.url,
                seller_type=row.seller_type,
                agency_name=row.agency_name,
                amount=snap.amount if snap else None,
                currency=snap.currency if snap else None,
                usd=snap.price_usd if snap else None,
                confidence=row.confidence,
                other_seller=row.seller_id != target.seller_id,
                cheaper_by=cheaper_by,
                cheaper_currency=cheaper_currency,
            )
        )
    out.sort(key=lambda o: (o.confidence not in ("high", "medium"), o.usd or Decimal(10**12)))
    return out


def _cheapest_other(
    others: list[OtherListing], current: CurrentPrice | None, cfg: AnalyticsConfig
) -> CheaperOffer | None:
    if current is None:
        return None
    best = None
    for o in others:
        if o.confidence not in ("high", "medium") or o.cheaper_by is None or o.cheaper_by <= 0:
            continue
        base = current.amount if o.cheaper_currency == current.currency else current.usd
        if not base or o.cheaper_by / base * 100 < Decimal(str(cfg.cheaper_min_pct)):
            continue
        if best is None or o.cheaper_by > best.cheaper_by:
            best = o
    if best is None:
        return None
    same = best.cheaper_currency == best.currency
    return CheaperOffer(
        listing_id=best.listing_id,
        source=best.source,
        amount=best.amount if same else best.usd,
        currency=best.currency if same else "USD",
        diff=best.cheaper_by,
        url=best.url,
        other_seller=best.other_seller,
    )


def _data_level(
    exposure: Exposure | None,
    moves: PriceMoves,
    included: list[ListingRow],
    others: list[OtherListing],
    now: datetime,
) -> DataLevel:
    if exposure is None:
        return "first_time"
    nothing_else = len(included) == 1 and not moves.drops and not moves.ups and not others
    if exposure.days < 2 and nothing_else:
        return "first_time"
    observed = (now - exposure.observed_since).days if exposure.observed_since else 0
    if exposure.at_least and observed < SHORT_OBSERVATION_DAYS:
        return "short"
    return "full"


def _sellers(included: list[ListingRow]) -> SellersInfo:
    sellers: dict[str, str] = {}
    for row in included:
        sellers.setdefault(row.seller_id, row.seller_type)
    types = list(sellers.values())
    return SellersInfo(
        count=len(sellers), owners=types.count("owner"), agencies=types.count("agency")
    )


# --- Уверенность склейки ------------------------------------------------------------------


def _basis(session: Session, cache: FeatureCache, listing: Listing, now: datetime) -> str | None:
    """Почему объявление в этом объекте — пересчёт правил Emma по текущим данным."""
    if listing.object_id is None:
        return None
    f = cache.get(listing)
    trusted, weak = linked_features(session, cache, listing.object_id, exclude=listing.id)
    if not trusted:
        return None
    verdict = compare(f, listing.object_id, trusted, DEFAULT_CONFIG, now, weak=weak)
    if verdict.outcome != "match":
        return None
    parts = {
        LOC_FINGERPRINT: ["адрес", "этаж", "площадь"],
        LOC_ADDRESS: ["адрес", "площадь"],
        LOC_GEO: ["координаты", "площадь"],
    }.get(verdict.location, ["комнаты", "площадь"])
    n = verdict.photo_matches
    if verdict.photo_ok:
        parts.append(f"{n} {plural(n, 'совпавшее фото', 'совпавших фото', 'совпавших фото')}")
    if any("перевыложил" in r for r in verdict.reasons):
        parts.append("тот же продавец")
    text = " + ".join(parts)
    if not verdict.photo_ok:
        text += ", фото не подтверждают"
    return text


def _match_info(
    session: Session,
    data: MarketData,
    oid: int | None,
    target: ListingRow,
    trusted: list[ListingRow],
    now: datetime,
) -> MatchInfo:
    if not oid:
        return MatchInfo(None, False, None, None, 0)
    all_rows = data.object_listings(oid, trusted_only=False)
    low = sum(1 for r in all_rows if r.confidence == "low")
    cache = FeatureCache(session)
    if not target.trusted:
        basis = _basis(session, cache, session.get(Listing, target.id), now)
        return MatchInfo(target.confidence, False, basis, target.confidence, low)
    if len(trusted) <= 1:
        return MatchInfo("high", True, None, target.confidence, low)
    # Основное объявление (первое) ни с чем не сравнивалось — смотрим на остальные связи.
    links = trusted[1:]
    weakest = min(
        links,
        key=lambda r: (CONFIDENCE_RANK.get(r.confidence or "", 0), r.id != target.id, r.id),
    )
    basis = _basis(session, cache, session.get(Listing, weakest.id), now)
    return MatchInfo(weakest.confidence, False, basis, target.confidence, low)


def _possible_object(
    session: Session,
    data: MarketData,
    oid: int,
    target: ListingRow,
    now: datetime,
    cfg: AnalyticsConfig,
) -> PossibleObject:
    trusted = data.object_listings(oid)
    events = data.object_events(oid)
    exposure = compute_exposure(trusted, events, now, data.source_starts, cfg=cfg)
    drops = sum(1 for e in events if e.type == "price_drop")
    prices = [
        s for r in trusted if r.active and (s := data.last_snapshot(r.id, priced=True)) is not None
    ]
    cheapest = min(prices, key=lambda s: s.price_usd or s.amount, default=None)
    return PossibleObject(
        object_id=oid,
        basis=_basis(session, FeatureCache(session), session.get(Listing, target.id), now),
        exposure=exposure,
        drops=drops,
        sources=tuple(sorted({r.source for r in trusted})),
        min_price=cheapest.amount if cheapest else None,
        min_price_currency=cheapest.currency if cheapest else None,
    )
