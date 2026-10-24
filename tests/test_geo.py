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


def test_metro_stations_file():
    path = ROOT / "src" / "minsk_realty" / "analytics" / "metro_stations.json"
    stations = json.loads(path.read_text(encoding="utf-8"))
    names = [s["name"] for s in stations]
    assert len(stations) >= 25
    assert len(set(names)) == len(names)
    assert not any(ch in name for name in names for ch in "іўІЎ")
    assert all(53.80 <= s["lat"] <= 54.00 and 27.35 <= s["lon"] <= 27.80 for s in stations)
