"""Анкета поиска квартиры: черновик, шаги, клавиатуры, разбор кнопок и роутер.

Одно сообщение меняется на месте; в первой строке копится запрос (`SearchQuery.label`).
Черновики живут в памяти `BotService` и теряются при перезапуске — тогда кнопка отвечает
«Кнопка устарела» и предлагает начать заново.

callback_data (≤ 64 байт):
q:b:<0..4>              бюджет-кнопка (4 — «больше 130»)    q:bc   своя сумма (текстом)
q:r:<1..4>              комнаты, переключатель (4 — «4+»)    q:ok   «Готово»
q:d:<0..8>              район по индексу MINSK_DISTRICTS     q:da   любой район
q:a:<0|30|40|50|60|80>  площадь от (0 — неважно)
q:h:<new|old|any>       новостройка / вторичка / неважно
q:f1 / q:fl             не первый / не последний этаж (переключатели)
q:back  назад    q:go  «Показать квартиры»    q:rep  повторить последний    q:new  заново
q:x:<0|1>               применить подсказку пустой выдачи
s:<best|cheap|fresh>:<offset>  страница выдачи: сортировка и сдвиг («Ещё 5», «Сначала …»)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Literal

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

from minsk_realty.analytics.fmt import MINSK_DISTRICTS
from minsk_realty.analytics.search import BUDGET_STEPS, SearchQuery, Sort
from minsk_realty.bot import keyboards
from minsk_realty.bot.keyboards import _btn
from minsk_realty.bot.texts import esc

if TYPE_CHECKING:
    from minsk_realty.bot.service import BotService, Reply, SearchPage

log = logging.getLogger(__name__)

Step = Literal["budget", "budget_custom", "rooms", "district", "area", "more"]

NEW_SEARCH = "🔍 Найти квартиру"
BUDGET_LABELS = ("до 60 тыс.", "до 80", "до 100", "до 130", "больше 130")
AREAS = (30, 40, 50, 60, 80)
PROMPTS: dict[Step, str] = {
    "budget": "Бюджет, USD:",
    "budget_custom": (
        "Напишите бюджет в долларах: 85000, 85 или 70-90. "
        "Числа меньше 1000 — тысячи, через дефис — от и до."
    ),
    "rooms": "Комнаты — можно несколько, затем «Готово»:",
    "district": "Район — можно несколько, затем «Готово». Или «Любой»:",
    "area": "Площадь:",
    "more": "Ещё условия — или сразу «Показать квартиры»:",
}
BACK: dict[Step, Step] = {
    "budget_custom": "budget",
    "rooms": "budget",
    "district": "rooms",
    "area": "district",
    "more": "area",
}
NEXT_ON_OK: dict[Step, Step] = {"rooms": "district", "district": "area"}
# Кнопки каждого шага; «Назад» — на всех. Кнопка из старого сообщения анкеты, которая не
# относится к текущему шагу, ничего не меняет: сообщение просто показывает текущий шаг.
STEP_ACTIONS: dict[Step, frozenset[str]] = {
    "budget": frozenset({"b", "bc"}),
    "budget_custom": frozenset(),
    "rooms": frozenset({"r", "ok"}),
    "district": frozenset({"d", "da", "ok"}),
    "area": frozenset({"a"}),
    "more": frozenset({"h", "f1", "fl"}),
}
# Кнопки шагов анкеты: только правят черновик в памяти, ограничитель частоты их не считает.
STEP_BUTTONS = frozenset({"b", "bc", "r", "d", "da", "a", "h", "f1", "fl", "ok", "back"})
_SIMPLE = frozenset({"bc", "da", "f1", "fl", "ok", "back", "go", "rep", "new"})
_VALUES = {
    "b": range(len(BUDGET_STEPS) + 1),
    "r": range(1, 5),
    "d": range(len(MINSK_DISTRICTS)),
    "a": (0, *AREAS),
    "x": range(2),
}
PAGE_SIZE = 5
SORT_LABELS: dict[Sort, str] = {
    "best": "Сначала выгодные",
    "cheap": "Сначала дешёвые",
    "fresh": "Сначала свежие",
}


@dataclass
class SearchDraft:
    query: SearchQuery = field(default_factory=SearchQuery)
    step: Step = "budget"
    message_id: int | None = None  # сообщение анкеты, которое правим после текста


@dataclass(frozen=True)
class SearchCallback:
    action: str  # код после «q:» (b, bc, r, …, x) или «page» для «s:»
    value: int | str | None = None  # для «page» — сдвиг
    sort: Sort | None = None  # для «page»


def parse_search_callback(data: str | None) -> SearchCallback | None:
    parts = (data or "").split(":")
    if parts[0] == "s":
        if len(parts) == 3 and parts[1] in SORT_LABELS and parts[2].isdecimal():
            return SearchCallback("page", int(parts[2]), parts[1])
        return None
    if parts[0] != "q":
        return None
    if len(parts) == 2 and parts[1] in _SIMPLE:
        return SearchCallback(parts[1])
    if len(parts) == 3:
        action, raw = parts[1], parts[2]
        if action == "h" and raw in ("new", "old", "any"):
            return SearchCallback(action, raw)
        if action in _VALUES and raw.isdigit() and int(raw) in _VALUES[action]:
            return SearchCallback(action, int(raw))
    return None


# --- Шаги -----------------------------------------------------------------------------------


def apply(draft: SearchDraft, cb: SearchCallback) -> None:
    """Применить кнопку к черновику (кнопки «go», «rep», «new» сюда не приходят)."""
    q, action, value = draft.query, cb.action, cb.value
    if action == "back":
        draft.step = BACK.get(draft.step, draft.step)
        return
    if action not in STEP_ACTIONS[draft.step]:
        return
    if action == "b":
        if value < len(BUDGET_STEPS):
            set_budget(draft, None, BUDGET_STEPS[value])
        else:
            set_budget(draft, BUDGET_STEPS[-1], None)
        return
    if action == "bc":
        draft.step = "budget_custom"
    elif action == "r":
        q = replace(q, rooms=q.rooms ^ {value})
    elif action == "d":
        q = replace(q, districts=q.districts ^ {MINSK_DISTRICTS[value]})
    elif action == "da":
        q, draft.step = replace(q, districts=frozenset()), "area"
    elif action == "ok":
        draft.step = NEXT_ON_OK[draft.step]
    elif action == "a":
        q, draft.step = replace(q, area_min=value or None), "more"
    elif action == "h":
        q = replace(q, housing=None if value == "any" else value)
    elif action == "f1":
        q = replace(q, not_first=not q.not_first)
    elif action == "fl":
        q = replace(q, not_last=not q.not_last)
    draft.query = q


def set_budget(draft: SearchDraft, low: int | None, high: int | None) -> None:
    draft.query = replace(draft.query, price_min=low, price_max=high)
    draft.step = "rooms"


def _mark(text: str, on: bool) -> str:
    return f"✓ {text}" if on else text


def _budget_rows(q: SearchQuery) -> list[list]:
    if q.price_min is None and q.price_max in BUDGET_STEPS:
        chosen = BUDGET_STEPS.index(q.price_max)
    elif q.price_min == BUDGET_STEPS[-1] and q.price_max is None:
        chosen = len(BUDGET_STEPS)
    else:
        chosen = None
    custom = chosen is None and (q.price_min is not None or q.price_max is not None)
    budget = [_btn(_mark(t, i == chosen), data=f"q:b:{i}") for i, t in enumerate(BUDGET_LABELS)]
    return [budget[:3], budget[3:], [_btn(_mark("своя сумма", custom), data="q:bc")]]


def _step_rows(draft: SearchDraft) -> list[list]:
    q = draft.query
    if draft.step == "budget":
        return _budget_rows(q)
    if draft.step == "rooms":
        return [
            [
                _btn(_mark("4+" if r == 4 else str(r), r in q.rooms), data=f"q:r:{r}")
                for r in range(1, 5)
            ]
        ]
    if draft.step == "district":
        names = [
            _btn(_mark(name, name in q.districts), data=f"q:d:{i}")
            for i, name in enumerate(MINSK_DISTRICTS)
        ]
        rows = [names[i : i + 3] for i in range(0, len(names), 3)]
        return [*rows, [_btn("Любой", data="q:da")]]
    if draft.step == "area":
        areas = [_btn(_mark(f"от {a} м²", q.area_min == a), data=f"q:a:{a}") for a in AREAS]
        return [areas[:3], [*areas[3:], _btn("Неважно", data="q:a:0")]]
    if draft.step == "more":
        return [
            [
                _btn(_mark("Новостройка", q.housing == "new"), data="q:h:new"),
                _btn(_mark("Вторичка", q.housing == "old"), data="q:h:old"),
                _btn("Неважно", data="q:h:any"),
            ],
            [
                _btn(_mark("Не первый этаж", q.not_first), data="q:f1"),
                _btn(_mark("Не последний этаж", q.not_last), data="q:fl"),
            ],
        ]
    return []  # своя сумма: ждём текст


def render(
    draft: SearchDraft, repeat: SearchQuery | None = None, note: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура текущего шага; `repeat` — последний запрос для кнопки «Повторить»,
    `note` — строка вместо подсказки шага (просьба повторить сумму)."""
    q = draft.query
    header = q.label() if q != SearchQuery() else "🔍 Поиск квартиры"
    rows = _step_rows(draft)
    if draft.step == "budget" and repeat is not None:
        rows.insert(0, [_btn(f"Повторить: {repeat.label()}", data="q:rep")])
    if draft.step != "budget":
        nav = [_btn("← Назад", data="q:back")]
        if draft.step in NEXT_ON_OK:
            nav.append(_btn("Готово", data="q:ok"))
        elif draft.step in ("area", "more"):
            nav.append(_btn("Показать квартиры", data="q:go", style="primary"))
        rows.append(nav)
    text = f"{esc(header)}\n\n{esc(note or PROMPTS[draft.step])}"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def new_search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(NEW_SEARCH, data="q:new")]])


