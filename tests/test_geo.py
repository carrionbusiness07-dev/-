"""Проверка координат площадок и данные станций метро (без сети и без БД)."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    # scripts/ — не пакет: грузим файл по пути.
    spec = importlib.util.spec_from_file_location(
        "check_coords", ROOT / "scripts" / "check_coords.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_approx_share_flags_shared_points():
    rows = [
        ("kufar", 53.9, 27.5, "Притыцкого", "1"),
        ("kufar", 53.9, 27.5, "Притыцкого", "3"),
        ("kufar", 53.9, 27.5, "Притыцкого", "5"),
        ("realt", 53.95, 27.6, "Ванеева", "10"),
    ]
    assert _load_script().approx_share(rows) == {"kufar": 1.0, "realt": 0.0}


def test_approx_share_ignores_missing_and_respelled_addresses():
    rows = [
        ("kufar", 53.9, 27.5, "Притыцкого", "5"),
        ("kufar", 53.9, 27.5, "Притыцкого", None),  # нет дома — не «другой дом»
        ("kufar", 53.9, 27.5, None, "5"),  # нет улицы — тоже
        ("realt", 53.95, 27.6, "ул. Притыцкого", "5"),
        ("realt", 53.95, 27.6, "Притыцкого", "д. 5"),  # то же самое, записанное иначе
    ]
    assert _load_script().approx_share(rows) == {"kufar": 0.0, "realt": 0.0}


def test_metro_stations_file():
    path = ROOT / "src" / "minsk_realty" / "analytics" / "metro_stations.json"
    stations = json.loads(path.read_text(encoding="utf-8"))
    names = [s["name"] for s in stations]
    assert len(stations) >= 25
    assert len(set(names)) == len(names)
    assert not any(ch in name for name in names for ch in "іўІЎ")
    assert all(53.80 <= s["lat"] <= 54.00 and 27.35 <= s["lon"] <= 27.80 for s in stations)


# --- analytics/geo.py ---
from minsk_realty.analytics import geo

A = geo.Station("А", 53.90, 27.50)
B = geo.Station("Б", 53.90, 27.60)


def test_distance_known_pair():
    assert abs(geo.distance_m(53.9, 27.5, 53.9, 27.6) - 6560) < 65


def test_walk_minutes():
    assert geo.walk_minutes(600) == 10
    assert geo.walk_minutes(100) == 5
    assert geo.walk_minutes(1500) == 25


def test_locate_nearest_and_far():
    near_b = geo.locate(53.90, 27.595, approx=False, stations=(A, B))
    assert near_b.station == B
    assert near_b.walk_min == geo.walk_minutes(near_b.station_m)
    far = geo.locate(53.99, 27.55, approx=False, stations=(A, B))  # ~10 км от обеих
    assert far.station is None and far.station_m is None and far.walk_min is None
    edge = geo.locate(53.93, 27.50, approx=False, stations=(A, B))  # ~3,3 км
    assert edge.station is None


def test_locate_approx_has_no_minutes():
    loc = geo.locate(53.90, 27.495, approx=True, stations=(A, B))
    assert loc.station == A and loc.station_m > 0 and loc.walk_min is None


def test_locate_rejects_garbage():
    for lat, lon in [(0, 0), (27.5, 53.9), (None, 27.5), (53.9, None)]:
        assert geo.locate(lat, lon, approx=False, stations=(A, B)) is None


def test_load_stations_reads_json():
    stations = geo.load_stations()
    assert len(stations) >= 25 and all(geo.valid_point(s.lat, s.lon) for s in stations)
