"""Склейка объявлений в объекты и события объекта (зона Веры).

Вход после прохода сборщика — `process_after_run(session, source, now)`.
Разбор склейки — `explain(session, object_id | "listing:<id>" | url)`.
Пороги — `MatchingConfig` в `config.py`.
"""

from minsk_realty.matching.config import DEFAULT_CONFIG, MatchingConfig
from minsk_realty.matching.explain import explain
from minsk_realty.matching.photos import phash_from_bytes, save_photo_hashes
from minsk_realty.matching.pipeline import ProcessStats, process_after_run

__all__ = [
    "DEFAULT_CONFIG",
    "MatchingConfig",
    "ProcessStats",
    "explain",
    "phash_from_bytes",
    "process_after_run",
    "save_photo_hashes",
]
