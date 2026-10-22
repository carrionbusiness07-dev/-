"""Склейка и события на парах объявлений. Сети нет: снимки собираются вручную.

Каждый `observe` — один проход площадки: записать снимки, погасить пропавшие объявления
(как полный проход сборщика) и запустить `process_after_run`.
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from minsk_realty.db.models import Base, Event, Listing, RealtyObject, Snapshot, SourceRun
from minsk_realty.db.repository import mark_unseen_inactive, save_snapshot
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.matching import explain, process_after_run, save_photo_hashes
from minsk_realty.models import ListingSnapshot

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
HOUR, DAY = timedelta(hours=1), timedelta(days=1)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'matching.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        yield session


def realt(ext, **kw) -> ListingSnapshot:
    data = {
        "source": "realt",
        "external_id": ext,
        "url": f"https://realt.by/sale-flats/object/{ext}/",
        "seen_at": T0,
        "price_amount": Decimal(92500),
        "price_currency": "USD",
        "price_usd": Decimal(92500),
        "seller_type": "agency",
        "agency_name": "Агентство Пример",
        "seller_key": "realt-seller-1",
        "rooms": 2,
        "area_total": 54.0,
        "floor": 5,
        "floors_total": 9,
        "street": "Притыцкого ул.",
        "house": "10",
        "address_raw": "Минск, Притыцкого ул., 10",
        "town": "Минск",
        "lat": 53.90480,
        "lon": 27.47820,
    }
    return ListingSnapshot(**(data | kw))


def kufar(ext, **kw) -> ListingSnapshot:
    # Как отдаёт адаптер Kufar: улица и дом только в строке адреса, этажа нет.
    data = {
        "source": "kufar",
        "external_id": ext,
        "url": f"https://re.kufar.by/vi/{ext}",
        "seen_at": T0,
        "price_amount": Decimal(92500),
        "price_currency": "USD",
        "price_usd": Decimal(92500),
        "seller_type": "agency",
        "seller_key": "kufar-seller-1",
        "rooms": 2,
        "area_total": 54.3,
        "address_raw": "Минск, ул. Притыцкого, 10",
        "district": "Фрунзенский",
        "town": "Минск",
        "lat": 53.90500,
        "lon": 27.47850,  # ~30 м от точки Realt
    }
    return ListingSnapshot(**(data | kw))


def hashes(seed, n=4):
    rnd = random.Random(seed)
    return [f"{rnd.getrandbits(64):016x}" for _ in range(n)]


def blurred(values, bits=3):
    """Те же фото после пережатия на другой площадке: несколько бит phash отличаются."""
    return [f"{int(h, 16) ^ ((1 << bits) - 1):016x}" for h in values]


def listing(session, source, ext) -> Listing:
    return session.scalar(
        select(Listing).where(Listing.source == source, Listing.external_id == ext)
    )


def observe(session, source, at, *snaps, photos=None, complete=True, process=True):
    for snap in snaps:
        save_snapshot(session, snap.model_copy(update={"seen_at": at}))
    session.flush()
    for ext, values in (photos or {}).items():
        save_photo_hashes(session, listing(session, source, ext).id, values)
    run = SourceRun(source=source, started_at=at, status="ok", complete=complete)
    mark_unseen_inactive(session, run)
    session.flush()
    return process_after_run(session, source, now=at) if process else None


def events(session, object_id, *, with_payload=False):
    rows = session.scalars(
        select(Event).where(Event.object_id == object_id).order_by(Event.at, Event.id)
    ).all()
    return rows if with_payload else [e.type for e in rows]


def objects_count(session):
    return session.scalar(select(func.count()).select_from(RealtyObject))


# --- Одна квартира на двух площадках -------------------------------------------------


def test_same_flat_on_kufar_and_realt_without_photos_is_low(session):
    observe(session, "realt", T0, realt("101"))
    observe(session, "kufar", T0 + HOUR, kufar("201"))
    r, k = listing(session, "realt", "101"), listing(session, "kufar", "201")
    assert k.object_id == r.object_id
    assert (r.match_confidence, k.match_confidence) == ("high", "low")
    # Склейка low не порождает объектных событий: cross_listed не пишется.
    assert events(session, r.object_id) == ["new"]


def test_same_flat_on_kufar_and_realt_with_photos_is_high(session):
    photos = hashes(1)
    observe(session, "realt", T0, realt("101"), photos={"101": photos})
    observe(session, "kufar", T0 + HOUR, kufar("201"), photos={"201": blurred(photos)[:3]})
    r, k = listing(session, "realt", "101"), listing(session, "kufar", "201")
    assert (k.object_id, k.match_confidence) == (r.object_id, "high")
    assert objects_count(session) == 1
    [new, cross] = events(session, r.object_id, with_payload=True)
    assert (new.type, cross.type, cross.listing_id) == ("new", "cross_listed", k.id)
    assert cross.notify and cross.payload["sources"] == ["kufar", "realt"]


def test_low_link_is_upgraded_when_photos_arrive(session):
    observe(session, "realt", T0, realt("101"))
    observe(session, "kufar", T0 + HOUR, kufar("201"))
    assert listing(session, "kufar", "201").match_confidence == "low"

    photos = hashes(2)
    save_photo_hashes(session, listing(session, "realt", "101").id, photos)
    save_photo_hashes(session, listing(session, "kufar", "201").id, blurred(photos))
    stats = process_after_run(session, "kufar", now=T0 + 2 * HOUR)
    k = listing(session, "kufar", "201")
    assert k.match_confidence == "high" and stats.regraded == 1
    assert events(session, k.object_id) == ["new", "cross_listed"]


def test_low_link_is_detached_when_photos_contradict(session):
    observe(session, "realt", T0, realt("101"))
    observe(session, "kufar", T0 + HOUR, kufar("201"))
    observe(
        session,
        "kufar",
        T0 + DAY,
        kufar("201", price_amount=Decimal(91000)),
        process=False,
    )
    save_photo_hashes(session, listing(session, "realt", "101").id, hashes(3))
    save_photo_hashes(session, listing(session, "kufar", "201").id, hashes(4))
    stats = process_after_run(session, "kufar", now=T0 + DAY)
    r, k = listing(session, "realt", "101"), listing(session, "kufar", "201")
    assert stats.detached == 1 and k.object_id != r.object_id
    assert k.match_confidence == "high"  # основное объявление своего объекта
    # Снижение цены Kufar ушло вместе с объявлением, у объекта Realt его нет.
    assert events(session, r.object_id) == ["new"]
    assert events(session, k.object_id) == ["new", "price_drop"]


def test_level1_fingerprint_two_agencies_on_realt_is_medium(session):
    observe(session, "realt", T0, realt("101"))
    other = {"seller_key": "realt-seller-2", "agency_name": "Другое агентство"}
    # Улица записана иначе, площадь округлена по-другому — отпечаток тот же.
    observe(
        session,
        "realt",
        T0 + DAY,
        realt("101"),
        realt("102", street="ул. Притыцкого", house="д. 10", area_total=54.8, **other),
    )
    a, b = listing(session, "realt", "101"), listing(session, "realt", "102")
    assert (b.object_id, b.match_confidence) == (a.object_id, "medium")
    assert events(session, a.object_id) == ["new", "cross_listed"]


# --- Похожие, но разные квартиры --------------------------------------------------------


def test_neighbour_flats_on_one_floor_are_not_glued(session):
    observe(
        session,
        "realt",
        T0,
        realt("101"),
        realt("102", area_total=48.1, seller_key="realt-seller-2", agency_name=None),
    )
    assert listing(session, "realt", "101").object_id != listing(session, "realt", "102").object_id

    # На Kufar этажа нет: одинаковые 2-комнатные в одном доме у разных продавцов,
    # висящие одновременно, — скорее соседи, чем одна квартира.
    observe(
        session,
        "kufar",
        T0,
        kufar("201", area_total=61.0),
        kufar("202", area_total=61.4, seller_key="kufar-seller-2"),
    )
    k1, k2 = listing(session, "kufar", "201"), listing(session, "kufar", "202")
    assert k1.object_id != k2.object_id
    assert objects_count(session) == 4


def test_second_weak_listing_from_same_platform_is_not_glued(session):
    observe(session, "realt", T0, realt("101"))
    # Две похожие квартиры на Kufar (разные продавцы) — к объекту Realt подходят обе,
    # но одновременно висеть на одной площадке одна квартира у разных продавцов вряд ли будет.
    observe(
        session,
        "kufar",
        T0 + HOUR,
        kufar("201"),
        kufar("202", area_total=54.5, seller_key="kufar-seller-2"),
    )
    r = listing(session, "realt", "101")
    k1, k2 = listing(session, "kufar", "201"), listing(session, "kufar", "202")
    assert (k1.object_id, k1.match_confidence) == (r.object_id, "low")
    assert k2.object_id != r.object_id


def test_same_layout_in_neighbour_entrance_is_not_glued_when_photos_differ(session):
    observe(
        session,
        "realt",
        T0,
        realt("101"),
        realt("102", seller_key="realt-seller-2", agency_name=None, lat=53.90510, lon=27.47900),
        photos={"101": hashes(5), "102": hashes(6)},
    )
    a, b = listing(session, "realt", "101"), listing(session, "realt", "102")
    assert a.object_id != b.object_id
    assert "фото противоречат" in explain(session, f"listing:{b.id}", now=T0)


def test_same_agency_two_identical_flats_are_not_glued(session):
    # Новостройка: агентство продаёт две одинаковые квартиры.
    observe(session, "realt", T0, realt("101"))
    observe(session, "realt", T0 + HOUR, realt("101"), realt("102"))
    assert listing(session, "realt", "102").object_id is None  # ждём: вдруг перевыкладывание
    observe(session, "realt", T0 + DAY + 2 * HOUR, realt("101"), realt("102"))
    a, b = listing(session, "realt", "101"), listing(session, "realt", "102")
    assert b.object_id is not None and a.object_id != b.object_id
    assert "relisted" not in events(session, a.object_id)


def test_ambiguous_candidates_create_separate_object(session):
    # Две квартиры одного дома на разных этажах (Realt) и объявление Kufar без этажа:
    # подходит к обеим одинаково слабо — не выбираем наугад.
    observe(
        session,
        "realt",
        T0,
        realt("101", floor=3),
        realt("102", floor=7, seller_key="realt-seller-2", agency_name=None),
    )
    stats = observe(session, "kufar", T0 + HOUR, kufar("201"))
    assert stats.ambiguous == 1 and objects_count(session) == 3


# --- Перевыкладывание ---------------------------------------------------------------------


def test_relisted_by_agency_with_new_price(session):
    observe(session, "kufar", T0, kufar("201"))
    observe(session, "kufar", T0 + 2 * DAY)  # объявление снято
    observe(session, "kufar", T0 + 6 * DAY, kufar("202", price_amount=Decimal(90000)))
    old, new = listing(session, "kufar", "201"), listing(session, "kufar", "202")
    assert new.object_id == old.object_id and new.match_confidence == "medium"
    rows = events(session, old.object_id, with_payload=True)
    assert [e.type for e in rows] == ["new", "relisted", "price_drop"]
    relisted, drop = rows[1], rows[2]
    assert relisted.payload["previous_listing_id"] == old.id
    assert relisted.payload["gap_days"] == 6.0
    assert drop.payload.items() >= {"old": "92500.00", "new": "90000.00", "currency": "USD"}.items()
    assert drop.payload["via"] == "relisted" and drop.payload["pct"] == -2.7
    assert drop.notify and relisted.notify


def test_relist_waits_while_old_listing_is_still_up(session):
    observe(session, "kufar", T0, kufar("201"))
    # Агентство сначала публикует новое объявление, старое снимает к следующему проходу.
    observe(session, "kufar", T0 + HOUR, kufar("201"), kufar("202", price_amount=Decimal(91000)))
    assert listing(session, "kufar", "202").object_id is None
    observe(session, "kufar", T0 + 2 * HOUR, kufar("202", price_amount=Decimal(91000)))
    new = listing(session, "kufar", "202")
    assert new.object_id == listing(session, "kufar", "201").object_id
    assert events(session, new.object_id) == ["new", "relisted", "price_drop"]


# --- Цена и валюта ---------------------------------------------------------------------


def test_exchange_rate_jump_is_not_a_price_drop(session):
    byn = {"price_amount": Decimal(300000), "price_currency": "BYN"}
    observe(session, "realt", T0, realt("101", price_usd=Decimal(100000), **byn))
    # Курс прыгнул: в долларах «подешевело» на 5%, в рублях цена та же.
    observe(session, "realt", T0 + DAY, realt("101", price_usd=Decimal(95000), **byn))
    item = listing(session, "realt", "101")
    # Снимок мог появиться и по другой причине (поправили адрес) — цена всё равно не менялась.
    session.add(
        Snapshot(
            listing_id=item.id,
            seen_at=T0 + 2 * DAY,
            content_hash="v1:test",
            price_amount=Decimal(300000),
            price_currency="BYN",
            price_usd=Decimal(90000),
            fx_rate=Decimal("3.333333"),
            rooms=2,
            area_total=54.0,
        )
    )
    process_after_run(session, "realt", now=T0 + 2 * DAY)
    assert events(session, item.object_id) == ["new"]


def test_currency_switch_is_not_a_price_event(session):
    observe(session, "realt", T0, realt("101"))
    observe(
        session,
        "realt",
        T0 + DAY,
        realt("101", price_amount=Decimal(300000), price_currency="BYN"),
    )
    observe(
        session,
        "realt",
        T0 + 2 * DAY,
        realt("101", price_amount=Decimal(290000), price_currency="BYN"),
    )
    item = listing(session, "realt", "101")
    rows = events(session, item.object_id, with_payload=True)
    assert [e.type for e in rows] == ["new", "price_drop"]
    assert rows[1].payload["currency"] == "BYN" and rows[1].at == T0 + 2 * DAY


def test_price_threshold_is_half_percent_from_last_event(session):
    prices = ["92500", "92100", "91950", "92500"]  # −0.43%, −0.59% от базы, +0.6%
    for i, price in enumerate(prices):
        observe(session, "realt", T0 + i * DAY, realt("101", price_amount=Decimal(price)))
    rows = events(session, listing(session, "realt", "101").object_id, with_payload=True)
    assert [(e.type, e.payload.get("pct")) for e in rows] == [
        ("new", None),
        ("price_drop", -0.59),
        ("price_up", 0.6),
    ]


# --- Снятие и возвращение -----------------------------------------------------------


def test_removed_only_after_three_days(session):
    observe(session, "realt", T0, realt("101"))
    observe(session, "realt", T0 + HOUR)  # пропало из выдачи
    object_id = listing(session, "realt", "101").object_id
    observe(session, "realt", T0 + 3 * DAY - timedelta(minutes=1))
    assert "removed" not in events(session, object_id)
    observe(session, "realt", T0 + 3 * DAY)
    [removed] = [e for e in events(session, object_id, with_payload=True) if e.type == "removed"]
    assert removed.at == T0 and removed.payload["confirmed_at"] == (T0 + 3 * DAY).isoformat()


def test_incomplete_runs_never_produce_removed(session):
    observe(session, "realt", T0, realt("101"))
    for day in range(1, 6):
        observe(session, "realt", T0 + day * DAY, complete=False)  # площадка отдала не всё
    assert events(session, listing(session, "realt", "101").object_id) == ["new"]


def test_returned_after_removed(session):
    observe(session, "realt", T0, realt("101"))
    observe(session, "realt", T0 + HOUR)
    observe(session, "realt", T0 + 4 * DAY)
    observe(session, "realt", T0 + 5 * DAY, realt("101"))
    item = listing(session, "realt", "101")
    rows = events(session, item.object_id, with_payload=True)
    assert [e.type for e in rows] == ["new", "removed", "returned"]
    assert rows[2].at == T0 + 5 * DAY and rows[2].payload["new_listing"] is False

    # Снова пропало и вернулось уже у другого продавца: returned, но не relisted.
    observe(session, "realt", T0 + 6 * DAY)
    observe(session, "realt", T0 + 10 * DAY)
    observe(
        session,
        "realt",
        T0 + 11 * DAY,
        realt("102", seller_key="realt-seller-2", agency_name=None),
    )
    assert listing(session, "realt", "102").object_id == item.object_id
    assert events(session, item.object_id) == [
        "new",
        "removed",
        "returned",
        "removed",
        "returned",
    ]


def test_low_link_mutes_its_events_and_blocks_removed(session):
    observe(session, "realt", T0, realt("101"))
    observe(session, "kufar", T0 + HOUR, kufar("201"))
    observe(session, "kufar", T0 + DAY, kufar("201", price_amount=Decimal(90000)))
    observe(session, "realt", T0 + DAY)  # на Realt объявление снято
    observe(session, "kufar", T0 + 5 * DAY, kufar("201", price_amount=Decimal(90000)))
    object_id = listing(session, "realt", "101").object_id
    rows = events(session, object_id, with_payload=True)
    assert [(e.type, e.notify) for e in rows] == [("new", True), ("price_drop", False)]


# --- Идемпотентность и разбор --------------------------------------------------------


def test_reprocessing_does_not_duplicate_events(session):
    observe(session, "kufar", T0, kufar("201"))
    observe(session, "kufar", T0 + 2 * DAY)
    observe(session, "kufar", T0 + 6 * DAY, kufar("202", price_amount=Decimal(90000)))
    observe(session, "realt", T0 + 6 * DAY, realt("101"), photos={})
    before = session.scalar(select(func.count()).select_from(Event))
    confidences = dict(session.execute(select(Listing.id, Listing.match_confidence)).all())
    for now in (T0 + 6 * DAY, T0 + 6 * DAY + HOUR):
        process_after_run(session, "kufar", now=now)
    process_after_run(session, None, now=T0 + 6 * DAY + HOUR, full=True)
    assert session.scalar(select(func.count()).select_from(Event)) == before
    assert dict(session.execute(select(Listing.id, Listing.match_confidence)).all()) == confidences


def test_explain_by_url_and_object(session):
    observe(session, "realt", T0, realt("101"))
    observe(session, "kufar", T0 + HOUR, kufar("1000201"))
    text = explain(session, "https://re.kufar.by/vi/1000201", now=T0 + HOUR)
    assert "kufar/1000201" in text and "уверенность low" in text and "адрес без этажа" in text
    object_id = listing(session, "realt", "101").object_id
    text = explain(session, object_id, now=T0 + HOUR)
    assert f"Объект #{object_id}" in text and "основное объявление" in text
    assert "не найдено" in explain(session, "https://realt.by/sale-flats/object/999/")
