"""Тексты бота. Всё, что уходит в Telegram, проходит здесь: HTML-экранирование, лимит
4096 символов, деньги как «$92 500» (форматирование — `analytics.fmt`, как в отчёте Leo).

Тексты на «вы», как отчёт /check. Цифры не считаем: берём из структур аналитики.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from minsk_realty.analytics import CheckReport, WeeklySummary, render_check_text
from minsk_realty.analytics.fmt import (
    SOURCE_NAMES,
    date_full,
    date_short,
    days_text,
    money,
    pct,
)
from minsk_realty.analytics.render import render_weekly_text
from minsk_realty.bot.links import SOURCE_DOMAINS, Link
from minsk_realty.bot.watchlist import FlatName, WatchItem, WatchResult
from minsk_realty.sources import names_text

TG_LIMIT = 4096
EXAMPLES = ("https://re.kufar.by/vi/123456789", "https://realt.by/sale-flats/object/1234567/")
SOURCES_TEXT = names_text("или")  # «Kufar, Realt, Onliner или Domovita»
AND_TEXT = names_text("и")  # «Kufar, Realt, Onliner и Domovita»

# Строка из отчёта Leo для «видим впервые». Если следить нельзя, бот её заменяет.
WATCH_PROMISE = "Нажмите «Следить» — сообщу, если цена изменится или объявление снимут."


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def tg_len(text: str) -> int:
    """Длина так, как её считает Telegram: в UTF-16 (эмодзи — 2 единицы)."""
    return len(text.encode("utf-16-le")) // 2


def fit(text: str, limit: int = TG_LIMIT) -> str:
    """Обрезать по границе строки, чтобы влезть в одно сообщение."""
    if tg_len(text) <= limit:
        return text
    tail = "\n…"
    out: list[str] = []
    used = tg_len(tail)
    for line in text.split("\n"):
        size = tg_len(line) + 1
        if used + size > limit:
            break
        out.append(line)
        used += size
    return "\n".join(out) + tail


def source_name(source: str | None) -> str:
    return SOURCE_NAMES.get(source or "", source or "")


# --- Статичные экраны -----------------------------------------------------------------------

START = (
    "Привет! Я показываю историю квартиры по ссылке на объявление.\n"
    "\n"
    f"Пришлите ссылку с {SOURCES_TEXT}, и я расскажу:\n"
    "• сколько квартира реально продаётся\n"
    "• как менялась цена\n"
    "• есть ли она на других площадках дешевле\n"
    "• стоит ли торговаться\n"
    "\n"
    "/watch — следить за квартирой   /list — мои квартиры"
)


def start_text() -> str:
    return esc(START)


def help_text(watch_limit: int) -> str:
    lines = [
        "<b>Как пользоваться</b>",
        (
            f"Пришлите ссылку на объявление о продаже квартиры в Минске с {SOURCES_TEXT} — "
            "покажу, сколько она на самом деле продаётся, как менялась цена и стоит ли торговаться."
        ),
        "",
        "/check ссылка — отчёт по объявлению (можно просто прислать ссылку)",
        "/watch ссылка — следить за квартирой (или кнопка «Следить» под отчётом)",
        "/list — мои квартиры",
        "/unwatch ссылка — перестать следить (или кнопка «Не следить» в /list)",
        "/forget — удалить мои данные",
        "",
        (
            f"В списке может быть до {watch_limit} квартир. Пишу, когда квартира дешевеет, "
            "дорожает, её перевыкладывают, снимают или она возвращается в продажу — не чаще "
            "раза в час по одной квартире."
        ),
        "",
        f"Понимаю объявления {AND_TEXT}.",
        (
            "Я храню только ваш Telegram ID, список квартир и ссылки, которые вы проверяли. "
            "Телефоны продавцов не показываю: связаться с продавцом можно через объявление "
            "на площадке."
        ),
    ]
    return "\n".join(esc(line) if not line.startswith("<b>") else line for line in lines)


def no_link_text() -> str:
    return esc(
        f"Пришлите ссылку на объявление о продаже квартиры с {SOURCES_TEXT} — покажу его "
        "историю. Например:\n" + "\n".join(EXAMPLES) + "\n\nВсе команды: /help"
    )


def unsupported_text(links: Sequence[Link]) -> str:
    coming = next((link.coming_soon for link in links if link.coming_soon), None)
    examples = "\n".join(EXAMPLES)
    if coming:
        text = (
            f"{coming} я пока не читаю — скоро добавлю. Сейчас понимаю объявления о продаже "
            f"квартир на {AND_TEXT}, например:\n{examples}"
        )
    elif any(link.host.endswith(SOURCE_DOMAINS) for link in links):
        text = (
            "Это ссылка не на объявление. Откройте само объявление о продаже квартиры и пришлите "
            f"ссылку на него, например:\n{examples}"
        )
    else:
        text = (
            f"Эту ссылку я не понимаю. Я работаю с объявлениями о продаже квартир в Минске на "
            f"{AND_TEXT}, например:\n{examples}"
        )
    return esc(text)


def check_usage_text() -> str:
    return esc(
        "Пришлите ссылку на объявление: /check https://re.kufar.by/vi/123456789\n"
        "Или просто отправьте ссылку сообщением."
    )


def several_links_note() -> str:
    return esc("В сообщении несколько ссылок — показываю первую. Остальные пришлите отдельно.")


# --- Отчёт /check ---------------------------------------------------------------------------

NOT_FOUND_NOTE = "Я собираю только объявления о продаже квартир в Минске."
CANT_WATCH_NOTE = (
    "Следить за этим объявлением пока не получится: мы не уверены, к какой квартире оно "
    "относится. Пришлите ссылку ещё раз через несколько дней."
)


def report_text(report: CheckReport, *, can_watch: bool, prefix: str = "") -> str:
    """Отчёт Leo в HTML для Telegram: экранирование, жирный заголовок, ≤ 4096 символов.

    Если не влезает, сокращаем историю цены (сначала до 5, потом до 3 строк), затем режем
    по строкам."""
    text = ""
    for max_history in (8, 5, 3):
        text = _report_html(report, can_watch, prefix, max_history)
        if tg_len(text) <= TG_LIMIT:
            return text
    return fit(text)


def _report_html(report: CheckReport, can_watch: bool, prefix: str, max_history: int) -> str:
    body = render_check_text(report, max_history=max_history)
    lines = body.split("\n")
    if report.status == "not_found":
        lines.insert(-1, NOT_FOUND_NOTE)
    if report.status == "ok" and not can_watch:
        if WATCH_PROMISE in lines:
            lines[lines.index(WATCH_PROMISE)] = CANT_WATCH_NOTE
        else:
            lines.insert(-1, CANT_WATCH_NOTE)
    out = [esc(line) for line in lines]
    if report.status == "ok" and out:
        out[0] = f"<b>{out[0]}</b>"
    return (prefix + "\n\n" if prefix else "") + "\n".join(out)


# --- Список «Мои квартиры» ------------------------------------------------------------------


def watch_result_text(result: WatchResult, name: FlatName | None = None) -> str:
    if result.status == "added":
        what = f": {name.subject[:1].lower()}{name.subject[1:]}" if name else ""
        text = (
            f"👁 Готово, слежу{what}.\nСообщу, если цена изменится, объявление перевыложат или "
            f"снимут. В списке {result.count} из {result.limit}: /list"
        )
    elif result.status == "already":
        text = "Уже слежу за этой квартирой. Весь список: /list"
    elif result.status == "limit":
        text = (
            f"В списке уже {result.count} квартир — это максимум. Уберите ненужную в /list "
            "и нажмите «Следить» ещё раз."
        )
    elif result.status == "not_matched":
        text = CANT_WATCH_NOTE
    elif result.status == "denied":
        return stale_button_text()
    else:
        text = (
            "Этого объявления пока нет в нашей базе, следить пока не за чем. Сборщик обходит "
            "площадки раз в 30–60 минут — пришлите ссылку позже."
        )
    return esc(text)


def watch_usage_text() -> str:
    return esc(
        "Чтобы следить за квартирой, пришлите ссылку: /watch https://re.kufar.by/vi/123456789\n"
        "Или нажмите «Следить» под отчётом."
    )


def unwatch_usage_text() -> str:
    return esc(
        "Пришлите ссылку: /unwatch https://re.kufar.by/vi/123456789\n"
        "Или откройте /list и нажмите «Не следить»."
    )


def stale_button_text() -> str:
    return esc("Эта кнопка устарела. Пришлите ссылку на объявление ещё раз.")


TOO_OFTEN = "Слишком часто. Подождите немного и попробуйте снова."


def unwatched_text(ok: bool) -> str:
    return esc("Больше не слежу за этой квартирой." if ok else "Этой квартиры нет в вашем списке.")


def forget_confirm_text() -> str:
    return esc(
        "Удалить ваши данные? Я сотру ваш Telegram ID, список квартир и историю проверок. "
        "Отменить это нельзя."
    )


def forgotten_text(ok: bool) -> str:
    if not ok:
        return esc("У меня нет ваших данных.")
    return esc("Готово: ваши данные удалены. Чтобы начать заново, просто пришлите ссылку.")


def _item_status(item: WatchItem) -> str:
    on_market = ""
    if item.days is not None:
        on_market = f"на рынке {'как минимум ' if item.at_least else ''}{days_text(item.days)}"
    drops = f"снижений: {item.drops}" if item.drops else "цену не снижали"
    if item.state == "removed":
        when = f" {date_short(item.last_seen)}" if item.last_seen else ""
        return f"Снята со всех площадок{when}" + (f", была {on_market}" if on_market else "")
    if item.state == "hidden":
        when = f" (последний раз видели {date_short(item.last_seen)})" if item.last_seen else ""
        return f"Сейчас объявлений нет{when}"
    if item.amount is None:
        return ", ".join(x for x in ("Цена не указана", on_market) if x)
    price = f"Сейчас: {money(item.amount, item.currency)} — {source_name(item.source)}"
    return " · ".join(x for x in (price, on_market, drops) if x)


def watchlist_text(items: Sequence[WatchItem], limit: int) -> str:
    if not items:
        return esc(
            "Список пуст. Пришлите ссылку на объявление и нажмите «Следить» под отчётом — "
            "я сообщу, если цена изменится или объявление снимут."
        )
    lines = [f"<b>Мои квартиры: {len(items)} из {limit}</b>", ""]
    for i, item in enumerate(items, 1):
        lines.append(esc(f"{i}. {item.name.subject}"))
        lines.append(esc(f"   {_item_status(item)}"))
    return fit("\n".join(lines))


# --- Уведомления по списку ------------------------------------------------------------------

ICONS = {
    "price_drop": "📉",
    "price_up": "📈",
    "relisted": "↻",
    "cross_listed": "🔁",
    "removed": "🚫",
    "returned": "✅",
}
NOTIFY_TYPES = tuple(ICONS)


@dataclass(frozen=True)
class EventNews:
    """Одно событие для уведомления. Все поля — из событий Emma и данных аналитики."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str | None = None  # площадка объявления события (перевыложена/вернулась/…)
    amount: Decimal | None = None  # его текущая цена
    currency: str | None = None
    drop_number: int = 0  # для price_drop: какое это снижение по счёту
    relist_price_changed: bool = False  # у перевыложенного объявления другая цена


