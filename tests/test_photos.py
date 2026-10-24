import builtins
import io
import logging
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select

import minsk_realty.collector.__main__ as main
from minsk_realty.collector import photos as step
from minsk_realty.db.models import Base, Listing, PhotoHash, RealtyObject, Snapshot
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.health.check import AlertThrottle
from minsk_realty.matching.photos import (
    count_photo_matches,
    hamming,
    is_informative,
    phash_from_bytes,
)


def picture(seed: int, size=(640, 480)) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    draw = ImageDraw.Draw(img)
    for _ in range(25):
        x0, y0 = rnd.randrange(size[0]), rnd.randrange(size[1])
        x1, y1 = x0 + rnd.randrange(40, 300), y0 + rnd.randrange(40, 300)
        draw.rectangle((x0, y0, x1, y1), fill=tuple(rnd.randrange(256) for _ in range(3)))
    return img


def to_bytes(img: Image.Image, fmt="JPEG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def test_phash_survives_recompression_and_resize():
    original = picture(1)
    a = phash_from_bytes(to_bytes(original, quality=95))
    # Другая площадка: уменьшила, пережала и поставила водяной знак.
    other = original.resize((400, 300))
    ImageDraw.Draw(other).text((10, 10), "kufar.by", fill=(255, 255, 255))
    b = phash_from_bytes(to_bytes(other, quality=60))
    assert len(a) == 16 and is_informative(a)
    assert hamming(a, b) <= 8


def test_different_photos_are_far_apart():
    hashes = [phash_from_bytes(to_bytes(picture(seed))) for seed in range(2, 6)]
    assert all(hamming(x, y) > 8 for i, x in enumerate(hashes) for y in hashes[i + 1 :])


def test_bad_bytes_raise_value_error():
    with pytest.raises(ValueError):
        phash_from_bytes(b"not an image")


def test_uniform_placeholder_is_not_informative():
    placeholder = phash_from_bytes(to_bytes(Image.new("RGB", (300, 200), (200, 200, 200))))
    assert not is_informative(placeholder)
    # Две заглушки «нет фото» не должны считаться совпавшими фото.
    assert count_photo_matches([placeholder], [placeholder], 8) == 0


def test_matches_are_one_to_one():
    h = phash_from_bytes(to_bytes(picture(7)))
    near = f"{int(h, 16) ^ 0b111:016x}"  # 3 бита отличаются
    # Одно фото фасада против трёх почти одинаковых снимков — одно совпадение, не три.
    assert count_photo_matches([h], [near, h, near], 8) == 1
    assert count_photo_matches([h, near], [h, near], 8) == 2


# --- шаг хешей фото в сборщике (collector/photos.py) ---

SINCE = datetime(2026, 10, 24, tzinfo=UTC)
OLD = SINCE - timedelta(days=30)


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'photos.db'}")
    Base.metadata.create_all(engine)
    step._NO_HASHES.clear()
    yield make_session_factory(engine)
    step._NO_HASHES.clear()


def add_listing(
    factory,
    *,
    source="kufar",
    obj=False,
    conf=None,
    first_seen=OLD,
    rooms=2,
    floor=3,
    area=50.0,
    n_photos=3,
    status="active",
):
    with factory() as session:
        object_id = None
        if obj:
            ro = RealtyObject()
            session.add(ro)
            session.flush()
            object_id = ro.id
        n = session.scalar(select(func.count()).select_from(Listing)) + 1
        listing = Listing(
            source=source,
            external_id=str(n),
            url=f"https://x/{n}",
            object_id=object_id,
            match_confidence=conf,
            status=status,
            first_seen=first_seen,
            last_seen=first_seen,
        )
        session.add(listing)
        session.flush()
        urls = [f"https://img.test/{source}/{n}/{i}.jpg" for i in range(n_photos)]
        session.add(
            Snapshot(
                listing_id=listing.id,
                seen_at=first_seen,
                content_hash="v1:t",
                rooms=rooms,
                floor=floor,
                area_total=area,
                extra={"photo_urls": urls} if urls else {},
            )
        )
        session.commit()
        return listing.id


def ok(request):
    seed = sum(map(ord, str(request.url)))  # своя картинка на каждую ссылку
    jpeg = to_bytes(picture(seed, size=(200, 150)))
    return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg)


def client(handler, calls=None):
    def wrapped(request):
        if calls is not None:
            calls.append(str(request.url))
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    return step.PhotoClient(transport=transport, sleep=lambda s: None, pause=(0, 0))


