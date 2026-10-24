"""Хеши фото после прохода площадки (спека оценки квартир, раздел 11).

Скачать фото в память → `phash_from_bytes` → выбросить байты → `save_photo_hashes`.
На диск ничего не пишется, в БД остаётся только 16-символьный хеш.

Бережность: не больше 6 фото на объявление и 150 на площадку за проход, один запрос за раз,
пауза 1–2 с, картинка не больше 5 МБ и только `image/*`, таймаут 20 с. Отказы 403/429 —
через `PoliteClient`: после трёх подряд хеширование площадки стоит до следующего прохода.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from minsk_realty.db.models import Listing, PhotoHash, Snapshot
from minsk_realty.matching.photos import phash_from_bytes, save_photo_hashes
from minsk_realty.sources.base import PoliteClient, SourceBlocked

log = logging.getLogger("minsk_realty.collector")

PHOTOS_PER_LISTING = 6
PHOTO_BUDGET = 150
PHOTO_PAUSE = (1.0, 2.0)
MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_TIMEOUT = 20.0  # на каждую операцию сокета
PHOTO_DEADLINE = 20.0  # на всё фото целиком: медленная «капельная» отдача не держит проход
PHOTO_MAX_REDIRECTS = 3
# Группа 4 «новые» — впервые замеченные после включения шага (день выкладки, 24.10.2026 UTC).
# Записи «когда шаг впервые сработал» в БД нет, поэтому дата задана здесь.
PHOTO_HASHING_SINCE = datetime(2026, 10, 24, tzinfo=UTC)
AREA_TOLERANCE = 1.0  # м², как в спеке: кандидаты-двойники — площадь ±1 м²

# shortcut: объявления, у которых не получилось ни одного хеша (все фото 404/битые/заглушки),
# пропускаются только до перезапуска процесса (множество в памяти). Перейти на отметку в БД,
# если после перезапуска такие объявления заметно съедают бюджет или множество растёт за 10⁵.
_NO_HASHES: set[int] = set()


def photo_hashing_enabled() -> bool:
    """Выключатель без правки кода: PHOTO_HASHING=0 (или false/no) в .env."""
    return os.environ.get("PHOTO_HASHING", "1").strip().lower() not in ("0", "false", "no")


@dataclass
class PhotoStats:
    listings: int = 0  # объявлений, за фото которых брались
    photos: int = 0  # скачано картинок
    hashes: int = 0  # новых строк photo_hashes
    failed: int = 0  # 404, не картинка, больше 5 МБ, обрыв связи, битые байты
    refused: bool = False  # хост трижды подряд ответил 403/429


class PhotoClient(PoliteClient):
    """`PoliteClient` для картинок: те же отказы и backoff, своя пауза, таймаут и предел тела."""

    clock = staticmethod(time.monotonic)

    def __init__(self, *, pause: tuple[float, float] = PHOTO_PAUSE, **kwargs) -> None:
        super().__init__(page_pause=pause, timeout=PHOTO_TIMEOUT, **kwargs)
        # Переходы на другой хост не запрещаем (CDN площадок так и отдают), но не больше трёх.
        self._client.max_redirects = PHOTO_MAX_REDIRECTS

    def _get_retrying(self, url: str, **kwargs) -> httpx.Response:
        # Без повторов при обрыве: потерянное фото дешевле 40 с ожидания. Тело читаем сами
        # и бросаем после 5 МБ или 20 с; не картинка, слишком большая или долгая — пустой ответ.
        started = self.clock()
        with self._client.stream("GET", url, **kwargs) as r:
            ctype = r.headers.get("content-type", "").lower()
            body = b""
            if r.is_success and ctype.startswith("image/"):
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_PHOTO_BYTES or self.clock() - started > PHOTO_DEADLINE:
                        chunks = []
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
            return httpx.Response(
                r.status_code, headers={"content-type": ctype}, content=body, request=r.request
            )


def _latest_snapshot_id():
    latest = aliased(Snapshot)
    return (
        select(latest.id)
        .where(latest.listing_id == Listing.id)
        .order_by(latest.seen_at.desc(), latest.id.desc())
        .limit(1)
        .correlate(Listing)
        .scalar_subquery()
    )


def _unresolved():
    return or_(Listing.object_id.is_(None), Listing.match_confidence == "low")


def _is_partner(rooms, floor, area, others) -> bool:
    if rooms is None or area is None:
        return False
    return any(
        (floor is None or f is None or f == floor) and abs(a - area) <= AREA_TOLERANCE
        for f, a in others.get(rooms, ())
    )


def photo_backlog(
    session: Session,
    source: str,
    *,
    limit_listings: int,
    since: datetime = PHOTO_HASHING_SINCE,
) -> list[tuple[int, list[str]]]:
    """(listing_id, ссылки) активных объявлений площадки без хешей, по приоритету раздела 11:
    1) не склеены; 2) склеены с low; 3) кандидаты-двойники 1–2 (те же комнаты, этаж или
    неизвестен, площадь ±1 м²); 4) впервые замечены после `since`. Остальные не берутся.
    """
    # as_string: JSON_EXTRACT / ->> дают NULL, если ключа нет (в SQLite без него было бы 'null').
    has_photos = Snapshot.extra["photo_urls"].as_string().is_not(None)
    # Узкая выборка (id и четыре числа) всех кандидатов площадки: группу 3 считаем в Python.
    rows = session.execute(
        select(
            Listing.id,
            Listing.object_id,
            Listing.match_confidence,
            Listing.first_seen,
            Snapshot.rooms,
            Snapshot.floor,
            Snapshot.area_total,
        )
        .join(Snapshot, Snapshot.id == _latest_snapshot_id())
        .where(
            Listing.source == source,
            Listing.status == "active",
            ~exists().where(PhotoHash.listing_id == Listing.id),
            has_photos,
        )
        .order_by(Listing.id)
    ).all()
    rows = [r for r in rows if r.id not in _NO_HASHES]

    groups: list[list[int]] = [[], [], [], []]
    rest = []
    for r in rows:
        if r.object_id is None:
            groups[0].append(r.id)
        elif r.match_confidence == "low":
            groups[1].append(r.id)
        else:
            rest.append(r)
    if rest:
        # Нерешённые объявления всех площадок: двойник может лежать на другой площадке.
        others: dict[int, list[tuple[int | None, float]]] = {}
        for listing_id, rooms, floor, area in session.execute(
            select(Listing.id, Snapshot.rooms, Snapshot.floor, Snapshot.area_total)
            .join(Snapshot, Snapshot.id == _latest_snapshot_id())
            .where(
                Listing.status == "active",
                _unresolved(),
                and_(Snapshot.rooms.is_not(None), Snapshot.area_total.is_not(None)),
            )
        ):
            others.setdefault(rooms, []).append((floor, area))
        for r in rest:
            if _is_partner(r.rooms, r.floor, r.area_total, others):
                groups[2].append(r.id)
            elif r.first_seen >= since:
                groups[3].append(r.id)

    chosen = [listing_id for group in groups for listing_id in group][:limit_listings]
    if not chosen:
        return []
    extras = dict(
        session.execute(
            select(Listing.id, Snapshot.extra)
            .join(Snapshot, Snapshot.id == _latest_snapshot_id())
            .where(Listing.id.in_(chosen))
        ).all()
    )
    return [(i, list(extras[i]["photo_urls"])[:PHOTOS_PER_LISTING]) for i in chosen]


def hash_photos(
    session_factory,
    source: str,
    *,
    budget: int = PHOTO_BUDGET,
    pause: tuple[float, float] = PHOTO_PAUSE,
    client: PoliteClient | None = None,
    since: datetime = PHOTO_HASHING_SINCE,
) -> PhotoStats:
    """Скачать и захешировать фото площадки в пределах бюджета. Байты живут только в памяти."""
    stats = PhotoStats()
    with session_factory() as session:
        backlog = photo_backlog(session, source, limit_listings=budget, since=since)
    own_client = client is None
    client = client or PhotoClient(pause=pause)
    requests = 0
    try:
        for listing_id, urls in backlog:
            if requests + len(urls) > budget:
                break  # объявление берём целиком: частичные хеши закрыли бы его навсегда
            stats.listings += 1
            hashes = []
            for url in urls:
                if requests:
                    client.pause()
                requests += 1
                try:
                    content = client.get(url).content
                    if not content:
                        raise ValueError("не картинка, больше 5 МБ или дольше 20 с")
                    stats.photos += 1
                    hashes.append(phash_from_bytes(content))
                except SourceBlocked:
                    raise
                except Exception as exc:  # noqa: BLE001 — одно битое фото не останавливает проход
                    stats.failed += 1  # только тип: в тексте ошибок httpx бывает URL
                    log.debug("%s: фото объявления %s: %s", source, listing_id, type(exc).__name__)
            added = 0
            if hashes:
                with session_factory() as session:  # сбой БД — дальше, в обёртку цикла
                    added = save_photo_hashes(session, listing_id, hashes)
                    session.commit()
            stats.hashes += added
            if not added:
                _NO_HASHES.add(listing_id)
    except SourceBlocked:
        stats.refused = True
        log.error(
            "%s: хост фото трижды подряд отказал (403/429), фото до следующего прохода", source
        )
    finally:
        if own_client:
            client.close()
    log.info(
        "%s: фото — объявлений %d, скачано %d, хешей %d, ошибок %d",
        source,
        stats.listings,
        stats.photos,
        stats.hashes,
        stats.failed,
    )
    return stats
