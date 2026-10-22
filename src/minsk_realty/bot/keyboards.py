"""Инлайн-клавиатуры и разбор callback_data (≤ 64 байт).

w:<listing_id>              — «Следить» под отчётом
u:<object_id>:r<listing_id> — «Не следить» под отчётом (вернуть кнопку «Следить»)
u:<object_id>:l             — «Не следить» в /list (перерисовать список)
u:<object_id>:n             — «Не следить» под уведомлением
r:<object_id>               — свежий отчёт по квартире из списка или уведомления
f:yes | f:no                — подтверждение /forget
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from minsk_realty.bot.watchlist import WatchItem

OPEN = "Открыть объявление"


@dataclass(frozen=True)
class Callback:
    action: str  # watch / unwatch / report / forget
    object_id: int | None = None
    listing_id: int | None = None
    context: str | None = None  # для unwatch: r / l / n; для forget: yes / no


def parse_callback(data: str | None) -> Callback | None:
    parts = (data or "").split(":")
    try:
        if parts[0] == "w" and len(parts) == 2:
            return Callback("watch", listing_id=int(parts[1]))
        if parts[0] == "r" and len(parts) == 2:
            return Callback("report", object_id=int(parts[1]))
        if parts[0] == "u" and len(parts) == 3:
            ctx = parts[2]
            if ctx.startswith("r") and ctx[1:].isdigit():
                return Callback("unwatch", int(parts[1]), int(ctx[1:]), "r")
            if ctx in ("l", "n"):
                return Callback("unwatch", int(parts[1]), context=ctx)
        if parts[0] == "f" and len(parts) == 2 and parts[1] in ("yes", "no"):
            return Callback("forget", context=parts[1])
    except ValueError:
        return None
    return None


def _btn(text: str, *, data: str | None = None, url: str | None = None, style=None):
    return InlineKeyboardButton(text=text, callback_data=data, url=url, style=style)


def report_keyboard(
    *,
    listing_id: int,
    url: str | None,
    object_id: int | None,
    can_watch: bool,
    watching: bool,
    suggest_watch: bool,
) -> InlineKeyboardMarkup | None:
    """Под отчётом: «Следить» (выделена, если истории мало) и «Открыть объявление»."""
    watch = None
    if watching and object_id is not None:
        watch = _btn("✓ Слежу · Не следить", data=f"u:{object_id}:r{listing_id}")
    elif can_watch:
        watch = _btn(
            "👁 Следить", data=f"w:{listing_id}", style="primary" if suggest_watch else None
        )
    open_ = _btn(OPEN, url=url) if url else None
    if watch and suggest_watch and not watching:
        rows = [[watch], [open_]] if open_ else [[watch]]
    else:
        rows = [[b for b in (open_, watch) if b]]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def list_keyboard(items: Sequence[WatchItem]) -> InlineKeyboardMarkup | None:
    rows = [
        [
            _btn(f"{i}. Отчёт", data=f"r:{item.object_id}"),
            _btn(f"{i}. Не следить", data=f"u:{item.object_id}:l"),
        ]
        for i, item in enumerate(items, 1)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def news_keyboard(object_id: int, url: str | None) -> InlineKeyboardMarkup:
    first = [_btn("Отчёт", data=f"r:{object_id}")]
    if url:
        first.append(_btn(OPEN, url=url))
    return InlineKeyboardMarkup(
        inline_keyboard=[first, [_btn("Не следить", data=f"u:{object_id}:n")]]
    )


def without_unwatch(markup: InlineKeyboardMarkup | None, object_id: int):
    """Клавиатура уведомления после «Не следить»: убрать эту кнопку, остальное оставить."""
    if markup is None:
        return None
    rows = [
        [b for b in row if b.callback_data != f"u:{object_id}:n"] for row in markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def forget_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("Да, удалить", data="f:yes", style="danger"),
                _btn("Отмена", data="f:no"),
            ]
        ]
    )
