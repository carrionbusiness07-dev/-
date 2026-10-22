"""Демо-база для кабинета: ~90 дней вымышленного рынка через настоящие сборщик и склейку.

    python scripts/demo_db.py demo.db            # создать/перезаписать демо-базу
    DATABASE_URL=sqlite:///demo.db python -m minsk_realty.web

Все квартиры, адреса, цены и агентства ВЫМЫШЛЕНЫ. Кабинет показывает плашку «демо-данные»,
пока в базе нет ни одного настоящего прохода сборщика (см. web/app.py).
"""

from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from minsk_realty.db.models import Base, Listing, SourceRun
from minsk_realty.db.repository import mark_unseen_inactive, save_snapshot
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.matching import process_after_run, save_photo_hashes
from minsk_realty.models import ListingSnapshot

DEMO_MARKER = "demo"  # пишется в SourceRun.error у демо-проходов: по нему кабинет узнаёт демо
START = datetime(2026, 7, 24, 6, 0, tzinfo=UTC)
DAYS = 90

# Район → (база $/м², улицы с примерными координатами). Улицы условные.
DISTRICTS = {
    "Центральный": (2650, [("Тимирязева", 53.915, 27.535), ("Кальварийская", 53.909, 27.520)]),
    "Советский": (2450, [("Сурганова", 53.925, 27.590), ("Богдановича", 53.920, 27.570)]),
    "Первомайский": (2250, [("Калиновского", 53.950, 27.640), ("Уручская", 53.945, 27.690)]),
    "Партизанский": (2100, [("Ванеева", 53.885, 27.610), ("Тростенецкая", 53.880, 27.625)]),
    "Заводской": (1950, [("Ангарская", 53.860, 27.660), ("Плеханова", 53.870, 27.640)]),
    "Ленинский": (2050, [("Чкалова", 53.880, 27.540), ("Кижеватова", 53.858, 27.540)]),
    "Октябрьский": (2150, [("Аэродромная", 53.870, 27.560), ("Корженевского", 53.850, 27.520)]),
    "Московский": (2300, [("Алибегова", 53.880, 27.480), ("Голубева", 53.875, 27.500)]),
    "Фрунзенский": (2200, [("Притыцкого", 53.905, 27.470), ("Сухаревская", 53.900, 27.440)]),
}
AGENCIES = ["Квадрат Плюс", "Минск Недвижимость", "Дом и Метр", "Столичный Ключ", "Альфа-Хаус"]
AREA = {1: (33, 42), 2: (44, 58), 3: (60, 78), 4: (78, 105)}


@dataclass
class Flat:
    oid: int
    district: str
    street: str
    house: str
    lat: float
    lon: float
    rooms: int
    area: float
    floor: int
    floors: int
    price: int
    agency: str | None
    photos: list[str]
    sources: tuple[str, ...]
    ext: dict[str, str] = field(default_factory=dict)
    active: bool = True
    gone_until: int | None = None


def money(value: float) -> int:
    return int(round(value / 500) * 500)


def make_flat(rnd: random.Random, oid: int) -> Flat:
    district = rnd.choice(list(DISTRICTS))
    base, streets = DISTRICTS[district]
    street, lat, lon = rnd.choice(streets)
    rooms = rnd.choices([1, 2, 3, 4], weights=[40, 34, 20, 6])[0]
    area = round(rnd.uniform(*AREA[rooms]), 1)
    floors = rnd.choice([5, 9, 9, 10, 12, 16, 19])
    roll = rnd.random()
    sources = ("realt",) if roll < 0.4 else ("kufar",) if roll < 0.7 else ("realt", "kufar")
    flat = Flat(
        oid=oid,
        district=district,
        street=street,
        house=str(rnd.randint(1, 120)),
        lat=lat + rnd.uniform(-0.006, 0.006),
        lon=lon + rnd.uniform(-0.009, 0.009),
        rooms=rooms,
        area=area,
        floor=rnd.randint(1, floors),
        floors=floors,
        price=money(area * base * rnd.uniform(0.85, 1.2)),
        agency=rnd.choice(AGENCIES) if rnd.random() < 0.6 else None,
        photos=[f"{rnd.getrandbits(64):016x}" for _ in range(5)],
        sources=sources,
    )
    for source in sources:
        flat.ext[source] = new_ext(rnd, source)
    return flat


def new_ext(rnd: random.Random, source: str) -> str:
    return str(
        rnd.randint(4_000_000, 4_999_999) if source == "realt" else rnd.randint(10**9, 2 * 10**9)
    )


