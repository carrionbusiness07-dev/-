"""Тестовые данные аналитики: сценарий из docs/02 (раздел 1.2) и «фон» рынка.

Сценарий проходит через настоящие сборщик-репозиторий и склейку Веры (как tests/test_matching.py),
фоновые объекты пишутся в БД напрямую — им склейка не нужна, только цены и районы.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from minsk_realty.db.models import Listing, RealtyObject, Snapshot, SourceRun
from minsk_realty.db.repository import mark_unseen_inactive, save_snapshot
from minsk_realty.matching import process_after_run, save_photo_hashes
from minsk_realty.models import ListingSnapshot

DAY, HOUR = timedelta(days=1), timedelta(hours=1)
OBS_START = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

KUFAR_URL = "https://re.kufar.by/vi/1000201"
REALT_URL = "https://realt.by/sale-flats/object/101/"
ONLINER_URL = "https://r.onliner.by/pk/apartments/301"

BASE = {
    "price_currency": "USD",
    "seller_type": "agency",
    "rooms": 2,
    "town": "Минск",
}


def realt(ext: str, **kw) -> ListingSnapshot:
    data = BASE | {
        "source": "realt",
        "external_id": ext,
        "url": f"https://realt.by/sale-flats/object/{ext}/",
        "seen_at": OBS_START,
        "price_amount": Decimal(98000),
        "price_usd": Decimal(98000),
        "agency_name": "Агентство Пример",
        "seller_key": "realt-seller-1",
        "area_total": 54.0,
        "floor": 5,
        "floors_total": 9,
        "street": "Притыцкого ул.",
        "house": "10",
        "address_raw": "Минск, Притыцкого ул., 10",
        "lat": 53.90480,
        "lon": 27.47820,
    }
    return ListingSnapshot(**(data | kw))


def kufar(ext: str, **kw) -> ListingSnapshot:
    data = BASE | {
        "source": "kufar",
        "external_id": ext,
        "url": f"https://re.kufar.by/vi/{ext}",
        "seen_at": OBS_START,
        "price_amount": Decimal(92500),
        "price_usd": Decimal(92500),
        "agency_name": "Агентство Пример",
        "seller_key": "kufar-seller-1",
        "area_total": 54.3,
        "address_raw": "Минск, ул. Притыцкого, 10",
        "district": "Фрунзенский",
        "lat": 53.90500,
        "lon": 27.47850,
    }
    return ListingSnapshot(**(data | kw))


def onliner(ext: str, **kw) -> ListingSnapshot:
    data = BASE | {
        "source": "onliner",
        "external_id": ext,
        "url": f"https://r.onliner.by/pk/apartments/{ext}",
        "seen_at": OBS_START,
        "price_amount": Decimal(91000),
        "price_usd": Decimal(91000),
        "agency_name": "Другое агентство",
        "seller_key": "onliner-seller-9",
        "area_total": 54.0,
        "floor": 5,
        "floors_total": 9,
        "street": "ул. Притыцкого",
        "house": "10",
        "lat": 53.90490,
        "lon": 27.47830,
    }
    return ListingSnapshot(**(data | kw))


def hashes(seed: int, n: int = 4) -> list[str]:
    rnd = random.Random(seed)
    return [f"{rnd.getrandbits(64):016x}" for _ in range(n)]


def blurred(values: list[str], bits: int = 3) -> list[str]:
    return [f"{int(h, 16) ^ ((1 << bits) - 1):016x}" for h in values]


def listing(session, source: str, ext: str) -> Listing:
    return session.scalar(
        select(Listing).where(Listing.source == source, Listing.external_id == ext)
    )


def observe(session, source, at, *snaps, photos=None, complete=True, process=True):
    """Один полный проход площадки: снимки, гашение пропавших, склейка и события."""
    for snap in snaps:
        save_snapshot(session, snap.model_copy(update={"seen_at": at}))
    session.flush()
    for ext, values in (photos or {}).items():
        save_photo_hashes(session, listing(session, source, ext).id, values)
    run = SourceRun(
        source=source,
        started_at=at,
        finished_at=at,
        status="ok",
        complete=complete,
        listings_seen=len(snaps),
    )
    session.add(run)
    mark_unseen_inactive(session, run)
    session.flush()
    if process:
        process_after_run(session, source, now=at)


def start_observation(session, at: datetime = OBS_START, sources=("kufar", "realt", "onliner")):
    """Первые проходы сборщика: с этого момента мы «наблюдаем рынок»."""
    for source in sources:
        session.add(
            SourceRun(source=source, started_at=at, finished_at=at, status="ok", complete=True)
        )
    session.flush()


def build_docs_scenario(session) -> None:
    """Квартира на Притыцкого: Realt с 20.07 ($98 000 → $95 000), снята 30.09, тем же агентством
    перевыложена на Kufar 03.10 за $92 500, другое агентство на Onliner за $91 000."""
    start_observation(session)
    photos = hashes(1)
    jul20 = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    observe(session, "realt", jul20, realt("101"), photos={"101": photos})
    aug28 = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    observe(
        session, "realt", aug28, realt("101", price_amount=Decimal(95000), price_usd=Decimal(95000))
    )
    sep29 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    observe(
        session, "realt", sep29, realt("101", price_amount=Decimal(95000), price_usd=Decimal(95000))
    )
    observe(session, "realt", sep29 + DAY)  # снято
    oct3 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    observe(session, "kufar", oct3, kufar("1000201"), photos={"1000201": blurred(photos)[:3]})
    oct5 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    observe(session, "onliner", oct5, onliner("301"), photos={"301": blurred(photos, 2)})
    for source, snap in (("kufar", kufar("1000201")), ("onliner", onliner("301"))):
        observe(session, source, NOW - HOUR, snap)


def add_background(
    session,
    n: int,
    *,
    district: str | None = "Фрунзенский",
    rooms: int = 2,
    ppm_values: list[float] | None = None,
    source: str = "kufar",
    start: datetime = OBS_START,
    step: timedelta = 7 * DAY,
    prefix: str = "bg",
) -> list[int]:
    """`n` фоновых объектов: по одному активному объявлению, площадь 50 м², цена из ppm."""
    ppm_values = ppm_values or [1780 + 10 * i for i in range(n)]
    ids = []
    for i in range(n):
        first_seen = start + i * step
        obj = RealtyObject(
            street_norm=f"ул тестовая {prefix}{i}",
            house=str(i + 1),
            rooms=rooms,
            area_total=50.0,
            district=district.lower() if district else None,
            first_seen_at=first_seen,
        )
        session.add(obj)
        session.flush()
        row = Listing(
            source=source,
            external_id=f"9{prefix.encode().hex()}{i:05d}",
            url=f"https://example.invalid/{prefix}/{i}",
            object_id=obj.id,
            match_confidence="high",
            seller_type="owner",
            seller_key=f"{prefix}-seller-{i}",
            status="active",
            first_seen=first_seen,
            last_seen=NOW - HOUR,
        )
        session.add(row)
        session.flush()
        price = Decimal(str(round(ppm_values[i] * 50)))
        session.add(
            Snapshot(
                listing_id=row.id,
                seen_at=first_seen,
                content_hash=f"bg{row.id}",
                price_amount=price,
                price_currency="USD",
                price_usd=price,
                rooms=rooms,
                area_total=50.0,
                district=district,
                town="Минск",
            )
        )
        ids.append(obj.id)
    session.flush()
    return ids
