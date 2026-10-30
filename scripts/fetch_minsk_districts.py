"""Выгрузка границ 9 районов Минска из OpenStreetMap (Overpass API), один запрос.

    python scripts/fetch_minsk_districts.py

Пишет src/minsk_realty/analytics/minsk_districts.json: {«Район»: [кольцо, ...]}, кольцо — список
[lon, lat]. Районы Минска в OSM — отношения boundary=administrative, admin_level=9 (найдено
запросом по тегам: Минск — 4, Минский район — 6, девять районов города — 9). Кольца — внешние и
внутренние вперемешку: point-in-polygon считает чётность пересечений по всем кольцам района.
Геометрия упрощается (Дуглас–Пекер, ~15 м). Одна попытка плюс одна повторная через 30 с.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

from minsk_realty.analytics.fmt import MINSK_DISTRICTS, normalize_district

URL = "https://overpass-api.de/api/interpreter"
QUERY = (
    '[out:json][timeout:90];rel["boundary"="administrative"]["admin_level"="9"]'
    "(53.80,27.35,54.00,27.80);out geom;"
)
OUT = Path(__file__).resolve().parent.parent / "src/minsk_realty/analytics/minsk_districts.json"
TOLERANCE = 0.00015  # градусов, ~15 м по широте


def fetch() -> dict:
    for attempt in (1, 2):
        try:
            resp = httpx.post(
                URL, data={"data": QUERY}, timeout=120, headers={"User-Agent": "minsk-realty/0.1"}
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"попытка {attempt}: {exc}", file=sys.stderr)
            if attempt == 2:
                raise
            time.sleep(30)
    raise AssertionError  # недостижимо


def join_rings(ways: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Склеивает линии участков границы концами в замкнутые кольца."""
    rest, rings = [list(w) for w in ways if len(w) > 1], []
    while rest:
        ring = rest.pop()
        while ring[0] != ring[-1]:
            for i, w in enumerate(rest):
                if w[0] == ring[-1]:
                    ring += w[1:]
                elif w[-1] == ring[-1]:
                    ring += w[::-1][1:]
                else:
                    continue
                del rest[i]
                break
            else:
                raise ValueError("граница района не замыкается")
        rings.append(ring)
    return rings


def simplify(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Дуглас–Пекер для незамкнутой линии (расстояние — в градусах, для нашей точности хватает)."""
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = (dx * dx + dy * dy) ** 0.5
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i][0] - x1, pts[i][1] - y1
        d = abs(px * dy - py * dx) / norm if norm else (px * px + py * py) ** 0.5
        if d > best:
            best, idx = d, i
    if best <= tol:
        return [pts[0], pts[-1]]
    return simplify(pts[: idx + 1], tol)[:-1] + simplify(pts[idx:], tol)


def build(elements: list[dict]) -> dict[str, list[list[list[float]]]]:
    result: dict[str, list[list[list[float]]]] = {}
    for el in elements:
        name = normalize_district(
            el.get("tags", {}).get("name:ru") or el.get("tags", {}).get("name")
        )
        if not name:
            continue
        ways = [
            [(p["lon"], p["lat"]) for p in m["geometry"]]
            for m in el["members"]
            if m["type"] == "way" and m.get("geometry")
        ]
        rings = []
        for ring in join_rings(ways):
            # Кольцо замкнуто: режем по самой дальней от начала точке, чтобы замыкание не терялось.
            far = max(
                range(len(ring)),
                key=lambda i: (ring[i][0] - ring[0][0]) ** 2 + (ring[i][1] - ring[0][1]) ** 2,
            )
            simple = (
                simplify(ring[: far + 1], TOLERANCE)[:-1] + simplify(ring[far:], TOLERANCE)[:-1]
            )
            if len(simple) >= 3:
                rings.append([[round(x, 5), round(y, 5)] for x, y in simple])
        result.setdefault(name, []).extend(rings)
    missing = set(MINSK_DISTRICTS) - set(result)
    if missing:
        raise ValueError(f"не найдены районы: {sorted(missing)}")
    return dict(sorted(result.items()))


def main() -> None:
    districts = build(fetch()["elements"])
    OUT.write_text(
        json.dumps(districts, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(f"районов: {len(districts)}, {OUT.stat().st_size} байт -> {OUT}")


if __name__ == "__main__":
    main()
