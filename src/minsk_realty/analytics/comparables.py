"""Цена квартиры против похожих квартир рядом: тот же тип комнатности, эпоха дома и отделка.

Шаги от точного к грубому: 1,5 км (`near`) → 3 км (`wide`) → район (`district`). Берём первый
шаг, где после отсечения выбросов хватает объектов (`min_sample`). Расстояния — по прямой.
Объявленные цены, а не сделки: вывод «дороже/дешевле похожих» — про объявления.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.geo import distance_m
from minsk_realty.analytics.medians import MedianCell, robust_summary, rooms_bucket

STEPS = ("near", "wide")


def epoch(year: int | None, now_year: int) -> str | None:
    """Эпоха постройки: сравниваем хрущёвку с хрущёвкой, а не с новостройкой."""
    if year is None:
        return None
    if year > now_year:
        return "building"
    if year < 1970:
        return "pre1970"
    if year <= 1990:
        return "1970_1990"
    return "1991_2010" if year <= 2010 else "post2010"


def house_key(street_norm: str | None, house: str | None) -> str | None:
    """Ключ дома «улица|дом»; None, если чего-то из двух нет."""
    return f"{street_norm}|{house}" if street_norm and house else None


@dataclass(frozen=True)
class ComparePoint:
    object_id: int
    lat: float
    lon: float
    rooms: int  # корзина комнатности (1, 2, 3, 4 = 4+)
    epoch: str | None
    usd_per_m2: float
    house_key: str | None  # "улица|дом": чтобы не показывать цены соседей по дому как «рядом»
    finish: str | None = None  # "none" (без отделки) / "done" (ремонт) / None (неизвестна)


@dataclass(frozen=True)
class Comparison:
    step: Literal["near", "wide", "district"]
    radius_km: float | None
    epoch: str | None
    rooms: int | None
    n: int  # объектов в сравнении после отсечения выбросов
    median: float
    usd_per_m2: float
    diff_pct: float
    group: Literal["suspicious", "below", "market", "above"]
    year_known: bool
    district: str | None
    points: tuple[ComparePoint, ...]  # кандидаты в радиусе (для карты); для district пусто
    finish: str | None = None  # отделка, по которой отобраны похожие; None — не учитывалась


def price_group(
    diff_pct: float, cfg: AnalyticsConfig = DEFAULT_ANALYTICS, *, new_building: bool = False
) -> str:
    # В новостройке большая скидка к похожим — норма (застройщик, этаж, стадия): не «подозрительно».
    if diff_pct <= cfg.group_suspicious_pct:
        return "below" if new_building else "suspicious"
    if diff_pct <= cfg.group_below_pct:
        return "below"
    return "above" if diff_pct >= cfg.group_above_pct else "market"


def find_comparison(
    points: tuple[ComparePoint, ...],
    *,
    object_id: int | None,
    point: tuple[float, float, bool] | None,
    rooms: int | None,
    year: int | None,
    usd_per_m2: float | None,
    district_cell: MedianCell | None,
    now_year: int,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
    finish: str | None = None,
    new_building: bool = False,
) -> Comparison | None:
    if usd_per_m2 is None:
        return None
    bucket = rooms_bucket(rooms)
    target_epoch = epoch(year, now_year)

    def result(
        step, radius_km, n, median, used_epoch, used_rooms, district, shown, used_finish=None
    ):
        diff = (usd_per_m2 / median - 1) * 100
        shown_diff = float(f"{diff:.1f}" if abs(diff) < 10 else f"{diff:.0f}")  # как в fmt.pct
        return Comparison(
            step, radius_km, used_epoch, used_rooms, n, median, usd_per_m2, diff,
            price_group(shown_diff, cfg, new_building=new_building), year is not None, district, shown, used_finish,
        )  # fmt: skip

    # Неточная координата (центр улицы) для радиуса 1,5 км не годится — сразу район.
    if point is not None and not point[2] and bucket is not None:
        lat, lon, _ = point
        near = [
            (distance_m(lat, lon, p.lat, p.lon), p)
            for p in points
            if p.rooms == bucket
            and (target_epoch is None or p.epoch == target_epoch)
            and (finish is None or p.finish == finish)
            and p.object_id != object_id
        ]
        for step, radius_km in zip(STEPS, cfg.compare_radii_km, strict=True):
            inside = tuple(p for d, p in near if d <= radius_km * 1000)
            s = robust_summary(
                (p.usd_per_m2 for p in inside),
                bounds=cfg.ppm_bounds_usd,
                iqr_k=cfg.iqr_k,
                min_sample=cfg.min_sample,
            )
            if s.median is not None:
                return result(
                    step,
                    radius_km,
                    len(s.kept),
                    s.median,
                    target_epoch,
                    bucket,
                    None,
                    inside,
                    finish,
                )

    if district_cell is not None and district_cell.enough:
        return result(
            "district", None, district_cell.n, district_cell.median, None, district_cell.rooms,
            district_cell.district, (),
        )  # fmt: skip
    return None
