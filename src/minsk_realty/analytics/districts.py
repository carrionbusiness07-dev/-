"""Район Минска по координатам: запасной вариант, когда площадка район не отдаёт (Realt, Onliner).

Границы — `minsk_districts.json` (OpenStreetMap, admin_level=9, см. scripts/fetch_minsk_districts.py).
Кольца района — внешние и внутренние вперемешку: чётность пересечений луча по всем кольцам даёт
верный ответ и для дыр, и для мультиполигонов.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files

Ring = tuple[tuple[float, float], ...]  # (lon, lat)


@cache
def load_districts() -> dict[str, tuple[Ring, ...]]:
    raw = (files("minsk_realty.analytics") / "minsk_districts.json").read_text(encoding="utf-8")
    return {
        name: tuple(tuple((x, y) for x, y in ring) for ring in rings)
        for name, rings in json.loads(raw).items()
    }


def _inside(lon: float, lat: float, rings: tuple[Ring, ...]) -> bool:
    odd = False
    for ring in rings:
        x1, y1 = ring[-1]
        for x2, y2 in ring:
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                odd = not odd
            x1, y1 = x2, y2
    return odd


def district_at(lat: float | None, lon: float | None) -> str | None:
    """Район по точке; None — вне Минска (или точка не задана)."""
    if lat is None or lon is None:
        return None
    return next((n for n, rings in load_districts().items() if _inside(lon, lat, rings)), None)