def snapshot(flat: Flat, source: str, at: datetime) -> ListingSnapshot:
    ext = flat.ext[source]
    common = {
        "source": source,
        "external_id": ext,
        "seen_at": at,
        "price_amount": Decimal(flat.price),
        "price_currency": "USD",
        "price_usd": Decimal(flat.price),
        "seller_type": "agency" if flat.agency else "owner",
        "agency_name": flat.agency,
        "seller_key": f"demo-{source}-{flat.agency or flat.oid}",
        "rooms": flat.rooms,
        "area_total": flat.area,
        "town": "Минск",
        "lat": flat.lat,
        "lon": flat.lon,
    }
    if source == "realt":
        return ListingSnapshot(
            **common,
            url=f"https://realt.by/sale-flats/object/{ext}/",
            floor=flat.floor,
            floors_total=flat.floors,
            street=f"{flat.street} ул.",
            house=flat.house,
            address_raw=f"Минск, {flat.street} ул., {flat.house}",
        )
    return ListingSnapshot(
        **common,
        url=f"https://re.kufar.by/vi/{ext}",
        address_raw=f"Минск, ул. {flat.street}, {flat.house}",
        district=flat.district,
    )


def build(path: Path, seed: int = 7, days: int = DAYS, flats_at_start: int = 260) -> None:
    rnd = random.Random(seed)
    if path.exists():
        path.unlink()
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    flats = [make_flat(rnd, i) for i in range(flats_at_start)]
    next_id = len(flats)

    with factory() as session:
        for day in range(days):
            today = START + timedelta(days=day)
            # Жизнь рынка за сутки.
            for _ in range(rnd.randint(1, 5)):
                flats.append(make_flat(rnd, next_id))
                next_id += 1
            for flat in flats:
                if not flat.active:
                    if flat.gone_until == day:
                        flat.active = True  # вернулась на рынок
                    continue
                r = rnd.random()
                if r < 0.025:
                    flat.price = money(flat.price * rnd.uniform(0.94, 0.985))
                elif r < 0.03:
                    flat.price = money(flat.price * rnd.uniform(1.01, 1.03))
                elif r < 0.04 and flat.agency:
                    # Перевыкладывание: агентство снимает и выкладывает заново, часто дешевле.
                    source = rnd.choice(flat.sources)
                    flat.ext[source] = new_ext(rnd, source)
                    if rnd.random() < 0.6:
                        flat.price = money(flat.price * rnd.uniform(0.96, 0.99))
                elif r < 0.05:
                    flat.active = False
                    flat.gone_until = day + rnd.randint(6, 30) if rnd.random() < 0.15 else None

            for i, source in enumerate(("kufar", "realt")):
                at = today + timedelta(hours=3 + i, minutes=rnd.randint(0, 40))
                seen = 0
                for flat in flats:
                    if flat.active and source in flat.sources:
                        save_snapshot(session, snapshot(flat, source, at))
                        seen += 1
                session.flush()
                for flat in flats:
                    if flat.active and source in flat.sources:
                        listing = session.scalar(
                            select(Listing.id).where(
                                Listing.source == source, Listing.external_id == flat.ext[source]
                            )
                        )
                        save_photo_hashes(session, listing, flat.photos)
                run = SourceRun(
                    source=source,
                    started_at=at,
                    finished_at=at + timedelta(minutes=6),
                    status="ok",
                    complete=True,
                    listings_seen=seen,
                    error=DEMO_MARKER,
                )
                session.add(run)
                mark_unseen_inactive(session, run)
                session.flush()
                process_after_run(session, source, now=at)
            session.commit()
            print(f"\r{today:%d.%m}: {sum(f.active for f in flats)} активных", end="", flush=True)

        # Состояние «сейчас»: последний проход Kufar с ошибками разбора — чтобы кабинет показал
        # жёлтый статус источника.
        last = START + timedelta(days=days - 1, hours=5)
        session.add(
            SourceRun(
                source="kufar",
                started_at=last,
                finished_at=last + timedelta(minutes=5),
                status="ok",
                complete=False,
                listings_seen=212,
                parse_errors=31,
                error=DEMO_MARKER,
            )
        )
        session.commit()
    print(f"\nготово: {path}")


if __name__ == "__main__":
    os.environ.setdefault("SELLER_KEY_SALT", "demo")
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "demo.db"))