def sort_keyboard(current: Sort) -> InlineKeyboardMarkup:
    """Под первым сообщением выдачи: три порядка, текущий отмечен."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(_mark(label, sort == current), data=f"s:{sort}:0")]
            for sort, label in SORT_LABELS.items()
        ]
    )


def more_keyboard(sort: Sort, offset: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(f"Ещё {PAGE_SIZE}", data=f"s:{sort}:{offset}")]]
    )


def hints_keyboard(hints: list[str]) -> InlineKeyboardMarkup:
    """Пустая выдача: каждая подсказка — кнопка, применяющая её (q:x:<номер>)."""
    rows = [[_btn(text, data=f"q:x:{i}")] for i, text in enumerate(hints)]
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [_btn(NEW_SEARCH, data="q:new")]])


# --- Роутер ---------------------------------------------------------------------------------


def build_router() -> Router:
    """/search и кнопки «q:»; подключается до общего роутера (у того кнопки без фильтра)."""
    router = Router(name="search")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.data.startswith(("q:", "s:")))

    @router.message(Command("search"))
    async def search_cmd(message: Message, service: BotService) -> None:
        reply = await asyncio.to_thread(service.search_start, message.chat.id)
        await _send(message.answer, message.chat.id, reply, service)

    @router.callback_query()
    async def on_button(query: CallbackQuery, bot: Bot, service: BotService) -> None:
        try:
            await _handle_button(query, bot, service)
        except Exception as exc:  # noqa: BLE001 — сбой одной кнопки не роняет бота
            log.error("кнопка поиска: сбой %s", type(exc).__name__)
            with suppress(Exception):
                await query.answer("Не получилось, попробуйте ещё раз")

    return router


async def _handle_button(query: CallbackQuery, bot: Bot, service: BotService) -> None:
    await query.answer()
    user_id = query.from_user.id
    message = query.message if isinstance(query.message, Message) else None
    send = message.answer if message is not None else partial(bot.send_message, user_id)
    cb = parse_search_callback(query.data)
    action = cb.action if cb is not None else None

    if action == "new":  # с /start, /help или «устарела»: анкета — новым сообщением
        reply = await asyncio.to_thread(service.search_start, user_id)
        await _send(send, user_id, reply, service)
        return
    if action in ("go", "rep", "page", "x"):  # выдача — новыми сообщениями, анкета выше
        if action == "page":
            job = partial(service.search_page, user_id, cb.sort, cb.value)
        elif action == "x":
            job = partial(service.search_relax, user_id, cb.value)
        else:
            job = partial(service.search_go, user_id, repeat=action == "rep")
        await send_page(send, await asyncio.to_thread(job))
        return
    reply = await asyncio.to_thread(service.search_button, user_id, cb)
    if message is None:
        await _send(send, user_id, reply, service)
        return
    with suppress(TelegramBadRequest):  # «message is not modified» — не ошибка
        await message.edit_text(reply.text, reply_markup=reply.keyboard)
    service.search_bind(user_id, message.message_id)


async def send_page(send, page: SearchPage) -> None:
    """Первое сообщение (с сортировками), затем карточки по одной; «Ещё 5» — под последней.
    Шесть сообщений подряд укладываются в лимиты Telegram на чат."""
    if page.summary is not None:
        await send(page.summary, reply_markup=page.sorts if page.cards else page.more)
    for n, card in enumerate(page.cards, 1):
        markup = keyboards.join(card.keyboard, page.more) if n == len(page.cards) else card.keyboard
        if card.preview_url:
            preview = LinkPreviewOptions(
                url=card.preview_url, prefer_large_media=True, show_above_text=True
            )
        else:
            preview = LinkPreviewOptions(is_disabled=True)
        await send(card.text, reply_markup=markup, link_preview_options=preview)


async def show_step(bot: Bot, chat_id: int, reply: Reply, service: BotService) -> None:
    """Шаг после текста («своя сумма»): правим сообщение анкеты, не вышло — шлём новое."""
    draft = service.drafts.get(chat_id)
    if draft is not None and draft.message_id is not None:
        with suppress(TelegramBadRequest):
            await bot.edit_message_text(
                reply.text,
                chat_id=chat_id,
                message_id=draft.message_id,
                reply_markup=reply.keyboard,
            )
            return
    await _send(partial(bot.send_message, chat_id), chat_id, reply, service)


async def _send(send, chat_id: int, reply: Reply, service: BotService) -> None:
    sent = await send(reply.text, reply_markup=reply.keyboard)
    service.search_bind(chat_id, sent.message_id)
