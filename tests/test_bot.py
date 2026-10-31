"""Бот: разбор ссылок, тексты, список, уведомления, недельная сводка, хендлеры aiogram.

Без сети: Telegram подменён фальшивой сессией aiogram, БД — SQLite во временной папке.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendVenue
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update
from aiogram.types import User as TgUser
from alembic import command
from alembic.config import Config
from analytics_data import (
    DAY,
    HOUR,
    KUFAR_URL,
    NOW,
    REALT_URL,
    add_background,
    build_docs_scenario,
    kufar,
    observe,
    onliner,
)
from sqlalchemy import func, select

from minsk_realty.analytics import build_check_report, render_check_text
from minsk_realty.analytics.fmt import MINSK_DISTRICTS
from minsk_realty.bot import keyboards, links, texts, weekly
from minsk_realty.bot import service as service_mod
from minsk_realty.bot.app import build_dispatcher
from minsk_realty.bot.config import BotConfig, ConfigError, chat_ref
from minsk_realty.bot.handlers import message_text
from minsk_realty.bot.logsafe import RedactTokenFilter, redact
from minsk_realty.bot.publish import notify_once, publish_weekly, scheduled_weekly
from minsk_realty.bot.ratelimit import TokenBucket
from minsk_realty.bot.search import PROMPTS, parse_search_callback
from minsk_realty.bot.service import INDEX_RETRY, BotService, IndexCache, reply_venue
from minsk_realty.bot.watchlist import (
    forget_user,
    has_seen_object,
    last_search,
    mark_shown,
    save_search,
)
from minsk_realty.db.models import (
    Base,
    ChannelPost,
    Check,
    Event,
    Listing,
    Notification,
    SavedSearch,
    SearchShown,
    User,
    Watch,
)
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.matching import process_after_run

TG = 4242  # telegram_id пользователя в тестах
CFG = BotConfig(token="42:TEST", admin_chat_id=777, channel_id="@khronika_test")
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'bot.db'}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def docs(factory):
    """Квартира на Притыцкого из docs/02 (1.2) + фон для медиан."""
    with factory() as session:
        build_docs_scenario(session)
        add_background(session, 12)
        process_after_run(session, None, now=NOW, full=True)
        session.commit()
    return factory


def make_service(factory, now: datetime = NOW, cfg: BotConfig = CFG):
    clock = Clock(now)
    return BotService(factory, cfg, clock=clock), clock


def listing_id(factory, external_id: str) -> int:
    with factory() as session:
        return session.scalar(select(Listing.id).where(Listing.external_id == external_id))


def seen(factory, *listing_ids: int, tg: int = TG) -> None:
    """Пользователь уже проверял эти объявления (кнопки «Следить»/«Отчёт» доступны)."""
    with factory() as session:
        user = session.scalar(select(User).where(User.telegram_id == tg))
        if user is None:
            user = User(telegram_id=tg, created_at=NOW)
            session.add(user)
            session.flush()
        for lid in listing_ids:
            session.add(Check(user_id=user.id, url="https://x", listing_id=lid, created_at=NOW))
        session.commit()


def count(factory, model, *where) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model).where(*where))


# --- Ссылки -------------------------------------------------------------------------------


def test_find_links_in_free_text():
    found = links.find_links(
        "Глянь https://re.kufar.by/vi/minsk/kvartira/1000201?utm_source=tg, ок?"
    )
    assert len(found) == 1
    link = found[0]
    assert (link.source, link.external_id) == ("kufar", "1000201")
    assert link.clean == "https://re.kufar.by/vi/minsk/kvartira/1000201"  # без utm

    realt = links.find_links("realt.by/sale-flats/object/101/")
    assert (realt[0].source, realt[0].external_id) == ("realt", "101")


def test_find_links_onliner_domovita_and_none():
    onl = links.find_links("https://r.onliner.by/pk/apartments/301")
    assert (onl[0].source, onl[0].external_id) == ("onliner", "301")
    dom = links.find_links(
        "https://domovita.by/minsk/flats/sale/1-komnatnaa-kvartira-ul-bratskaa-42-114"
    )
    assert (dom[0].source, dom[0].external_id) == (
        "domovita",
        "1-komnatnaa-kvartira-ul-bratskaa-42-114",
    )
    assert not links.find_links("https://domovita.by/minsk/flats/sale")[0].supported
    assert links.find_links("2-комн., ул.Притыцкого, 54.3 м², т.е. недорого") == []
    search = links.find_links("https://re.kufar.by/l/minsk/kupit/kvartiru")
    assert search and not search[0].supported


def test_hidden_text_links_are_read():
    msg = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=TG, type="private"),
        text="вот эта квартира",
        entities=[MessageEntity(type="text_link", offset=4, length=3, url=KUFAR_URL)],
    )
    assert KUFAR_URL in message_text(msg)


# --- Тексты -------------------------------------------------------------------------------


def test_start_lists_current_sources():
    text = texts.start_text()
    assert "Kufar, Realt, Onliner или Domovita" in text
    assert "скоро" not in text
    assert "/watch" in text and "/list" in text


def test_html_escaping_and_telegram_length():
    assert texts.esc("<b>&") == "&lt;b&gt;&amp;"
    assert texts.tg_len("🏠") == 2  # Telegram считает в UTF-16
    long = "\n".join(f"строка {i} 🏠" for i in range(1000))
    cut = texts.fit(long)
    assert texts.tg_len(cut) <= texts.TG_LIMIT and cut.endswith("…")


def test_unsupported_link_texts():
    onl = texts.unsupported_text(links.find_links("https://r.onliner.by/pk/"))
    assert "не на объявление" in onl
    other = texts.unsupported_text(links.find_links("https://example.com/x"))
    assert "не понимаю" in other and "re.kufar.by/vi/" in other
    search = texts.unsupported_text(links.find_links("https://re.kufar.by/l/minsk"))
    assert "не на объявление" in search


def test_watch_promise_matches_leo_text(factory):
    """Бот заменяет эту строку отчёта, если следить нельзя: она должна совпадать с Leo."""
    with factory() as session:
        add_background(session, 1, start=NOW - HOUR)
        session.commit()
        lid = session.scalar(select(Listing.id))
        report = build_check_report(session, f"listing:{lid}", NOW)
    assert report.data_level == "first_time"
    assert texts.WATCH_PROMISE in render_check_text(report).split("\n")
    html = texts.report_text(report, can_watch=False)
    assert texts.WATCH_PROMISE not in html and "не получится" in html


# --- /check -------------------------------------------------------------------------------


def test_check_report_from_link(docs):
    service, _ = make_service(docs)
    reply = service.check_text(TG, f"Что скажете? {KUFAR_URL}")
    assert reply.text.startswith("<b>🏠 2-комн.")
    assert "$92 500" in reply.text and "Наблюдаем рынок с 15.07.2026" in reply.text
    assert texts.tg_len(reply.text) <= texts.TG_LIMIT
    buttons = [b for row in reply.keyboard.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["Открыть объявление", "👁 Следить"]
    assert buttons[0].url == KUFAR_URL
    assert buttons[1].callback_data == f"w:{listing_id(docs, '1000201')}"

    # Каждый /check — в журнал; о пользователе только telegram_id.
    with docs() as session:
        user = session.scalar(select(User))
        check = session.scalar(select(Check))
    assert user.telegram_id == TG
    assert set(User.__table__.columns.keys()) == {"id", "telegram_id", "created_at"}
    assert check.user_id == user.id and check.listing_id == listing_id(docs, "1000201")


def test_reply_venue(docs):
    with docs() as session:
        report = build_check_report(session, f"listing:{listing_id(docs, '1000201')}", NOW)
    loc = report.location
    assert loc is not None and not loc.approx
    venue = reply_venue(report)
    assert (venue.lat, venue.lon) == (loc.lat, loc.lon)
    assert venue.title == report.flat.street and venue.address == f"{report.flat.district} р-н"
    bare = replace(report, flat=replace(report.flat, street=None, district=None))
    assert (reply_venue(bare).title, reply_venue(bare).address) == ("Квартира", "Минск")
    approx = reply_venue(replace(report, location=replace(loc, approx=True)))
    assert approx.title == f"≈ {report.flat.street} (примерно)"
    assert approx.address == "точка площадки неточная" and (approx.lat, approx.lon) == (
        loc.lat,
        loc.lon,
    )
    assert reply_venue(replace(report, location=None)) is None


def _venue_calls(calls):
    return [c for c in calls if isinstance(c, SendMessage | SendVenue)]


def test_check_sends_venue_after_report(docs):
    service, _ = make_service(docs)
    with docs() as session:
        report = build_check_report(session, f"listing:{listing_id(docs, '1000201')}", NOW)
    for update in (_message(KUFAR_URL), _message(f"/check {KUFAR_URL}")):
        _, calls = _feed(service, update)
        assert [type(c) for c in _venue_calls(calls)] == [SendMessage, SendVenue]
        pin = next(c for c in calls if isinstance(c, SendVenue))
        assert (pin.latitude, pin.longitude) == (report.location.lat, report.location.lon)
        assert pin.title == report.flat.street and pin.address == f"{report.flat.district} р-н"


@pytest.mark.parametrize("with_message", [True, False])
def test_report_button_sends_venue(docs, with_message):
    service, _ = make_service(docs)
    with docs() as session:
        oid = session.get(Listing, listing_id(docs, "1000201")).object_id
    service.check_text(TG, KUFAR_URL)  # пользователь видел квартиру
    button = CallbackQuery(
        id="1",
        from_user=TgUser(id=TG, is_bot=False, first_name="Имя"),
        chat_instance="x",
        data=f"r:{oid}",
        message=_message("список") if with_message else None,
    )
    _, calls = _feed(service, button)
    assert [type(c) for c in _venue_calls(calls)] == [SendMessage, SendVenue]
    assert next(c for c in calls if isinstance(c, SendVenue)).chat_id == TG


def test_no_venue_without_coordinates(docs, monkeypatch):
    service, _ = make_service(docs)
    monkeypatch.setattr("minsk_realty.bot.service.reply_venue", lambda report: None)
    _, calls = _feed(service, _message(KUFAR_URL))
    assert [type(c) for c in _venue_calls(calls)] == [SendMessage]


def test_venue_failure_does_not_break_handler(docs):
    service, _ = make_service(docs)

    async def go():
        tg = FakeTelegram()
        orig = tg.make_request

        async def failing(bot, method, timeout=None):
            if isinstance(method, SendVenue):
                raise TelegramBadRequest(method=method, message="bad")
            return await orig(bot, method, timeout)

        tg.make_request = failing
        bot = Bot("42:TEST", session=tg)
        await build_dispatcher(service, CFG).feed_update(
            bot, Update(update_id=1, message=_message(KUFAR_URL))
        )

    asyncio.run(go())  # не падает


def test_check_not_found_does_not_promise_watching(docs):
    service, _ = make_service(docs)
    reply = service.check_text(TG, "https://re.kufar.by/vi/99999999")
    assert "пока нет в нашей базе" in reply.text and "только объявления о продаже" in reply.text
    assert "Следить" not in reply.text and reply.keyboard is None
    assert count(docs, Check) == 1


def test_check_unsupported_and_no_link(docs):
    service, _ = make_service(docs)
    assert "Пришлите ссылку" in service.check_text(TG, "привет").text
    reply = service.check_text(TG, "https://example.com/flat/1")
    assert "не понимаю" in reply.text and reply.keyboard is None
    assert count(docs, User) == 0  # чужие ссылки не храним и пользователя не заводим
    service.check_text(TG, "https://r.onliner.by/pk/apartments/301")
    assert count(docs, Check) == 1  # спрос на Onliner — да


def test_check_several_links_shows_first(docs):
    service, _ = make_service(docs)
    reply = service.check_text(TG, f"{KUFAR_URL} и {REALT_URL}")
    assert reply.text.startswith(texts.several_links_note())
    assert "$92 500" in reply.text


def test_report_suggest_watch_highlights_button():
    kb = keyboards.report_keyboard(
        listing_id=5,
        url="https://re.kufar.by/vi/5",
        object_id=1,
        can_watch=True,
        watching=False,
        suggest_watch=True,
    )
    assert kb.inline_keyboard[0][0].text == "👁 Следить"
    assert kb.inline_keyboard[0][0].style == "primary"
    watching = keyboards.report_keyboard(
        listing_id=5, url=None, object_id=1, can_watch=True, watching=True, suggest_watch=True
    )
    assert watching.inline_keyboard[0][0].callback_data == "u:1:r5"


def test_callback_data_roundtrip():
    assert keyboards.parse_callback("w:12").listing_id == 12
    cb = keyboards.parse_callback("u:3:r12")
    assert (cb.action, cb.object_id, cb.listing_id, cb.context) == ("unwatch", 3, 12, "r")
    assert keyboards.parse_callback("u:3:l").context == "l"
    assert keyboards.parse_callback("r:3").object_id == 3
    assert keyboards.parse_callback("f:yes").context == "yes"
    for bad in (None, "", "w:x", "u:1:zz", "zzz", "f:maybe"):
        assert keyboards.parse_callback(bad) is None


# --- Список -------------------------------------------------------------------------------


def test_watch_list_unwatch(docs):
    service, _ = make_service(docs)
    lid = listing_id(docs, "1000201")
    service.check_text(TG, KUFAR_URL)
    reply, result = service.watch_listing(TG, lid)
    assert result.status == "added" and "слежу: квартира на Притыцкого" in reply.text
    assert service.watch_listing(TG, lid)[1].status == "already"
    # Ссылка на другое объявление той же квартиры — тот же объект, не второй пункт.
    assert service.watch_text(TG, REALT_URL).text.startswith("Уже слежу")

    shown = service.watchlist(TG)
    assert "Мои квартиры: 1 из 10" in shown.text
    assert "Квартира на Притыцкого (2-комн., 54 м²)" in shown.text
    assert "$91 000 — Onliner" in shown.text  # самая низкая цена среди площадок
    oid = result.object_id
    assert [b.callback_data for b in shown.keyboard.inline_keyboard[0]] == [
        f"r:{oid}",
        f"u:{oid}:l",
    ]

    assert service.unwatch(TG, oid) is True
    assert service.unwatch(TG, oid) is False
    assert "Список пуст" in service.watchlist(TG).text


def test_watch_limit(factory):
    with factory() as session:
        add_background(session, 3)
        session.commit()
        ids = session.scalars(select(Listing.id).order_by(Listing.id)).all()
    service, _ = make_service(factory, cfg=BotConfig(token="1:x", watch_limit=2))
    seen(factory, *ids)
    assert [service.watch_listing(TG, i)[1].status for i in ids] == ["added", "added", "limit"]
    assert "это максимум" in service.watch_listing(TG, ids[2])[0].text
    assert count(factory, Watch) == 2


def test_cannot_watch_low_confidence_listing(factory):
    with factory() as session:
        add_background(session, 1)
        row = session.scalar(select(Listing))
        row.match_confidence = "low"
        session.commit()
        lid = row.id
    service, _ = make_service(factory)
    seen(factory, lid)
    reply, result = service.watch_listing(TG, lid)
    assert result.status == "not_matched" and "не уверены" in reply.text
    assert count(factory, Watch) == 0


def test_forget_deletes_everything(docs):
    service, _ = make_service(docs)
    service.check_text(TG, KUFAR_URL)
    service.watch_listing(TG, listing_id(docs, "1000201"))
    assert "удалены" in service.forget(TG).text
    assert count(docs, User) == count(docs, Watch) == count(docs, Check) == 0
    assert "нет ваших данных" in service.forget(TG).text


def test_save_search_upsert(factory):
    with factory() as session:
        assert last_search(session, TG) is None
        save_search(session, TG, {"rooms": [2], "price_max": 90000})
        save_search(session, TG, {"rooms": [3]})
        session.commit()
        assert last_search(session, TG) == {"rooms": [3]}
        assert last_search(session, TG + 1) is None
    assert count(factory, SavedSearch) == 1


def test_seen_via_search(docs):
    with docs() as session:
        oid = session.scalar(select(Listing.object_id).where(Listing.external_id == "1000201"))
        assert not has_seen_object(session, TG, oid)
        mark_shown(session, TG, [oid])
        mark_shown(session, TG, [oid, oid])  # повтор не падает на составном ключе
        session.commit()
        assert has_seen_object(session, TG, oid)
        assert not has_seen_object(session, TG + 1, oid)
        mark_shown(session, TG, [])
    assert count(docs, SearchShown) == 1


def test_forget_removes_search(docs):
    with docs() as session:
        oid = session.scalar(select(Listing.object_id).where(Listing.external_id == "1000201"))
        save_search(session, TG, {"rooms": [2]})
        mark_shown(session, TG, [oid])
        session.commit()
        assert forget_user(session, TG) is True
        session.commit()
    assert count(docs, SavedSearch) == count(docs, SearchShown) == count(docs, User) == 0


# --- Уведомления --------------------------------------------------------------------------

_keys = itertools.count()


def add_event(factory, object_id, type_, created_at, *, listing=None, **kw) -> int:
    with factory() as session:
        event = Event(
            object_id=object_id,
            listing_id=listing,
            type=type_,
            at=kw.pop("at", created_at),
            payload=kw.pop("payload", {}),
            dedup_key=f"test:{next(_keys)}",
            created_at=created_at,
            **kw,
        )
        session.add(event)
        session.commit()
        return event.id


def drop(old: int, new: int) -> dict:
    return {"old": str(old), "new": str(new), "currency": "USD", "pct": (new - old) / old * 100}


@pytest.fixture
def watched(factory):
    """Один объект, пользователь следит за ним с NOW."""
    with factory() as session:
        (oid,) = add_background(session, 1)
        session.commit()
        lid = session.scalar(select(Listing.id))
    service, clock = make_service(factory)
    seen(factory, lid)
    assert service.watch_listing(TG, lid)[1].status == "added"
    return service, clock, oid, lid


def test_notifies_new_events_once(factory, watched):
    service, clock, oid, lid = watched
    add_event(factory, oid, "price_drop", NOW - HOUR, listing=lid, payload=drop(90000, 88000))
    add_event(factory, oid, "price_drop", NOW + HOUR, listing=lid, payload=drop(89000, 87000))
    clock.now = NOW + 2 * HOUR
    out = service.prepare_notifications()
    assert len(out) == 1 and out[0].telegram_id == TG
    text = out[0].text
    assert text.startswith(
        "📉 Квартира на Тестовая Bg0 (2-комн., 50 м²) подешевела: $89 000 → $87 000 (−2.2%)"
    )
    assert "$90 000" not in text  # событие до «Следить» не присылаем
    service.finish_notifications([(out[0], "sent")])
    clock.now += 2 * HOUR
    assert service.prepare_notifications() == []  # никогда дважды


def test_skips_not_notify_retracted_untrusted_new_and_stale(factory, watched):
    service, clock, oid, lid = watched
    at = NOW + HOUR
    add_event(factory, oid, "price_drop", at, listing=lid, notify=False, payload=drop(9, 8))
    add_event(factory, oid, "price_drop", at, listing=lid, retracted_at=at, payload=drop(9, 8))
    add_event(factory, oid, "price_drop", at, listing=lid, trusted=False, payload=drop(9, 8))
    add_event(factory, oid, "new", at, listing=lid)
    clock.now = at + HOUR
    assert service.prepare_notifications() == []
    add_event(factory, oid, "removed", at + HOUR)
    clock.now = at + HOUR + CFG.notify_max_age + HOUR  # бот долго лежал: старое не шлём
    assert service.prepare_notifications() == []


def test_hourly_limit_merges_events(factory, watched):
    service, clock, oid, lid = watched
    add_event(factory, oid, "price_drop", NOW + HOUR, listing=lid, payload=drop(90000, 88000))
    clock.now = NOW + HOUR
    first = service.prepare_notifications()
    service.finish_notifications([(first[0], "sent")])

    minutes = timedelta(minutes=1)
    add_event(
        factory, oid, "price_drop", NOW + 90 * minutes, listing=lid, payload=drop(88000, 87000)
    )
    with factory() as session:  # снята: все объявления неактивны 3+ суток
        row = session.get(Listing, lid)
        row.status, row.last_seen = "inactive", NOW - 4 * DAY
        session.commit()
    add_event(factory, oid, "removed", NOW + 100 * minutes)
    clock.now = NOW + 110 * minutes
    assert service.prepare_notifications() == []  # прошло меньше часа
    clock.now = NOW + 2 * HOUR + timedelta(minutes=1)
    (merged,) = service.prepare_notifications()
    assert len(merged.event_ids) == 2
    lines = merged.text.split("\n")
    assert lines[0].endswith(":")
    assert lines[1].startswith("📉 Подешевела: $88 000 → $87 000")
    assert lines[2] == "🚫 Снята со всех площадок"
    assert lines[-1] == "Если она вернётся, я сообщу."
    assert merged.url is None  # снята: открывать нечего


def test_restart_never_resends(factory, watched):
    service, clock, oid, lid = watched
    add_event(factory, oid, "price_drop", NOW + HOUR, listing=lid, payload=drop(90000, 88000))
    clock.now = NOW + HOUR
    assert len(service.prepare_notifications()) == 1  # зарезервировали и «упали»
    assert service.recover() == 1
    clock.now += 3 * HOUR
    assert service.prepare_notifications() == []
    assert count(factory, Notification, Notification.status == "failed") == 1


def test_release_on_retry_after_and_failed_on_forbidden(factory, watched):
    service, clock, oid, lid = watched
    add_event(factory, oid, "price_drop", NOW + HOUR, listing=lid, payload=drop(90000, 88000))
    clock.now = NOW + HOUR
    calls = []

    async def busy(chat_id, text, markup):
        calls.append(chat_id)
        raise TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="", retry_after=5)

    assert asyncio.run(notify_once(busy, service)) == 0
    assert count(factory, Notification) == 0  # резерв снят — уйдёт в следующий проход

    async def blocked(chat_id, text, markup):
        raise TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="blocked")

    assert asyncio.run(notify_once(blocked, service)) == 0
    assert count(factory, Notification, Notification.status == "failed") == 1
    assert count(factory, Watch) == 0  # заблокировал бота — список убран, 403 больше не ловим
    clock.now += 2 * HOUR
    assert service.prepare_notifications() == []


def test_notify_once_sends_with_buttons(factory, watched):
    service, clock, oid, lid = watched
    add_event(factory, oid, "price_drop", NOW + HOUR, listing=lid, payload=drop(90000, 88000))
    clock.now = NOW + HOUR
    sent = []

    async def send(chat_id, text, markup):
        sent.append((chat_id, text, markup))

    assert asyncio.run(notify_once(send, service)) == 1
    chat_id, text, markup = sent[0]
    assert chat_id == TG and "подешевела" in text
    data = [b.callback_data or b.url for row in markup.inline_keyboard for b in row]
    assert data[0] == f"r:{oid}" and data[-1] == f"u:{oid}:n"
    assert count(factory, Notification, Notification.status == "sent") == 1


def test_docs_scenario_messages(docs):
    """Сообщения docs 1.3 на живой склейке Emma: снижение и перевыкладывание."""
    service, clock = make_service(docs)
    service.check_text(TG, KUFAR_URL)
    service.watch_listing(TG, listing_id(docs, "1000201"))
    clock.now = NOW + 2 * HOUR
    with docs() as session:
        observe(
            session,
            "kufar",
            clock.now,
            kufar("1000201", price_amount=Decimal(90000), price_usd=Decimal(90000)),
        )
        observe(session, "onliner", clock.now, onliner("301"))
        session.commit()
    (msg,) = service.prepare_notifications()
    assert msg.text == (
        "📉 Квартира на Притыцкого (2-комн., 54 м²) подешевела: $92 500 → $90 000 (−2.7%)\n"
        "   Это 3-е снижение за 94 дня."
    )
    assert msg.url == KUFAR_URL


def test_relisted_message_mentions_site_counter():
    from minsk_realty.bot.watchlist import FlatName

    news = texts.ObjectNews(
        name=FlatName("ул. Притыцкого", 2, 54.0, "Фрунзенский"),
        events=(
            texts.EventNews("relisted", source="kufar", amount=Decimal(90000), currency="USD"),
        ),
        days=108,
        site_days=1,
    )
    assert texts.news_text(news) == (
        "↻ Квартира на Притыцкого (2-комн., 54 м²) перевыложена: новое объявление на Kufar, "
        "цена та же ($90 000)\n   Счётчик на сайте показывает «1 день», на самом деле 108 дней."
    )
    removed = texts.ObjectNews(
        name=FlatName(None, 3, 71.5, "Советский"), events=(texts.EventNews("removed"),)
    )
    assert texts.news_text(removed) == (
        "🚫 Квартира (3-комн., 71.5 м², Советский р-н) снята со всех площадок\n"
        "   Если она вернётся, я сообщу."
    )


# --- Недельная сводка ---------------------------------------------------------------------


def test_week_boundary_and_window():
    # 22.10.2026 — четверг; понедельник 19.10 00:00 по Минску = 18.10 21:00 UTC.
    assert weekly.week_boundary(NOW) == datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
    monday_10 = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)  # 10:00 по Минску
    assert weekly.publish_window(monday_10 - HOUR, CFG) is None
    assert weekly.publish_window(monday_10, CFG) == datetime(2026, 10, 25, 21, 0, tzinfo=UTC)
    assert weekly.publish_window(monday_10 + 3 * DAY, CFG) is None


def test_weekly_published_once(docs):
    service, clock = make_service(docs, now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    posts = []

    async def send(chat_id, text, markup):
        posts.append((chat_id, text))

    result = asyncio.run(publish_weekly(send, service, CFG, scheduled=True))
    assert result.text == "Сводка 2026-W43 опубликована в канал."
    chat_id, text = posts[0]
    assert chat_id == "@khronika_test"
    assert text.startswith("<b>📊 Квартиры Минска на продажу, неделя 43</b>")
    assert texts.tg_len(text) <= texts.TG_LIMIT

    clock.now += HOUR
    asyncio.run(publish_weekly(send, service, CFG, scheduled=True))
    assert "уже опубликована" in asyncio.run(publish_weekly(send, service, CFG)).text
    assert len(posts) == 1
    asyncio.run(publish_weekly(send, service, CFG, force=True))
    assert len(posts) == 2
    assert count(docs, ChannelPost) == 1


def test_weekly_failure_alerts_admin_and_is_not_retried_by_schedule(docs):
    service, clock = make_service(docs, now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    posts = []
    channel_ok = False

    async def send(chat_id, text, markup):
        if chat_id == CFG.channel_id and not channel_ok:
            raise TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="no")
        posts.append((chat_id, text))

    result = asyncio.run(scheduled_weekly(send, service, CFG))
    assert result.status == "failed"
    assert posts == [(777, "⚠️ " + texts.esc(result.text))]  # админу — что случилось

    channel_ok = True
    clock.now += HOUR
    assert asyncio.run(scheduled_weekly(send, service, CFG)).status == "skipped"
    assert len(posts) == 1  # бот не долбит канал каждые 10 минут
    assert asyncio.run(publish_weekly(send, service, CFG)).status == "sent"  # админ повторил
    assert posts[-1][0] == "@khronika_test"


def test_weekly_preview(docs):
    service, _ = make_service(docs, now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    assert "/weekly publish" in service.weekly_preview()


# --- Хендлеры через Dispatcher (Telegram подменён) ----------------------------------------


class FakeTelegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=NOW,
                chat=Chat(
                    id=method.chat_id if isinstance(method.chat_id, int) else -100,
                    type="private",
                ),
                text=method.text,
            )
        if isinstance(method, SendVenue):
            return Message(
                message_id=len(self.calls),
                date=NOW,
                chat=Chat(id=method.chat_id, type="private"),
            )
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def close(self) -> None:
        pass


def _message(text: str, chat_id: int = TG, chat_type: str = "private") -> Message:
    return Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=TgUser(id=chat_id, is_bot=False, first_name="Имя"),
        text=text,
    )


def _feed(service, *updates, cfg: BotConfig = CFG) -> list:
    async def go():
        tg = FakeTelegram()
        bot = Bot("42:TEST", session=tg)
        dp = build_dispatcher(service, cfg)
        for i, upd in enumerate(updates):
            kind = "message" if isinstance(upd, Message) else "callback_query"
            await dp.feed_update(bot, Update(update_id=i + 1, **{kind: upd}))
        return [c for c in tg.calls if isinstance(c, SendMessage)], tg.calls

    return asyncio.run(go())


def test_handlers_start_link_and_watch_button(docs):
    service, _ = make_service(docs)
    lid = listing_id(docs, "1000201")
    button = CallbackQuery(
        id="1",
        from_user=TgUser(id=TG, is_bot=False, first_name="Имя"),
        chat_instance="x",
        data=f"w:{lid}",
        message=_message("отчёт"),
    )
    sent, calls = _feed(service, _message("/start"), _message(KUFAR_URL), button)
    assert sent[0].text == texts.start_text()
    assert sent[1].text.startswith("<b>🏠") and sent[1].reply_markup is not None
    assert "Готово, слежу" in sent[2].text
    assert count(docs, Watch) == 1
    assert any(type(c).__name__ == "EditMessageReplyMarkup" for c in calls)


def test_handlers_weekly_only_for_admin(docs):
    service, _ = make_service(docs, now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    sent, _ = _feed(service, _message("/weekly"), _message("/weekly", chat_id=777))
    assert "Пришлите ссылку" not in sent[1].text and "неделя 43" in sent[1].text
    assert "неделя" not in sent[0].text  # обычному пользователю — справка, не сводка


def test_handlers_ignore_groups(docs):
    service, _ = make_service(docs)
    sent, _ = _feed(service, _message(KUFAR_URL, chat_id=-100, chat_type="group"))
    assert sent == [] and count(docs, Check) == 0


# --- Запуск, конфиг, логи -----------------------------------------------------------------


def test_config_from_env():
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN не задан"):
        BotConfig.from_env({})
    cfg = BotConfig.from_env(
        {"TELEGRAM_BOT_TOKEN": "1:x", "CHANNEL_ID": "-100123", "BOT_WATCH_LIMIT": "5"}
    )
    assert cfg.channel_id == -100123 and cfg.watch_limit == 5 and cfg.admin_chat_id is None
    assert chat_ref("@chan") == "@chan" and chat_ref(" ") is None
    with pytest.raises(ConfigError):
        BotConfig.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "BOT_WATCH_LIMIT": "0"})


def test_token_never_reaches_logs(caplog):
    token = "123456789:AAH" + "x" * 32
    assert token not in redact(f"POST https://api.telegram.org/bot{token}/getUpdates")
    logger = logging.getLogger("test.bot.redact")
    redactor = RedactTokenFilter(token)
    caplog.handler.addFilter(redactor)
    try:
        try:
            raise RuntimeError(f"Cannot connect https://api.telegram.org/bot{token}/sendMessage")
        except RuntimeError:
            logger.exception("сбой %s", token)
    finally:
        caplog.handler.removeFilter(redactor)
    assert token not in caplog.text and "<TOKEN>" in caplog.text


# --- Миграция 0004 ------------------------------------------------------------------------


def test_0004_upgrade_downgrade(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "0003")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO users (id, telegram_id, created_at) VALUES (1, 5, '2026-10-01')")
        )
        conn.execute(sa.text("INSERT INTO objects (id, first_seen_at) VALUES (1, '2026-10-01')"))
        conn.execute(
            sa.text(
                "INSERT INTO events (id, object_id, type, at, payload, dedup_key, notify, trusted,"
                " created_at) VALUES (1, 1, 'removed', '2026-10-02', '{}', 'r', 1, 1, '2026-10-02')"
            )
        )
    command.upgrade(config, "head")
    command.check(config)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notifications (user_id, object_id, event_id, status, created_at)"
                " VALUES (1, 1, 1, 'sent', '2026-10-02')"
            )
        )
        # одно событие одному пользователю — один раз
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(
                sa.text(
                    "INSERT INTO notifications (user_id, object_id, event_id, status,"
                    " created_at) VALUES (1, 1, 1, 'pending', '2026-10-03')"
                )
            )
    command.downgrade(config, "0003")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM events")).scalar() == 1
    command.upgrade(config, "head")
    engine.dispose()


# --- Замечания Grant ----------------------------------------------------------------------


def test_cancel_during_send_is_never_resent(factory, watched):
    """1. Остановка бота посреди отправки: сообщение могло уйти — после перезапуска не шлём."""
    service, clock, oid, lid = watched
    add_event(factory, oid, "price_drop", NOW + HOUR, listing=lid, payload=drop(90000, 88000))
    clock.now = NOW + HOUR

    async def cancelled(chat_id, text, markup):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(notify_once(cancelled, service))
    assert count(factory, Notification, Notification.status == "failed") == 1
    service.recover()
    clock.now += 3 * HOUR
    assert service.prepare_notifications() == []


def test_broken_event_does_not_block_others(factory, caplog, monkeypatch):
    """2. Битый payload одного события не останавливает рассылку остальным."""
    with factory() as session:
        good, bad = add_background(session, 2)
        session.commit()
        lids = session.scalars(select(Listing.id).order_by(Listing.id)).all()
    service, clock = make_service(factory)
    seen(factory, *lids)
    for lid in lids:
        service.watch_listing(TG, lid)
    broken = {"old": "abc", "new": "88000", "currency": "USD", "pct": "abc"}
    bad_id = add_event(factory, bad, "price_drop", NOW + HOUR, listing=lids[1], payload=broken)
    add_event(factory, good, "price_drop", NOW + HOUR, listing=lids[0], payload=drop(90000, 88000))
    clock.now = NOW + HOUR
    # alembic fileConfig в тесте миграции выключает уже созданные логгеры — включаем обратно.
    monkeypatch.setattr(logging.getLogger("minsk_realty.bot.notify"), "disabled", False)
    with caplog.at_level(logging.ERROR):
        out = service.prepare_notifications()
    assert [o.object_id for o in out] == [good]
    assert f"[{bad_id}]" in caplog.text and str(TG) not in caplog.text
    with factory() as session:
        status = session.scalar(select(Notification.status).where(Notification.event_id == bad_id))
    assert status == "failed"
    clock.now += 2 * HOUR
    assert service.prepare_notifications() == []  # битое не пересобираем каждые 10 минут


def test_main_without_token_waits_instead_of_exiting(monkeypatch, caplog):
    """3. Без токена — одно сообщение и ожидание сигнала, а не выход (перезапуск Docker)."""
    from minsk_realty.bot import __main__ as entry

    waited = []
    monkeypatch.setattr(entry, "wait_for_stop", lambda: waited.append(True))
    monkeypatch.setattr(entry, "setup_logging", lambda *a: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(logging.getLogger("minsk_realty.bot"), "disabled", False)
    with caplog.at_level(logging.ERROR):
        assert entry.main() == 0
    assert waited == [True] and "TELEGRAM_BOT_TOKEN не задан" in caplog.text
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    assert entry.main() == 0 and len(waited) == 2


def test_forged_callbacks_cannot_export_reports(docs):
    """4. «r:<id>» и «w:<id>» работают только для того, что пользователь сам видел."""
    service, _ = make_service(docs)
    lid = listing_id(docs, "1000201")
    with docs() as session:
        oid = session.get(Listing, lid).object_id
        other = add_background(session, 1, prefix="oth")[0]
        session.commit()
    stale = texts.stale_button_text()
    assert service.report_for_object(TG, oid).text == stale
    assert service.report_for_object(TG, other).text == stale
    reply, result = service.watch_listing(TG, lid)
    assert result.status == "denied" and reply.text == stale
    assert count(docs, Watch) == 0 and count(docs, Check) == 0

    service.check_text(TG, KUFAR_URL)  # пользователь сам прислал ссылку
    assert service.report_for_object(TG, oid).text.startswith("<b>🏠")
    assert service.report_for_object(TG, other).text == stale  # чужой объект — нет
    assert service.watch_listing(TG, lid)[1].status == "added"
    assert service.report_for_object(TG + 1, oid).text == stale  # другой пользователь — нет


def test_rate_limit_bucket():
    now = [0.0]
    bucket = TokenBucket(burst=3, per_minute=60, clock=lambda: now[0])
    assert [bucket.hit(1) for _ in range(4)] == [True, True, True, False]
    assert bucket.hit(2) is True  # у другого пользователя своя корзина
    now[0] += 1.0  # 60 в минуту → один токен в секунду
    assert bucket.hit(1) is True and bucket.hit(1) is False


def test_rate_limit_middleware_replies_once(docs):
    service, _ = make_service(docs)
    cfg = replace(CFG, rate_burst=2, rate_per_minute=0.01)
    button = CallbackQuery(
        id="9",
        from_user=TgUser(id=TG, is_bot=False, first_name="Имя"),
        chat_instance="x",
        data="r:1",
    )
    sent, calls = _feed(service, *[_message("/start") for _ in range(5)], button, cfg=cfg)
    assert [m.text for m in sent].count(texts.esc(texts.TOO_OFTEN)) == 1
    assert len(sent) == 3  # 2 ответа на /start + одно «слишком часто», дальше — тишина
    answers = [c for c in calls if isinstance(c, AnswerCallbackQuery)]
    assert answers and answers[0].text == texts.TOO_OFTEN


def test_link_search_is_bounded():
    """5. Ссылки ищем только в первых MAX_SCAN символах: мусор не держит GIL."""
    links.find_links("прогрев")
    far = "x " * links.MAX_SCAN + KUFAR_URL
    assert links.find_links(far) == []
    for junk in ("a." * 2048, "a-" * 2048 + ".", ("ab." * 1400)[:4096], "x" * 4096):
        started = time.perf_counter()
        links.find_links(junk)
        assert time.perf_counter() - started < 0.1


def _admin_msg(chat_id: int, chat_type: str, user_id: int) -> Message:
    return Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Имя"),
        text="/weekly publish force",
    )


def test_admin_commands_only_from_admin_in_private(docs):
    """6. ADMIN_CHAT_ID — группа: участники не публикуют; админ — только в личке."""
    service, _ = make_service(docs, now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    group_cfg = replace(CFG, admin_chat_id=-100777)
    sent, _ = _feed(service, _admin_msg(-100777, "group", 5), cfg=group_cfg)
    assert sent == [] and count(docs, ChannelPost) == 0
    # Сам админ, но в группе; и чужой пользователь в личке — сводку не публикуют.
    sent, _ = _feed(service, _admin_msg(-100, "group", 777), _admin_msg(5, "private", 5))
    assert all("Сводка" not in m.text for m in sent)
    assert count(docs, ChannelPost) == 0
    sent, _ = _feed(service, _admin_msg(777, "private", 777))
    assert "Сводка 2026-W43" in sent[-1].text
    assert count(docs, ChannelPost) == 1


def test_user_and_watch_creation_are_idempotent(factory, monkeypatch):
    """7. Двойное нажатие / две ссылки нового пользователя: без IntegrityError наружу."""
    from minsk_realty.bot import watchlist

    with factory() as session:
        (oid,) = add_background(session, 1)
        user = watchlist.get_or_create_user(session, TG, NOW)
        session.commit()
        user_id = user.id
    real = watchlist.find_user
    calls = []

    def stale_find(session, telegram_id):  # параллельный запрос уже создал пользователя
        calls.append(telegram_id)
        return None if len(calls) == 1 else real(session, telegram_id)

    monkeypatch.setattr(watchlist, "find_user", stale_find)
    with factory() as session:
        assert watchlist.get_or_create_user(session, TG, NOW).id == user_id
        session.commit()
    monkeypatch.setattr(watchlist, "find_user", real)
    with factory() as session:
        assert watchlist.insert_watch(session, user_id, oid, NOW) is True
        session.commit()
    with factory() as session:
        monkeypatch.setattr(session, "get", lambda *a, **k: None)  # «не видим» чужую вставку
        assert watchlist.insert_watch(session, user_id, oid, NOW) is False
        session.commit()
    assert count(factory, Watch) == 1 and count(factory, User) == 1


def test_button_always_answered_on_error(docs):
    class Broken(BotService):
        def report_for_object(self, telegram_id, object_id):
            raise RuntimeError("boom")

    service = Broken(docs, CFG, clock=Clock(NOW))
    button = CallbackQuery(
        id="7",
        from_user=TgUser(id=TG, is_bot=False, first_name="Имя"),
        chat_instance="x",
        data="r:1",
        message=_message("уведомление"),
    )
    _, calls = _feed(service, button)
    assert any(isinstance(c, AnswerCallbackQuery) for c in calls)  # индикатор не висит


def test_weekly_stuck_pending(docs):
    """8. Падение между записью и отправкой: не «уже опубликована» навсегда."""
    service, clock = make_service(docs, now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    assert service.reserve_weekly().status == "reserved"  # и тут бот упал
    posts = []

    async def send(chat_id, text, markup):
        posts.append(text)

    busy = asyncio.run(publish_weekly(send, service, CFG))
    assert "публикуется прямо сейчас" in busy.text and posts == []
    clock.now += weekly.STALE_PENDING + timedelta(minutes=1)
    assert asyncio.run(publish_weekly(send, service, CFG)).status == "sent"
    assert len(posts) == 1
    again = asyncio.run(publish_weekly(send, service, CFG))
    assert "уже опубликована 26.10.2026" in again.text

    # После перезапуска зависшая публикация → failed и вопрос админу, а не повтор вслепую.
    service.reserve_weekly(force=True)
    assert service.recover_weekly() == ["2026-W43"]
    assert asyncio.run(scheduled_weekly(send, service, CFG)).status == "skipped"
    assert len(posts) == 1
    assert "/weekly publish" in texts.weekly_interrupted_text("2026-W43")


# --- Анкета поиска --------------------------------------------------------------------------


def sync_service(factory):
    """Сервис, у которого индекс поиска строится сразу, без фонового потока."""
    service, _ = make_service(factory)
    service.index = IndexCache(factory, run=lambda job: job())
    return service


def press(service, data: str, tg: int = TG):
    return service.search_button(tg, parse_search_callback(data))


def buttons(markup) -> dict[str, str]:
    """callback_data → текст кнопки."""
    return {b.callback_data: b.text for row in markup.inline_keyboard for b in row}


def _press_query(data: str, message_id: int = 1) -> CallbackQuery:
    msg = _message("анкета").model_copy(update={"message_id": message_id})
    return CallbackQuery(
        id="1",
        from_user=TgUser(id=TG, is_bot=False, first_name="Имя"),
        chat_instance="x",
        data=data,
        message=msg,
    )


def test_search_callback_parsing():
    assert parse_search_callback("q:b:4").value == 4
    assert parse_search_callback("q:h:new").value == "new"
    assert parse_search_callback("q:go").action == "go"
    for bad in ("q:b:5", "q:r:0", "q:d:9", "q:a:35", "q:h:x", "q:zz", "q:b:x", "w:1", None):
        assert parse_search_callback(bad) is None
    for data in (f"q:d:{len(MINSK_DISTRICTS) - 1}", "q:a:80", "q:back", "q:rep"):
        assert len(data.encode()) <= 64 and parse_search_callback(data) is not None


def test_start_has_search_button(docs):
    service, _ = make_service(docs)
    sent, _ = _feed(service, _message("/start"), _message("/help"))
    assert len(sent) == 2
    for msg in sent:
        first = msg.reply_markup.inline_keyboard[0][0]
        assert (first.text, first.callback_data) == ("🔍 Найти квартиру", "q:new")
    assert "торг" not in texts.start_text() + texts.help_text(10)


def test_wizard_steps_and_header(factory):
    service, _ = make_service(factory)
    start = service.search_start(TG)
    assert "q:b:2" in buttons(start.keyboard) and "q:rep" not in buttons(start.keyboard)
    fr, ce = MINSK_DISTRICTS.index("Фрунзенский"), MINSK_DISTRICTS.index("Центральный")
    steps = ["q:b:2", "q:r:2", "q:ok", f"q:d:{fr}", f"q:d:{ce}", "q:ok", "q:a:50"]
    replies = [press(service, data) for data in steps]
    for reply in replies:
        assert "q:back" in buttons(reply.keyboard)
    assert "q:go" not in buttons(replies[0].keyboard)  # комнаты
    assert "q:go" not in buttons(replies[2].keyboard)  # районы
    assert "q:go" in buttons(replies[5].keyboard)  # площадь
    assert "q:go" in buttons(replies[6].keyboard)  # ещё
    assert buttons(replies[1].keyboard)["q:r:2"] == "✓ 2"  # переключатель отмечен
    assert buttons(replies[4].keyboard)[f"q:d:{ce}"] == "✓ Центральный"
    assert replies[-1].text.startswith(
        "До $100 000 · 2-комн. · Фрунзенский, Центральный · от 50 м²"
    )
    more = press(service, "q:f1")
    assert buttons(more.keyboard)["q:f1"].startswith("✓")
    assert "не первый этаж" in more.text.split("\n")[0]
    stray = press(service, "q:da")  # кнопка не того шага (из старого сообщения) — без изменений
    assert stray.text == more.text and service.drafts[TG].step == "more"


def test_back_keeps_answers(factory):
    service, _ = make_service(factory)
    service.search_start(TG)
    for data in ("q:b:1", "q:r:3", "q:ok", "q:d:0"):
        press(service, data)
    rooms = press(service, "q:back")
    assert buttons(rooms.keyboard)["q:r:3"] == "✓ 3" and "q:ok" in buttons(rooms.keyboard)
    budget = press(service, "q:back")
    assert buttons(budget.keyboard)["q:b:1"].startswith("✓")
    assert budget.text.startswith("До $80 000 · 3-комн. · Заводской")
    again = press(service, "q:b:1")
    assert buttons(again.keyboard)["q:r:3"] == "✓ 3"
    district = press(service, "q:ok")
    assert buttons(district.keyboard)["q:d:0"] == "✓ Заводской"


def test_custom_budget(factory):
    service, _ = make_service(factory)
    service.search_start(TG)
    custom = press(service, "q:bc")
    assert list(buttons(custom.keyboard)) == ["q:back"]
    # Не сумма — тот же шаг с просьбой повторить (Review Focus 1).
    for junk in ("abc", "0", "-5", "сто тысяч"):
        again = service.search_text(TG, junk)
        assert texts.BUDGET_RETRY in again.text and list(buttons(again.keyboard)) == ["q:back"]
        assert texts.esc(PROMPTS["budget_custom"]) not in again.text
        assert service.drafts[TG].step == "budget_custom"
    rooms = service.search_text(TG, "70-90")
    assert rooms.text.startswith("$70 000–90 000") and "q:r:1" in buttons(rooms.keyboard)
    assert service.search_text(TG, "85") is None  # уже не шаг «своя сумма»
    assert service.search_text(TG + 1, "85") is None  # анкеты нет

    press(service, "q:back")
    press(service, "q:bc")
    assert service.search_text(TG, "$85 000").text.startswith("До $85 000")

    press(service, "q:back")
    press(service, "q:bc")
    _, calls = _feed(service, _message("hello"))  # не сумма → шаг с просьбой повторить
    retry = [c for c in calls if isinstance(c, SendMessage | EditMessageText)]
    assert retry and texts.BUDGET_RETRY in retry[0].text
    assert service.drafts[TG].step == "budget_custom"


def test_custom_budget_edits_wizard_message(factory):
    service, _ = make_service(factory)
    service.search_start(TG)
    service.search_bind(TG, 5)
    press(service, "q:bc")
    sent, calls = _feed(service, _message("90"))
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert edit.message_id == 5 and edit.text.startswith("До $90 000")
    assert sent == []


def test_stale_button(factory):
    service, _ = make_service(factory)
    stale = press(service, "q:r:2")
    assert stale.text == texts.search_stale_text()
    assert "Кнопка устарела, начните поиск заново" in stale.text
    assert buttons(stale.keyboard) == {"q:new": "🔍 Найти квартиру"}
    assert service.search_button(TG, None).text == stale.text
    page = service.search_go(TG)
    assert page.summary == stale.text and buttons(page.more) == {"q:new": "🔍 Найти квартиру"}
    assert service.search_go(TG, repeat=True).summary == stale.text  # и повторять нечего

    _, calls = _feed(service, _press_query("q:ok"), _press_query("q:zzz"))  # без исключений
    assert sum(isinstance(c, AnswerCallbackQuery) for c in calls) == 2
    edits = [c for c in calls if isinstance(c, EditMessageText)]
    assert len(edits) == 2 and all(c.text == stale.text for c in edits)


def test_wizard_through_handlers(factory):
    service = sync_service(factory)
    sent, calls = _feed(
        service, _message("/search"), _press_query("q:b:0", message_id=1), _press_query("q:go")
    )
    assert "q:b:0" in buttons(sent[0].reply_markup)
    assert service.drafts[TG].message_id == 1
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert edit.text.startswith("До $60 000") and "q:r:1" in buttons(edit.reply_markup)
    assert sent[-1].text == texts.esc("Ничего не нашёл.")  # пустая база

    sent, _ = _feed(service, _press_query("q:new"))  # новое сообщение, а не правка старого
    assert "q:rep" in buttons(sent[0].reply_markup)


def test_link_during_wizard(docs):
    service, _ = make_service(docs)
    service.search_start(TG)
    press(service, "q:b:2")
    press(service, "q:back")
    press(service, "q:bc")
    assert service.search_text(TG, f"глянь {KUFAR_URL}") is None
    sent, _ = _feed(service, _message(f"глянь {KUFAR_URL}"))
    assert sent[0].text.startswith("<b>🏠")  # обычный отчёт /check
    draft = service.drafts[TG]
    assert draft.step == "budget_custom" and draft.query.price_max == 100_000


def test_repeat_last_search(docs):
    service = sync_service(docs)
    service.search_start(TG)
    for data in ("q:b:2", "q:r:2", "q:ok", "q:da"):
        press(service, data)
    page = service.search_go(TG)
    assert page.summary and page.cards == [] and page.more is None
    label = "До $100 000 · 2-комн."
    with docs() as session:
        assert last_search(session, TG)["price_max"] == 100_000

    start = service.search_start(TG)
    first = start.keyboard.inline_keyboard[0][0]
    assert (first.callback_data, first.text) == ("q:rep", f"Повторить: {label}")
    assert service.drafts[TG].query.label() == "Любые квартиры"  # новая анкета с нуля

    restarted = sync_service(docs)  # бот перезапустился: черновиков нет, запрос в БД
    again = restarted.search_go(TG, repeat=True)
    assert again.summary == page.summary
    assert restarted.drafts[TG].query.label() == label


def test_search_waits_for_first_index(docs):
    jobs = []
    service, _ = make_service(docs)
    service.index = IndexCache(docs, run=jobs.append)
    service.search_start(TG)
    page = service.search_go(TG)
    assert page.summary == texts.search_preparing_text()
    assert "Готовлю данные, попробуйте через минуту" in page.summary
    assert len(jobs) == 1
    jobs.pop()()
    assert service.search_go(TG).summary.startswith("Нашёл")


def test_index_cache_rebuilds_in_background(factory, monkeypatch, caplog):
    version = ["v1"]
    builds = iter([("a",), ("b",)])

    def fake_build(data, baseline, at):
        result = next(builds, None)
        if result is None:
            raise RuntimeError("boom")
        return result

    monkeypatch.setattr(service_mod, "data_version", lambda session: version[0])
    monkeypatch.setattr(service_mod, "build_index", fake_build)
    # alembic fileConfig в тесте миграции выключает уже созданные логгеры — включаем обратно.
    monkeypatch.setattr(logging.getLogger("minsk_realty.bot.service"), "disabled", False)
    jobs = []
    cache = IndexCache(factory, run=jobs.append)
    with factory() as session:
        assert cache.get(session, NOW) is None and len(jobs) == 1
        assert cache.get(session, NOW) is None and len(jobs) == 1  # одна сборка за раз
        jobs.pop()()
        assert cache.get(session, NOW) == ("a",) and jobs == []
        version[0] = "v2"
        assert cache.get(session, NOW) == ("a",) and len(jobs) == 1  # старый, пока строится
        assert cache.get(session, NOW) == ("a",) and len(jobs) == 1
        jobs.pop()()
        assert cache.get(session, NOW) == ("b",) and jobs == []
        version[0] = "v3"
        cache.get(session, NOW)
        with caplog.at_level(logging.ERROR):
            jobs.pop()()  # сборка упала
        assert "индекс поиска" in caplog.text
        assert cache.get(session, NOW) == ("b",) and jobs == []  # старый, повтор не сразу
        assert cache.get(session, NOW + INDEX_RETRY - HOUR / 60) == ("b",) and jobs == []
        assert cache.get(session, NOW + INDEX_RETRY) == ("b",) and len(jobs) == 1


def test_wizard_steps_not_rate_limited(factory):
    """Кнопки шагов анкеты не тратят лимит; «Показать квартиры» и прочее — тратят."""
    service = sync_service(factory)
    service.search_start(TG)
    press(service, "q:b:0")
    cfg = replace(CFG, rate_burst=2, rate_per_minute=0.01)
    taps = [_press_query(f"q:r:{1 + i % 4}") for i in range(6)]
    gos = [_press_query("q:go") for _ in range(3)]
    sent, calls = _feed(service, *taps, *gos, cfg=cfg)
    assert sum(isinstance(c, EditMessageText) for c in calls) == 6  # все нажатия шагов
    assert len(sent) == 2  # две выдачи, третья — «слишком часто»
    answers = [c.text for c in calls if isinstance(c, AnswerCallbackQuery)]
    assert answers.count(texts.TOO_OFTEN) == 1
