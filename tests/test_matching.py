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
from minsk_realty.matching import MatchingConfig, explain, process_after_run, save_photo_hashes
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


def test_level1_fingerprint_on_other_platform_is_medium(session):
    observe(session, "realt", T0, realt("101"))
    # Улица записана иначе, площадь округлена по-другому — отпечаток тот же. Другая площадка
    # (адаптера ещё нет, но схема её допускает) с этажом — отпечаток уровня 1 без фото.
    observe(
        session,
        "onliner",
        T0 + DAY,
        realt(
            "102",
            source="onliner",
            street="ул. Притыцкого",
            house="д. 10",
            area_total=54.8,
            seller_key="onliner-seller-2",
            agency_name="Другое агентство",
        ),
    )
    a, b = listing(session, "realt", "101"), listing(session, "onliner", "102")
    assert (b.object_id, b.match_confidence) == (a.object_id, "medium")
    assert b.match_basis == {
        "rule": "compare",
        "location": "fingerprint",
        "photo_matches": 0,
        "photos_needed": 0,
        "relist": False,
    }
    assert a.match_basis == {"rule": "founder"}
    assert events(session, a.object_id) == ["new", "cross_listed"]


def test_two_agencies_same_platform_need_photos(session):
    # Тот же отпечаток уровня 1 у двух агентств на Realt одновременно. Без фото это
    # неотличимо от такой же квартиры в соседнем подъезде — не склеиваем (п. 6 ревью).
    other = {"seller_key": "realt-seller-2", "agency_name": "Другое агентство"}
    observe(session, "realt", T0, realt("101"))
    observe(
        session,
        "realt",
        T0 + DAY,
        realt("101"),
        realt("102", street="ул. Притыцкого", house="д. 10", area_total=54.8, **other),
    )
    a, b = listing(session, "realt", "101"), listing(session, "realt", "102")
    assert b.object_id != a.object_id
    assert "соседние квартиры" in explain(session, f"listing:{b.id}", now=T0 + DAY)


def test_two_agencies_same_platform_with_photos_are_glued(session):
    # С фото (больше половины совпали) — одна квартира у двух агентств: склейка и дубль.
    other = {"seller_key": "realt-seller-2", "agency_name": "Другое агентство"}
    photos = hashes(21, 6)
    observe(session, "realt", T0, realt("201"), photos={"201": photos})
    observe(
        session,
        "realt",
        T0 + DAY,
        realt("201"),
        realt("202", **other),
        photos={"202": blurred(photos[:4]) + hashes(22, 2)},
    )
    a, b = listing(session, "realt", "201"), listing(session, "realt", "202")
    assert (b.object_id, b.match_confidence) == (a.object_id, "high")
    cross = events(session, a.object_id, with_payload=True)[-1]
    assert cross.type == "cross_listed" and cross.payload["same_source"] and cross.notify


def test_neighbour_entrance_fingerprint_without_photos_is_not_glued(session):
    # Соседние подъезды: та же улица, дом, этаж, комнаты и площадь, точки в 40 м. Отпечаток
    # уровня 1 совпадает полностью; раньше это давало medium и cross_listed same_source.
    observe(
        session,
        "realt",
        T0,
        realt("101"),
        realt("102", seller_key="realt-seller-2", agency_name=None, lat=53.90510, lon=27.47870),
    )
    a, b = listing(session, "realt", "101"), listing(session, "realt", "102")
    assert a.object_id != b.object_id
    assert events(session, a.object_id) == ["new"] and events(session, b.object_id) == ["new"]


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


# --- Ревью Глеба: ложные склейки по фото (п. 1) ----------------------------------------


def test_neighbours_sharing_facade_and_layout_photos_are_not_glued(session):
    # Соседняя квартира в том же доме: агентства выложили одинаковые фасад и типовую
    # планировку, интерьеры разные. 2 совпадения из 8 — это соседи, а не дубль.
    common = hashes("facade+plan", 2)
    observe(
        session,
        "realt",
        T0,
        realt("R1", floor=7, seller_key="r-a", agency_name="Агентство А"),
        photos={"R1": common + hashes("interior-R1", 6)},
    )
    observe(
        session,
        "kufar",
        T0 + HOUR,
        kufar("K1", seller_key="k-b"),
        photos={"K1": blurred(common) + hashes("interior-K1", 6)},
    )
    r, k = listing(session, "realt", "R1"), listing(session, "kufar", "K1")
    assert r.object_id != k.object_id
    assert "cross_listed" not in events(session, r.object_id)
    text = explain(session, f"listing:{k.id}", now=T0 + HOUR)
    assert "совпало 2 (нужно ≥ 4)" in text