@dataclass(frozen=True)
class ObjectNews:
    name: FlatName
    events: tuple[EventNews, ...]
    days: int | None = None  # на рынке
    at_least: bool = False
    site_days: int | None = None  # сколько показывает сайт у текущего объявления
    drops: int = 0
    state: str = "active"


def _payload_money(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return money(Decimal(str(value)), payload.get("currency")) if value is not None else "—"


def _phrase(e: EventNews) -> str:
    """Что случилось, без подлежащего: «подешевела: $92 500 → $90 000 (−2.7%)»."""
    p = e.payload
    price = f" ({money(e.amount, e.currency)})" if e.amount is not None else ""
    if e.type in ("price_drop", "price_up"):
        verb = "подешевела" if e.type == "price_drop" else "подорожала"
        if p.get("via") == "relisted":
            verb += " при перевыкладывании"
        change = f" ({pct(float(p['pct']))})" if p.get("pct") is not None else ""
        return f"{verb}: {_payload_money(p, 'old')} → {_payload_money(p, 'new')}{change}"
    if e.type == "relisted":
        text = f"перевыложена: новое объявление на {source_name(e.source or p.get('source'))}"
        if e.amount is not None and not e.relist_price_changed:
            return text + f", цена та же{price}"
        return text + price
    if e.type == "cross_listed":
        if p.get("same_source"):
            return f"продаётся ещё в одном объявлении на {source_name(e.source)}{price}"
        return f"появилась ещё и на {source_name(e.source)}{price}"
    if e.type == "removed":
        return "снята со всех площадок"
    if e.type == "returned":
        text = f"снова в продаже на {source_name(e.source or p.get('source'))}{price}"
        away = p.get("away_days")
        if away is not None and float(away) >= 1:
            text += f", не было {days_text(int(float(away)))}"
        return text
    return e.type


def _market_days(news: ObjectNews) -> str | None:
    if news.days is None:
        return None
    return ("как минимум " if news.at_least else "") + days_text(news.days)


def _detail(news: ObjectNews, e: EventNews) -> str | None:
    """Вторая строка одиночного уведомления (docs 1.3)."""
    days = _market_days(news)
    if e.type == "price_drop" and e.drop_number:
        n = "Первое снижение" if e.drop_number == 1 else f"Это {e.drop_number}-е снижение"
        if days is None:
            return n + "."
        return f"{n} за {days}." if not news.at_least else f"{n}, на рынке {days}."
    if (
        e.type == "relisted"
        and news.site_days is not None
        and news.days is not None
        and news.days - news.site_days >= 7
    ):
        return f"Счётчик на сайте показывает «{days_text(news.site_days)}», на самом деле {days}."
    if e.type == "removed":
        return "Если она вернётся, я сообщу."
    return None


def news_text(news: ObjectNews) -> str:
    """Уведомление по одной квартире. Несколько событий — одним сообщением."""
    events = news.events
    if len(events) == 1:
        e = events[0]
        lines = [f"{ICONS.get(e.type, '•')} {news.name.subject} {_phrase(e)}"]
        detail = _detail(news, e)
        if detail:
            lines.append(f"   {detail}")
        return fit(esc("\n".join(lines)))
    lines = [f"{news.name.subject}:"]
    for e in events:
        phrase = _phrase(e)
        lines.append(f"{ICONS.get(e.type, '•')} {phrase[:1].upper()}{phrase[1:]}")
    facts = []
    days = _market_days(news)
    if days and news.state != "removed":
        facts.append(f"На рынке {days}")
    if news.drops:
        facts.append(f"снижений цены: {news.drops}")
    if facts:
        lines.append(", ".join(facts) + ".")
    if events[-1].type == "removed":
        lines.append("Если она вернётся, я сообщу.")
    return fit(esc("\n".join(lines)))


# --- Недельная сводка -----------------------------------------------------------------------


def weekly_text(summary: WeeklySummary) -> str:
    lines = render_weekly_text(summary).split("\n")
    out = [esc(line) for line in lines]
    if out:
        out[0] = f"<b>{out[0]}</b>"
    return fit("\n".join(out))


def weekly_preview_text(summary: WeeklySummary, *, published_at: datetime | None) -> str:
    state = (
        f"Уже опубликована {date_full(published_at)}. Опубликовать ещё раз: /weekly publish force"
        if published_at
        else "Опубликовать в канал: /weekly publish"
    )
    return fit(weekly_text(summary) + "\n\n" + esc(f"— Предпросмотр. {state}"))


def weekly_interrupted_text(period: str) -> str:
    return esc(
        f"⚠️ Сводка {period}: бот остановился во время публикации, и я не знаю, вышел ли пост. "
        "Проверьте канал. Если поста нет — /weekly publish"
    )