def backlog_ids(factory, limit=100):
    with factory() as session:
        backlog = step.photo_backlog(session, "kufar", limit_listings=limit, since=SINCE)
    return [listing_id for listing_id, _ in backlog]


def test_backlog_priority_groups(db):
    new = add_listing(db, obj=True, conf="high", first_seen=SINCE + timedelta(hours=1), rooms=4)
    plain = add_listing(db, obj=True, conf="high", rooms=4)  # ни в одну группу
    partner = add_listing(db, obj=True, conf="high", floor=None, area=50.8)
    low = add_listing(db, obj=True, conf="low", area=50)
    unmatched = add_listing(db, rooms=3, floor=5, area=70)
    # Нерешённый двойник на другой площадке тоже делает объявление kufar кандидатом.
    add_listing(db, source="realt", rooms=1, floor=None, area=30)
    cross = add_listing(db, obj=True, conf="medium", rooms=1, floor=7, area=30.9)
    far = add_listing(db, obj=True, conf="high", rooms=3, floor=6, area=70)  # другой этаж
    add_listing(db, n_photos=0)  # нет ссылок — не берём
    add_listing(db, status="inactive")  # снято — не берём
    order = backlog_ids(db)
    assert order[:2] == [unmatched, low]
    assert set(order[2:4]) == {partner, cross} and order[4:] == [new]
    assert plain not in order and far not in order
    assert backlog_ids(db, limit=2) == [unmatched, low]


def test_hashes_at_most_six_per_listing_and_within_budget(db):
    for _ in range(5):
        add_listing(db, n_photos=10)
    calls = []
    stats = step.hash_photos(db, "kufar", budget=14, client=client(ok, calls), since=SINCE)
    # Объявление берётся целиком (6 фото) или никак: 6 + 6 = 12 ≤ 14, третье не влезает.
    assert len(calls) == 12 and (stats.listings, stats.photos, stats.hashes) == (2, 12, 12)
    assert stats.failed == 0 and not stats.refused
    assert {url.rsplit("/", 1)[-1] for url in calls} == {f"{i}.jpg" for i in range(6)}


def test_listing_with_hashes_is_not_taken_again(db):
    listing = add_listing(db)
    step.hash_photos(db, "kufar", client=client(ok), since=SINCE)
    with db() as session:
        assert session.scalar(select(func.count()).select_from(PhotoHash)) == 3
    calls = []
    stats = step.hash_photos(db, "kufar", client=client(ok, calls), since=SINCE)
    assert calls == [] and stats.listings == 0 and listing not in backlog_ids(db)


def test_404_non_image_and_broken_bytes_are_skipped(db):
    add_listing(db, n_photos=4)

    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "0.jpg":
            return httpx.Response(404)
        if name == "1.jpg":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")
        if name == "2.jpg":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"junk")
        return ok(request)

    stats = step.hash_photos(db, "kufar", client=client(handler), since=SINCE)
    assert (stats.photos, stats.hashes, stats.failed) == (2, 1, 3)


def test_too_large_image_is_cut_off(db, monkeypatch):
    add_listing(db, n_photos=1)
    monkeypatch.setattr(step, "MAX_PHOTO_BYTES", 1000)
    stats = step.hash_photos(db, "kufar", client=client(ok), since=SINCE)
    assert (stats.photos, stats.hashes, stats.failed) == (0, 0, 1)


def test_listing_without_any_hash_is_skipped_until_restart(db):
    listing = add_listing(db, n_photos=2)
    step.hash_photos(db, "kufar", client=client(lambda r: httpx.Response(404)), since=SINCE)
    assert listing not in backlog_ids(db)
    step._NO_HASHES.clear()  # перезапуск процесса
    assert listing in backlog_ids(db)


def test_three_refusals_stop_the_source_for_this_pass(db):
    add_listing(db, n_photos=6)
    add_listing(db, n_photos=6)
    calls = []
    refuse = client(lambda r: httpx.Response(403), calls)
    stats = step.hash_photos(db, "kufar", client=refuse, since=SINCE)
    assert stats.refused and len(calls) == 3 and stats.hashes == 0
    assert backlog_ids(db) != []  # не помечены как «без фото»: следующий проход попробует