def test_floorless_platform_needs_three_photos_for_high(session):
    # Kufar не отдаёт этаж: 2 из 2 совпавших фото + адрес — medium, а не high.
    photos = hashes(31, 2)
    observe(session, "realt", T0, realt("101"), photos={"101": photos})
    observe(session, "kufar", T0 + HOUR, kufar("201"), photos={"201": blurred(photos)})
    k = listing(session, "kufar", "201")
    assert k.object_id == listing(session, "realt", "101").object_id
    assert k.match_confidence == "medium"
    assert k.match_basis["location"] == "address" and k.match_basis["photo_matches"] == 2


def test_popular_photo_is_not_evidence(session):
    # Фасад агентство ставит ко всем квартирам дома: тот же хеш у 2- и 3-комнатной — значит,
    # он «типовой» и уликой не считается. Остаётся одно настоящее совпадение — мало.
    facade, plan = hashes("facade", 1), hashes("plan", 1)
    observe(
        session,
        "realt",
        T0,
        realt("101", seller_key="r-a", agency_name="А"),
        realt("109", rooms=3, area_total=71.0, floor=2, seller_key="r-a", agency_name="А"),
        photos={"101": facade + plan, "109": facade + hashes("other", 1)},
    )
    observe(
        session,
        "kufar",
        T0 + HOUR,
        kufar("201", seller_key="k-b"),
        photos={"201": facade + blurred(plan)},
    )
    r, k = listing(session, "realt", "101"), listing(session, "kufar", "201")
    # Без «типового» фасада у сторон по одному фото: это не улика и не противоречие —
    # остаётся адрес без этажа, то есть low (без событий).
    assert (k.object_id, k.match_confidence) == (r.object_id, "low")
    assert events(session, r.object_id) == ["new"]
    assert "типовых не учитываются" in explain(session, f"listing:{k.id}", now=T0 + HOUR)


def test_popular_hashes_need_conflicting_flats(session):
    from minsk_realty.matching import DEFAULT_CONFIG
    from minsk_realty.matching.features import popular_hashes

    shared = hashes("shared", 1)
    # Одна квартира на двух площадках и перевыложенная с теми же фото — не «типовое».
    observe(session, "realt", T0, realt("101"), photos={"101": shared})
    observe(session, "kufar", T0, kufar("201"), photos={"201": shared})
    assert popular_hashes(session, DEFAULT_CONFIG) == set()
    # Тот же снимок у квартиры на другом этаже — уже «типовой».
    observe(
        session,
        "realt",
        T0 + HOUR,
        realt("101"),
        realt("102", floor=8, seller_key="realt-seller-2"),
        photos={"102": shared},
    )
    assert popular_hashes(session, DEFAULT_CONFIG) == set(shared)


# --- Нехватка данных не отцепляет (п. 2) ---------------------------------------------


def test_snapshot_without_area_does_not_detach(session):
    observe(session, "realt", T0, realt("101"))
    observe(session, "kufar", T0 + HOUR, kufar("201"))
    object_id = listing(session, "realt", "101").object_id
    # Разовый сбой разбора: снимок без площади и комнат.
    stats = observe(session, "kufar", T0 + 2 * HOUR, kufar("201", area_total=None, rooms=None))
    k = listing(session, "kufar", "201")
    assert (k.object_id, k.match_confidence) == (object_id, "low")
    assert stats.detached == 0 and stats.created == 0
    observe(session, "kufar", T0 + 3 * HOUR, kufar("201"))
    assert listing(session, "kufar", "201").object_id == object_id
    assert objects_count(session) == 1
    assert events(session, object_id) == ["new"]


