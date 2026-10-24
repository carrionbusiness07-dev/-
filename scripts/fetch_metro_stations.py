"""Выгрузка станций минского метро из OpenStreetMap (Overpass API), один запрос.

    python scripts/fetch_metro_stations.py

Пишет src/minsk_realty/analytics/metro_stations.json: [{"name", "lat", "lon"}], по имени.
Узлы с одним именем (платформы, входы) сливаются в одну станцию: координаты усредняются.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import httpx

URL = "https://overpass-api.de/api/interpreter"
QUERY = '[out:json];node["railway"="station"]["station"="subway"](53.80,27.35,54.00,27.80); out;'
OUT = Path(__file__).resolve().parent.parent / "src/minsk_realty/analytics/metro_stations.json"


def merge_by_name(elements: list[dict]) -> list[dict]:
    points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for el in elements:
        name = el.get("tags", {}).get("name")
        if name:
            points[name].append((el["lat"], el["lon"]))
    return [
        {
            "name": name,
            "lat": round(sum(p[0] for p in pts) / len(pts), 6),
            "lon": round(sum(p[1] for p in pts) / len(pts), 6),
        }
        for name, pts in sorted(points.items())
    ]


def main() -> None:
    resp = httpx.post(
        URL, data={"data": QUERY}, timeout=90, headers={"User-Agent": "minsk-realty/0.1"}
    )
    resp.raise_for_status()
    stations = merge_by_name(resp.json()["elements"])
    OUT.write_text(json.dumps(stations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"станций: {len(stations)} -> {OUT}")


if __name__ == "__main__":
    main()