@pytest.fixture
def cycle(db, monkeypatch):
    """run_cycle без настоящего сбора: площадка «прошла», дальше шаг фото и склейка."""
    order, alerts = [], []

    class Adapter:
        def close(self):
            pass

    monkeypatch.setattr(main, "ADAPTERS", {"kufar": Adapter})
    monkeypatch.setattr(main, "collect", lambda adapter, factory: SimpleNamespace(status="ok"))
    monkeypatch.setattr(main, "process_after_run", lambda session, src: order.append("match"))
    monkeypatch.setattr(main, "notify", alerts.append)
    monkeypatch.setattr(main, "check_sources", lambda session, sources: [])
    monkeypatch.setattr(main, "ping_watchdog", lambda **kw: None)
    monkeypatch.delenv("PHOTO_HASHING", raising=False)
    return SimpleNamespace(order=order, alerts=alerts, db=db)


def test_cycle_hashes_before_matching_and_alerts_once_on_refusal(cycle, monkeypatch):
    def refused(factory, source):
        cycle.order.append("photos")
        return step.PhotoStats(refused=True)

    monkeypatch.setattr(main, "hash_photos", refused)
    throttle = AlertThrottle()
    main.run_cycle(cycle.db, ["kufar"], set(), throttle)
    main.run_cycle(cycle.db, ["kufar"], set(), throttle)
    assert cycle.order == ["photos", "match", "photos", "match"]
    assert len(cycle.alerts) == 1 and "фото" in cycle.alerts[0]


def test_cycle_still_matches_when_photo_step_fails(cycle, monkeypatch):
    def boom(factory, source):
        raise RuntimeError("photo step crashed")

    monkeypatch.setattr(main, "hash_photos", boom)
    main.run_cycle(cycle.db, ["kufar"], set(), AlertThrottle())
    assert cycle.order == ["match"]


@pytest.mark.parametrize("value", ["0", "false", "No"])
def test_kill_switch_downloads_nothing(cycle, monkeypatch, value):
    add_listing(cycle.db)
    calls = []
    monkeypatch.setattr(step.PhotoClient, "get", lambda self, url, **kw: calls.append(url))
    monkeypatch.setenv("PHOTO_HASHING", value)
    main.run_cycle(cycle.db, ["kufar"], set(), AlertThrottle())
    assert calls == [] and cycle.order == ["match"]


def test_cycle_enabled_by_default_downloads(cycle, monkeypatch):
    add_listing(cycle.db, n_photos=1)
    calls = []

    def get(self, url, **kw):
        calls.append(url)
        return ok(httpx.Request("GET", url))

    monkeypatch.setattr(step.PhotoClient, "get", get)
    monkeypatch.setattr(step.PhotoClient, "pause", lambda self: None)
    main.run_cycle(cycle.db, ["kufar"], set(), AlertThrottle())
    assert len(calls) == 1


def test_photo_step_writes_no_files(db, tmp_path, monkeypatch):
    add_listing(db, n_photos=6)
    before = {p.name for p in tmp_path.rglob("*")}
    writes = []
    real_open = builtins.open

    def spy(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            writes.append(file)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    stats = step.hash_photos(db, "kufar", client=client(ok), since=SINCE)
    assert stats.hashes == 6 and writes == []
    assert {p.name for p in tmp_path.rglob("*")} - before <= {"photos.db-journal"}


def collector_logs(monkeypatch, caplog):
    # Другие тесты настраивают logging и могут выключить уже созданные логгеры.
    monkeypatch.setattr(logging.getLogger("minsk_realty.collector"), "disabled", False)
    caplog.set_level(logging.DEBUG, logger="minsk_realty.collector")


def test_unexpected_photo_errors_are_counted_and_pass_continues(db, caplog, monkeypatch):
    broken = add_listing(db, n_photos=3)
    healthy = add_listing(db, n_photos=2)
    with db() as session:
        snap = session.scalar(select(Snapshot).where(Snapshot.listing_id == broken))
        snap.extra = {
            "photo_urls": [
                "http://exa\tmple.test/0.jpg",  # httpx.InvalidURL
                "https://img.test/bomb.jpg",  # DecompressionBombError
                "https://img.test/torn.jpg",  # httpx.StreamError посреди тела
            ]
        }
        session.commit()
    real_phash = step.phash_from_bytes

    def phash(data):
        if data == b"BOMB":
            raise Image.DecompressionBombError("https://img.test/bomb.jpg too big")
        return real_phash(data)

    def torn():
        yield b"\xff\xd8"
        raise httpx.StreamError("https://img.test/torn.jpg closed")

    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "bomb.jpg":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"BOMB")
        if name == "torn.jpg":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=torn())
        return ok(request)

    monkeypatch.setattr(step, "phash_from_bytes", phash)
    collector_logs(monkeypatch, caplog)
    stats = step.hash_photos(db, "kufar", client=client(handler), since=SINCE)
    assert (stats.listings, stats.failed, stats.hashes) == (2, 3, 2)
    assert broken in step._NO_HASHES and healthy not in backlog_ids(db)
    assert "kufar: фото — объявлений 2, скачано 3, хешей 2, ошибок 3" in caplog.text
    assert "img.test" not in caplog.text and "mple" not in caplog.text