def test_listing_without_area_waits_before_becoming_object(session):
    observe(session, "kufar", T0, kufar("201", area_total=None))
    assert listing(session, "kufar", "201").object_id is None  # ждём данных
    observe(session, "kufar", T0 + 2 * HOUR, kufar("201"))  # площадь появилась
    observe(session, "realt", T0 + 3 * HOUR, realt("101"))
    assert objects_count(session) == 1
    # Данных так и не появилось — через сутки отдельный объект (история не теряется).
    observe(session, "kufar", T0 + 4 * HOUR, kufar("201"), kufar("202", rooms=None))
    observe(session, "kufar", T0 + 2 * DAY, kufar("201"), kufar("202", rooms=None))
    assert listing(session, "kufar", "202").object_id is not None


# --- Неподвижная точка (п. 3) ---------------------------------------------------------


def _assignments(session):
    rows = session.execute(
        select(Listing.id, Listing.object_id, Listing.match_confidence, Listing.match_basis)
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def _event_rows(session):
    rows = session.scalars(select(Event).order_by(Event.id)).all()
    return [(e.id, e.type, e.notify, e.trusted, e.retracted_at, e.payload) for e in rows]


def test_second_pass_on_same_data_changes_nothing(session):
    # Kufar склеен low с Realt. В одном проходе появляются фото у Kufar и объявление другой
    # площадки с теми же фото: новая склейка поднимает Kufar до high. Раньше это делал только
    # следующий проход (пересмотр шёл до новых склеек).
    photos = hashes(41, 4)
    observe(session, "realt", T0, realt("101"))
    observe(session, "kufar", T0 + HOUR, kufar("201"))
    assert listing(session, "kufar", "201").match_confidence == "low"
    save_photo_hashes(session, listing(session, "kufar", "201").id, blurred(photos))
    observe(
        session,
        "onliner",
        T0 + 2 * HOUR,
        realt("301", source="onliner", seller_key="onliner-1", agency_name="Третье"),
        photos={"301": photos},
    )
    assert listing(session, "kufar", "201").match_confidence == "high"
    before, events_before = _assignments(session), _event_rows(session)
    for now in (T0 + 2 * HOUR, T0 + 3 * HOUR):
        stats = process_after_run(session, None, now=now)
        assert stats.changes == 0 and sum(stats.events.values()) == 0
    stats = process_after_run(session, None, now=T0 + 3 * HOUR, full=True)
    assert stats.changes == 0 and sum(stats.events.values()) == 0
    assert _assignments(session) == before and _event_rows(session) == events_before


def test_random_market_reaches_fixed_point(session):
    # Плотный дом: много похожих квартир на двух площадках, часть с фото.
    rnd = random.Random(7)
    realt_snaps, kufar_snaps, photos = [], [], {"realt": {}, "kufar": {}}
    for i in range(40):
        rooms = rnd.choice([1, 2, 3])
        area = round({1: 38, 2: 54, 3: 70}[rooms] + rnd.uniform(-2, 2), 1)
        house = str(rnd.randint(1, 3))
        floor = rnd.randint(1, 9)
        common = {"rooms": rooms, "area_total": area, "price_amount": Decimal(50000 + i)}
        realt_snaps.append(
            realt(
                f"r{i}", house=house, floor=floor, seller_key=f"rs{i}", agency_name=None, **common
            )
        )
        kufar_snaps.append(
            kufar(
                f"k{i}",
                address_raw=f"Минск, ул. Притыцкого, {house}",
                seller_key=f"ks{i}",
                **(common | {"area_total": area + 0.3}),
            )
        )
        if rnd.random() < 0.5:
            ph = hashes(f"flat{i}", 5)
            photos["realt"][f"r{i}"] = ph
            photos["kufar"][f"k{i}"] = blurred(ph[:4]) + hashes(f"facade{house}", 1)
    observe(session, "realt", T0, *realt_snaps, photos=photos["realt"], process=False)
    observe(session, "kufar", T0, *kufar_snaps, photos=photos["kufar"], process=False)
    first = process_after_run(session, None, now=T0 + HOUR)
    assert first.changes > 0
    before, events_before = _assignments(session), _event_rows(session)
    second = process_after_run(session, None, now=T0 + HOUR)
    assert second.changes == 0 and sum(second.events.values()) == 0
    assert _assignments(session) == before and _event_rows(session) == events_before


# --- Отцепление отзывает события (п. 4) -----------------------------------------------


def test_detach_retracts_events_and_restores_without_notify(session):
    """B (onliner) склеен с A по отпечатку, C — перевыкладывание B. Потом порог площади
    ужесточили, и B отцепился: события, основанные на B, отзываются, а не висят."""
    a_kw = {"seller_key": "r-a", "agency_name": "А"}
    b_kw = {"source": "onliner", "seller_key": "o-b", "agency_name": "Б"}
    observe(session, "realt", T0, realt("A", **a_kw))
    observe(session, "onliner", T0 + HOUR, realt("B", area_total=54.9, **b_kw))
    observe(session, "onliner", T0 + 2 * DAY)  # B снято
    observe(session, "realt", T0 + 4 * DAY, realt("A", **a_kw))
    observe(
        session,
        "onliner",
        T0 + 4 * DAY,
        realt("C", price_amount=Decimal(90000), **b_kw),
    )
    observe(session, "realt", T0 + 5 * DAY)  # A снято
    observe(session, "onliner", T0 + 5 * DAY)  # C снято
    a, b, c = (
        listing(session, s, e) for s, e in [("realt", "A"), ("onliner", "B"), ("onliner", "C")]
    )
    oid = a.object_id
    assert b.object_id == oid and c.object_id == oid
    process_after_run(session, None, now=T0 + 8 * DAY)
    types = events(session, oid)
    assert types == ["new", "cross_listed", "relisted", "price_drop", "cross_listed", "removed"]

    strict = MatchingConfig(area_tolerance_m2=0.5)
    stats = process_after_run(session, None, now=T0 + 9 * DAY, config=strict, full=True)
    assert stats.detached == 1 and b.object_id != oid and c.object_id == oid
    rows = {e.dedup_key: e for e in events(session, oid, with_payload=True)}
    for key in (f"cross:{b.id}", f"relisted:{c.id}", f"price-relist:{c.id}"):
        assert rows[key].retracted_at == T0 + 9 * DAY and not rows[key].notify, key
    assert rows[f"cross:{c.id}"].retracted_at is None
    # removed ссылался на B: отозван и сразу восстановлен без B — но без уведомления.
    removed = rows[f"removed:{(T0 + 4 * DAY).isoformat()}"]
    assert removed.retracted_at is None and not removed.notify
    assert removed.payload["listing_ids"] == [a.id, c.id]
    # У B свой объект; его старые события в истории, но не в уведомлениях.
    assert [(e.type, e.notify) for e in events(session, b.object_id, with_payload=True)] == [
        ("new", False),
        ("removed", False),
    ]
    # Повтор ничего не меняет и не создаёт событий заново.
    snapshot = _event_rows(session)
    again = process_after_run(session, None, now=T0 + 9 * DAY, config=strict, full=True)
    assert again.changes == 0 and sum(again.events.values()) == 0
    assert _event_rows(session) == snapshot


# --- Старые события без уведомления (п. 5) --------------------------------------------


def test_late_link_does_not_notify_old_events(session):
    photos = hashes("p", 4)
    observe(session, "realt", T0, realt("R1"), photos={"R1": photos})
    observe(session, "kufar", T0 + DAY, kufar("K1", price_amount=Decimal(99000)), process=False)
    observe(session, "kufar", T0 + 2 * DAY, kufar("K1", price_amount=Decimal(95000)), process=False)
    observe(session, "realt", T0 + 2 * DAY, realt("R1"), process=False)
    observe(
        session,
        "kufar",
        T0 + 30 * DAY,
        kufar("K1", price_amount=Decimal(95000)),
        photos={"K1": blurred(photos)},
        process=False,
    )
    observe(session, "realt", T0 + 30 * DAY, realt("R1"))
    oid = listing(session, "realt", "R1").object_id
    assert listing(session, "kufar", "K1").object_id == oid
    rows = events(session, oid, with_payload=True)
    assert [(e.type, e.notify, e.trusted) for e in rows] == [
        ("new", True, True),
        ("cross_listed", False, True),  # одновременно висели с T0+1д — давно
        ("price_drop", False, True),  # снижение месячной давности: в историю, не в рассылку
    ]
    # Свежее снижение после склейки уведомляет как обычно.
    observe(session, "kufar", T0 + 31 * DAY, kufar("K1", price_amount=Decimal(93000)))
    assert [(e.type, e.notify) for e in events(session, oid, with_payload=True)][-1] == (
        "price_drop",
        True,
    )


def test_first_run_on_accumulated_db_is_quiet(session):
    for day in range(10):
        observe(
            session,
            "realt",
            T0 + day * DAY,
            realt("101", price_amount=Decimal(92500 - 1000 * day)),
            process=False,
        )
    process_after_run(session, None, now=T0 + 9 * DAY, full=True)
    rows = events(session, listing(session, "realt", "101").object_id, with_payload=True)
    assert [e.notify for e in rows if e.at < T0 + 7 * DAY] == [False] * 7  # new + 6 снижений
    assert all(e.notify for e in rows if e.at >= T0 + 7 * DAY)


# --- Блокировка прохода (п. 7) --------------------------------------------------------


def test_concurrent_processing_is_skipped_not_failed(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'lock.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as first:
        observe(first, "realt", T0, realt("101"), process=False)
        first.commit()
        assert not process_after_run(first, "realt", now=T0).skipped  # блокировка взята
        with factory() as second:
            stats = process_after_run(second, None, now=T0)
            assert stats.skipped and "пропущено" in str(stats)
            second.rollback()
        first.commit()  # конец транзакции снимает блокировку
    with factory() as third:
        stats = process_after_run(third, None, now=T0 + HOUR)
        assert not stats.skipped and stats.changes == 0
        third.commit()


# --- Поля объекта и улица (мелочи и п. 9) ---------------------------------------------


def test_object_fields_follow_founder_corrections(session):
    observe(session, "realt", T0, realt("101", floor=None))
    oid = listing(session, "realt", "101").object_id
    obj = session.get(RealtyObject, oid)
    assert (obj.street_tail, obj.floor, obj.fingerprint) == ("притыцкого", None, None)
    # Продавец уточнил площадь и указал этаж.
    observe(session, "realt", T0 + DAY, realt("101", area_total=56.0, floor=5))
    assert (obj.area_total, obj.floor) == (56.0, 5)
    assert obj.fingerprint == "притыцкого|10|2|5/9"


def test_street_name_variants_find_each_other(session):
    observe(
        session,
        "realt",
        T0,
        realt("101", street="Якуба Коласа ул.", house="21", address_raw="Минск, Якуба Коласа, 21"),
        photos={"101": hashes(51, 4)},
    )
    observe(
        session,
        "kufar",
        T0 + HOUR,
        kufar("201", address_raw="Минск, Сухарево-5, ул. Я. Коласа, 21, 154"),
        photos={"201": blurred(hashes(51, 4))},
    )
    k = listing(session, "kufar", "201")
    assert (k.object_id, k.match_confidence) == (listing(session, "realt", "101").object_id, "high")


def test_legacy_false_high_is_revised_on_first_pass(session):
    """БД до 0003: старые правила склеили соседей high (2 общих фото из 8) и разослали
    cross_listed. Первый проход по новым правилам отцепляет склейку и отзывает событие."""
    from minsk_realty.matching.events import generate_events

    common = hashes("facade+plan", 2)
    observe(
        session,
        "realt",
        T0,
        realt("R1", floor=7, seller_key="r-a", agency_name="Агентство А"),
        photos={"R1": common + hashes("interior-R1", 6)},
    )
    observe(
        session,
        "kufar",
        T0 + HOUR,
        kufar("K1", seller_key="k-b"),
        photos={"K1": blurred(common) + hashes("interior-K1", 6)},
        process=False,
    )
    r, k = listing(session, "realt", "R1"), listing(session, "kufar", "K1")
    oid = r.object_id
    r.match_basis = None  # склейки до миграции оснований не имеют
    k.object_id, k.match_confidence = oid, "high"
    generate_events(session, oid, MatchingConfig(), T0 + HOUR)
    [cross] = [e for e in events(session, oid, with_payload=True) if e.type == "cross_listed"]
    assert cross.notify

    stats = process_after_run(session, "kufar", now=T0 + 2 * HOUR)
    assert stats.detached == 1 and k.object_id != oid
    assert cross.retracted_at == T0 + 2 * HOUR and not cross.notify
    assert r.match_basis == {"rule": "founder"}
