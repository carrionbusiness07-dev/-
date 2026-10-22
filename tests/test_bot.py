"""Бот: разбор ссылок, тексты, список, уведомления, недельная сводка, хендлеры aiogram.

Без сети: Telegram подменён фальшивой сессией aiogram, БД — SQLite во временной папке.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
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
from minsk_realty.bot import keyboards, links, texts, weekly
from minsk_realty.bot.app import build_dispatcher
from minsk_realty.bot.config import BotConfig, ConfigError, chat_ref
from minsk_realty.bot.handlers import message_text
from minsk_realty.bot.logsafe import RedactTokenFilter, redact
from minsk_realty.bot.publish import notify_once, publish_weekly, scheduled_weekly
from minsk_realty.bot.service import BotService
from minsk_realty.db.models import (
    Base,
    ChannelPost,
    Check,
    Event,
    Listing,
    Notification,
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


def test_find_links_unsupported_and_none():
    onl = links.find_links("https://r.onliner.by/pk/apartments/301")
    assert onl and not onl[0].supported and onl[0].coming_soon == "Onliner"
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
    assert "Kufar или Realt" in text
    assert "Onliner и Domovita — скоро" in text
    assert "/watch" in text and "/list" in text


def test_html_escaping_and_telegram_length():
    assert texts.esc("<b>&") == "&lt;b&gt;&amp;"
    assert texts.tg_len("🏠") == 2  # Telegram считает в UTF-16
    long = "\n".join(f"строка {i} 🏠" for i in range(1000))
    cut = texts.fit(long)
    assert texts.tg_len(cut) <= texts.TG_LIMIT and cut.endswith("…")


def test_unsupported_link_texts():
    onl = texts.unsupported_text(links.find_links("https://r.onliner.by/pk/apartments/301"))
    assert "Onliner я пока не читаю" in onl and "Kufar и Realt" in onl
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
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
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


def _feed(service, *updates) -> list:
    async def go():
        tg = FakeTelegram()
        bot = Bot("42:TEST", session=tg)
        dp = build_dispatcher(service, CFG)
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