def test_cycle_logs_photo_step_crash_without_url(cycle, monkeypatch, caplog):
    def boom(factory, source):
        raise httpx.InvalidURL("bad https://img.test/secret.jpg")

    monkeypatch.setattr(main, "hash_photos", boom)
    collector_logs(monkeypatch, caplog)
    main.run_cycle(cycle.db, ["kufar"], set(), AlertThrottle())
    assert cycle.order == ["match"] and "InvalidURL" in caplog.text
    assert "img.test" not in caplog.text


def test_redirect_chain_over_limit_is_failed(db):
    add_listing(db, n_photos=1)
    calls = []

    def handler(request):  # бесконечная цепочка переходов
        return httpx.Response(302, headers={"location": f"{request.url}x"})

    stats = step.hash_photos(db, "kufar", client=client(handler, calls), since=SINCE)
    assert (stats.photos, stats.failed) == (0, 1)
    assert len(calls) == 1 + step.PHOTO_MAX_REDIRECTS


def test_slow_drip_body_past_deadline_is_failed(db):
    add_listing(db, n_photos=1)
    drip = client(
        lambda r: httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=iter([b"x"] * 100)
        )
    )
    ticks = iter(range(0, 10_000, 5))  # каждый взгляд на часы — плюс 5 с
    drip.clock = lambda: next(ticks)
    stats = step.hash_photos(db, "kufar", client=drip, since=SINCE)
    assert (stats.photos, stats.failed) == (0, 1)


def test_pass_time_cap_stops_before_next_listing(db):
    add_listing(db, n_photos=2)
    add_listing(db, n_photos=2)
    calls = []
    # Старт, проверка перед первым объявлением, перед вторым — уже за пределом.
    clock = iter([0, 0, step.PHOTO_PASS_SECONDS + 1]).__next__
    stats = step.hash_photos(db, "kufar", client=client(ok, calls), since=SINCE, clock=clock)
    assert stats.out_of_time and stats.listings == 1 and stats.hashes == 2 and len(calls) == 2


def timeout(request):
    raise httpx.ConnectTimeout("timed out", request=request)


def test_three_network_errors_stop_the_step_without_alert_or_mark(cycle, monkeypatch):
    first = add_listing(cycle.db, n_photos=2)
    second = add_listing(cycle.db, n_photos=2)
    calls = []
    stats = step.hash_photos(cycle.db, "kufar", client=client(timeout, calls), since=SINCE)
    assert stats.offline and not stats.refused and len(calls) == 3
    assert first not in step._NO_HASHES and second not in step._NO_HASHES

    monkeypatch.setattr(step, "PhotoClient", lambda pause: client(timeout))
    main.run_cycle(cycle.db, ["kufar"], set(), AlertThrottle())
    assert cycle.alerts == [] and cycle.order == ["match"]
    assert backlog_ids(cycle.db) == [first, second]


def test_network_errors_not_in_a_row_do_not_stop(db):
    add_listing(db, n_photos=3)
    add_listing(db, n_photos=2)
    script = iter([timeout, timeout, ok, timeout, ok])
    stats = step.hash_photos(db, "kufar", client=client(lambda r: next(script)(r)), since=SINCE)
    assert not stats.offline and (stats.listings, stats.failed, stats.hashes) == (2, 3, 2)


@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_no_photo_step_after_unsuccessful_pass(cycle, monkeypatch, status):
    monkeypatch.setattr(main, "collect", lambda adapter, factory: SimpleNamespace(status=status))
    monkeypatch.setattr(main, "hash_photos", lambda *a, **kw: cycle.order.append("photos"))
    main.run_cycle(cycle.db, ["kufar"], set(), AlertThrottle())
    assert cycle.order == ["match"]
