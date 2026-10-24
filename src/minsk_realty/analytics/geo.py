"""Расстояния и ближайшее метро по координатам объекта.

Расстояние — по прямой (гаверсинус). Пешком идут не по прямой, а по улицам, поэтому
время считаем по прямой ×1.3 при 83 м/мин (5 км/ч), округляя до 5 минут.
Метро дальше `MAX_STATION_M` не показываем: пешком это уже не «рядом с метро».
Приблизительные координаты (общая точка на весь двор или улицу) дают станцию,
но не минуты: точность точки меньше точности минут.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

EARTH_R_M = 6_371_000
WALK_DETOUR = 1.3
WALK_M_PER_MIN = 83
MAX_STATION_M = 3000
# Рамка Минска включительно: отсекает нули и перепутанные lat/lon.
LAT_RANGE = (53.80, 54.00)
LON_RANGE = (27.35, 27.80)


@dataclass(frozen=True)
class Station:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    approx: bool
    station: Station | None  # None — метро дальше MAX_STATION_M
    station_m: float | None
    walk_min: int | None  # None, если метро далеко или координата приблизительная


@cache
def load_stations() -> tuple[Station, ...]:
    raw = (files("minsk_realty.analytics") / "metro_stations.json").read_text(encoding="utf-8")
    return tuple(Station(s["name"], s["lat"], s["lon"]) for s in json.loads(raw))


def valid_point(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    return LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def walk_minutes(distance_m: float) -> int:
    minutes = distance_m * WALK_DETOUR / WALK_M_PER_MIN
    return max(5, math.floor(minutes / 5 + 0.5) * 5)


def locate(
    lat: float | None,
    lon: float | None,
    *,
    approx: bool,
    stations: tuple[Station, ...] | None = None,
) -> Location | None:
    if not valid_point(lat, lon):
        return None
    best, best_m = None, None
    for s in stations if stations is not None else load_stations():
        d = distance_m(lat, lon, s.lat, s.lon)
        if best_m is None or d < best_m:
            best, best_m = s, d
    if best_m is None or best_m > MAX_STATION_M:
        return Location(lat, lon, approx, None, None, None)
    return Location(lat, lon, approx, best, best_m, None if approx else walk_minutes(best_m))
