"""Медианы $/м² и срока экспозиции по районам и комнатности.

Точка — объект, а не объявление: одна квартира у трёх агентств — одна точка.
Цена объекта — `MarketData.object_price` (минимальная последняя цена среди активных надёжных
объявлений, в USD). Площадь — из того же снимка, иначе из объекта.

Отсечение выбросов, по шагам:
1. грубые границы: $/м² в `ppm_bounds_usd`, площадь в `area_bounds_m2`;
2. Тьюки: вне [Q1 − 1.5·IQR, Q3 + 1.5·IQR] (квартили — `statistics.quantiles(method=
   "inclusive")`, то же, что `percentile_cont` в PostgreSQL), если в группе ≥ 4 точек.
Медиана показывается, только если после отсечения осталось ≥ `min_sample` (10) объектов.

Это цены объявлений, а не сделок. Ориентир по сделкам — статистика НКА (realt.by/statistics).

Микрорайонов в БД пока нет (`objects.microdistrict` никто не заполняет), поэтому самый мелкий
уровень — район. Район есть только у объектов, где хоть одно объявление пришло с Kufar.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import MarketData
from minsk_realty.analytics.exposure import compute_exposure

if TYPE_CHECKING:
    from minsk_realty.analytics.comparables import ComparePoint

RoomsKey = int | None  # 1, 2, 3, 4 (= 4+), None — все


@dataclass(frozen=True)
class MedianCell:
    district: str | None  # None — весь Минск
    rooms: RoomsKey
    n: int  # точек после отсечения выбросов
    n_raw: int  # точек до отсечения
    median: float | None  # None — мало данных (n < min_sample)
    q1: float | None = None
    q3: float | None = None

    @property
    def enough(self) -> bool:
        return self.median is not None


@dataclass(frozen=True)
class Summary:
    n_raw: int
    kept: list[float]
    median: float | None
    q1: float | None
    q3: float | None


def robust_summary(
    values: Iterable[float],
    *,
    bounds: tuple[float, float] | None = None,
    iqr_k: float = DEFAULT_ANALYTICS.iqr_k,
    min_sample: int = DEFAULT_ANALYTICS.min_sample,
) -> Summary:
    raw = [float(v) for v in values]
    vals = [v for v in raw if bounds is None or bounds[0] <= v <= bounds[1]]
    if len(vals) >= 4:
        q1, _, q3 = statistics.quantiles(vals, n=4, method="inclusive")
        lo, hi = q1 - iqr_k * (q3 - q1), q3 + iqr_k * (q3 - q1)
        vals = [v for v in vals if lo <= v <= hi]
    vals.sort()
    if len(vals) < max(min_sample, 1):
        return Summary(len(raw), vals, None, None, None)
    if len(vals) >= 2:
        q1, median, q3 = statistics.quantiles(vals, n=4, method="inclusive")
    else:
        q1 = median = q3 = vals[0]
    return Summary(len(raw), vals, median, q1, q3)


def rooms_bucket(rooms: int | None) -> int | None:
    if not rooms or rooms <= 0:
        return None
    return min(rooms, 4)


@dataclass(frozen=True)
class MarketBaseline:
    """Медианы рынка на момент `at`. Считается целиком по БД — кэшируйте на час-другой."""

    at: datetime
    observation_start: datetime | None
    price_cells: tuple[MedianCell, ...]  # $/м²
    exposure_cells: tuple[MedianCell, ...]  # дни на рынке, rooms=None
    active_objects: int
    # Доля активных объектов со сроком «как минимум» (застали уже висящими).
    exposure_truncated_share: float | None
    min_sample: int
    # Активные объекты с ценой, площадью и координатами — для сравнения с похожими квартирами.
    points: tuple[ComparePoint, ...] = ()

    def price_cell(self, district: str | None, rooms: RoomsKey) -> MedianCell | None:
        return _find(self.price_cells, district, rooms)

    def exposure_cell(self, district: str | None) -> MedianCell | None:
        return _find(self.exposure_cells, district, None)

    def price_for(self, district: str | None, rooms: int | None) -> MedianCell | None:
        """Самая точная группа, где хватает данных: район+комнаты → район → Минск+комнаты →
        Минск. Если данных не хватает нигде — самая точная группа (с median=None)."""
        bucket = rooms_bucket(rooms)
        order = [(district, bucket), (district, None)] if district else []
        order += [(None, bucket), (None, None)]
        cells = [c for d, r in dict.fromkeys(order) if (c := self.price_cell(d, r)) is not None]
        for cell in cells:
            if cell.enough:
                return cell
        return cells[0] if cells else None

    def exposure_for(self, district: str | None) -> MedianCell | None:
        for cell in (self.exposure_cell(district), self.exposure_cell(None)):
            if cell is not None and cell.enough:
                return cell
        return self.exposure_cell(district) or self.exposure_cell(None)

    def districts(self) -> list[MedianCell]:
        """Районы (все комнаты), по убыванию медианы; районы без данных — в конце."""
        cells = [c for c in self.price_cells if c.district is not None and c.rooms is None]
        return sorted(cells, key=lambda c: (c.median is None, -(c.median or 0), c.district))

    def city(self) -> MedianCell | None:
        return self.price_cell(None, None)


def _find(cells: tuple[MedianCell, ...], district: str | None, rooms: RoomsKey):
    return next((c for c in cells if c.district == district and c.rooms == rooms), None)


def compute_baseline(
    data: MarketData, at: datetime, *, cfg: AnalyticsConfig = DEFAULT_ANALYTICS
) -> MarketBaseline:
    ppm_groups: dict[tuple[str | None, RoomsKey], list[float]] = defaultdict(list)
    exp_groups: dict[str | None, list[float]] = defaultdict(list)
    from minsk_realty.analytics.comparables import ComparePoint, epoch, house_key  # цикл импортов

    points: list[ComparePoint] = []
    active = truncated = 0
    lo_area, hi_area = cfg.area_bounds_m2
    for object_id in data.objects:
        if not data.object_active_at(object_id, at):
            continue
        active += 1
        district = data.object_district(object_id)

        listings = [r for r in data.object_listings(object_id) if r.first_seen <= at]
        events = [e for e in data.object_events(object_id) if e.at <= at]
        exposure = compute_exposure(listings, events, at, data.source_starts, cfg=cfg)
        if exposure is not None:
            truncated += exposure.at_least
            for key in {district, None}:
                exp_groups[key].append(exposure.days)

        price = data.object_price(object_id, at)
        if price is None or price.area is None or not lo_area <= price.area <= hi_area:
            continue
        value = price.usd_per_m2
        bucket = rooms_bucket(price.rooms or data.object_rooms(object_id))
        for d in {district, None}:
            for r in {bucket, None}:
                ppm_groups[(d, r)].append(value)
        # Неточные координаты тоже годятся: для радиуса 1,5 км точность «до улицы» достаточна.
        coords = data.object_point(object_id, cfg.approx_coord_sources)
        if bucket is not None and coords is not None:
            obj = data.objects.get(object_id)
            key = house_key(obj.street_norm, obj.house) if obj else None
            points.append(
                ComparePoint(
                    object_id, coords[0], coords[1], bucket,
                    epoch(data.building_years.get(object_id), at.year), value, key,
                )
            )  # fmt: skip

    def cell(d, r, values, bounds):
        s = robust_summary(values, bounds=bounds, iqr_k=cfg.iqr_k, min_sample=cfg.min_sample)
        return MedianCell(d, r, len(s.kept), s.n_raw, s.median, s.q1, s.q3)

    price_cells = tuple(
        cell(d, r, vals, cfg.ppm_bounds_usd) for (d, r), vals in sorted(ppm_groups.items(), key=_k)
    )
    # Срок экспозиции — без отсечения выбросов: долгие объекты и есть то, что мы ищем.
    exposure_cells = tuple(
        MedianCell(d, None, len(vals), len(vals), *_plain(vals, cfg.min_sample))
        for d, vals in sorted(exp_groups.items(), key=lambda kv: kv[0] or "")
    )
    return MarketBaseline(
        at=at,
        observation_start=data.observation_start,
        price_cells=price_cells,
        exposure_cells=exposure_cells,
        active_objects=active,
        exposure_truncated_share=truncated / active if active else None,
        min_sample=cfg.min_sample,
        points=tuple(points),
    )


def _plain(values: list[float], min_sample: int) -> tuple[float | None, float | None, float | None]:
    if len(values) < max(min_sample, 1):
        return None, None, None
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return median, q1, q3


def _k(item):
    (d, r), _ = item
    return (d or "", r or 0)
