"""Хендлеры aiogram: достать telegram_id и текст, позвать BotService, отправить ответ.

Логики здесь нет: всё, что можно проверить без Telegram, живёт в service/texts/keyboards.
Пользовательские команды работают только в личных чатах; /weekly — только для ADMIN_CHAT_ID.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from functools import partial

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message, MessageEntity

from minsk_realty.bot import keyboards, search, texts
from minsk_realty.bot.config import BotConfig
from minsk_realty.bot.publish import publish_weekly
from minsk_realty.bot.service import BotService, Reply

log = logging.getLogger(__name__)


def message_text(message: Message) -> str:
    """Текст сообщения плюс адреса из «спрятанных» ссылок (текст со ссылкой внутри)."""
    text = message.text or message.caption or ""
    entities: list[MessageEntity] = list(message.entities or message.caption_entities or [])
    hidden = [e.url for e in entities if e.type == "text_link" and e.url]
    return " ".join([text, *hidden]).strip()


def is_admin(message: Message, cfg: BotConfig) -> bool:
    """Только сам админ (from_user.id) и только в личном чате с ботом: если ADMIN_CHAT_ID —
    группа, её участники не должны публиковать сводку."""
    if cfg.admin_chat_id is None or message.from_user is None:
        return False
    return message.chat.type == ChatType.PRIVATE and str(message.from_user.id) == str(
        cfg.admin_chat_id
    )


async def _typing(bot: Bot, chat_id: int) -> None:
    with suppress(Exception):
        await bot.send_chat_action(chat_id, ChatAction.TYPING)


def build_router(cfg: BotConfig) -> Router:
    root = Router(name="khronika")
    admin = Router(name="admin")
    users = Router(name="users")
    users.message.filter(F.chat.type == ChatType.PRIVATE)
    admin.message.filter(F.chat.type == ChatType.PRIVATE)
    root.include_routers(admin, users)

    # --- Админ ---------------------------------------------------------------------------

    @admin.message(Command("weekly"), lambda m: is_admin(m, cfg))
    async def weekly_cmd(
        message: Message, command: CommandObject, bot: Bot, service: BotService
    ) -> None:
        args = (command.args or "").split()
        if not args:
            await message.answer(await asyncio.to_thread(service.weekly_preview))
            return
        if args[0] != "publish":
            await message.answer("Команды: /weekly — предпросмотр, /weekly publish — в канал")
            return
        result = await publish_weekly(bot, service, cfg, force="force" in args[1:])
        await message.answer(texts.esc(result.text))

    # --- Пользователь ----------------------------------------------------------------------

    @users.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(texts.start_text(), reply_markup=search.new_search_keyboard())

    @users.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(
            texts.help_text(cfg.watch_limit), reply_markup=search.new_search_keyboard()
        )

    @users.message(Command("check"))
    async def check(
        message: Message, command: CommandObject, bot: Bot, service: BotService
    ) -> None:
        if not command.args:
            await message.answer(texts.check_usage_text())
            return
        await _typing(bot, message.chat.id)
        reply = await asyncio.to_thread(service.check_text, message.chat.id, command.args)
        await message.answer(reply.text, reply_markup=reply.keyboard)
        await _send_venue(message.answer_venue, reply)

    @users.message(Command("watch"))
    async def watch(message: Message, command: CommandObject, service: BotService) -> None:
        if not command.args:
            await message.answer(texts.watch_usage_text())
            return
        reply = await asyncio.to_thread(service.watch_text, message.chat.id, command.args)
        await message.answer(reply.text, reply_markup=reply.keyboard)

    @users.message(Command("unwatch"))
    async def unwatch(message: Message, command: CommandObject, service: BotService) -> None:
        reply = await asyncio.to_thread(service.unwatch_text, message.chat.id, command.args)
        await message.answer(reply.text, reply_markup=reply.keyboard)

    @users.message(Command("list"))
    async def watchlist(message: Message, service: BotService) -> None:
        reply = await asyncio.to_thread(service.watchlist, message.chat.id)
        await message.answer(reply.text, reply_markup=reply.keyboard)

    @users.message(Command("forget"))
    async def forget(message: Message) -> None:
        await message.answer(texts.forget_confirm_text(), reply_markup=keyboards.forget_keyboard())

    @users.message(F.text | F.caption)
    async def any_text(message: Message, bot: Bot, service: BotService) -> None:
        text = message_text(message)
        if text.startswith("/"):
            await message.answer(texts.help_text(cfg.watch_limit))
            return
        step = await asyncio.to_thread(service.search_text, message.chat.id, text)
        if step is not None:  # сумма на шаге «своя сумма»; всё прочее — как без анкеты
            await search.show_step(bot, message.chat.id, step, service)
            return
        await _typing(bot, message.chat.id)
        reply = await asyncio.to_thread(service.check_text, message.chat.id, text)
        await message.answer(reply.text, reply_markup=reply.keyboard)
        await _send_venue(message.answer_venue, reply)

    @users.message()
    async def other(message: Message) -> None:
        await message.answer(texts.no_link_text())

    # --- Кнопки ----------------------------------------------------------------------------

    @users.callback_query()
    async def on_button(query: CallbackQuery, bot: Bot, service: BotService) -> None:
        """Любая кнопка получает ответ: иначе у пользователя крутится индикатор."""
        try:
            await handle_button(query, bot, service)
        except Exception as exc:  # noqa: BLE001 — сбой одной кнопки не роняет бота
            log.error("кнопка %s: сбой %s", (query.data or "")[:2], type(exc).__name__)
            with suppress(Exception):
                await query.answer("Не получилось, попробуйте ещё раз")

    async def handle_button(query: CallbackQuery, bot: Bot, service: BotService) -> None:
        cb = keyboards.parse_callback(query.data)
        user_id = query.from_user.id
        message = query.message if isinstance(query.message, Message) else None
        if cb is None:
            await query.answer("Кнопка устарела")
            return

        if cb.action == "watch":
            reply, result = await asyncio.to_thread(service.watch_listing, user_id, cb.listing_id)
            await query.answer()
            if message is not None:
                if result.status in ("added", "already"):
                    markup = keyboards.card_after_watch(
                        message.reply_markup, result.object_id, cb.listing_id, watching=True
                    ) or await asyncio.to_thread(service.report_markup, user_id, cb.listing_id)
                    await _edit_markup(message, markup)
                await message.answer(reply.text)
            return

        if cb.action == "unwatch":
            ok = await asyncio.to_thread(service.unwatch, user_id, cb.object_id)
            await query.answer("Больше не слежу" if ok else "Этой квартиры уже нет в списке")
            if message is None:
                return
            if cb.context == "l":
                reply = await asyncio.to_thread(service.watchlist, user_id)
                with suppress(TelegramBadRequest):
                    await message.edit_text(reply.text, reply_markup=reply.keyboard)
            elif cb.context == "r" and cb.listing_id is not None:
                markup = keyboards.card_after_watch(
                    message.reply_markup, cb.object_id, cb.listing_id, watching=False
                ) or await asyncio.to_thread(service.report_markup, user_id, cb.listing_id)
                await _edit_markup(message, markup)
            else:
                await _edit_markup(
                    message, keyboards.without_unwatch(message.reply_markup, cb.object_id)
                )
            return

        if cb.action == "report":
            await query.answer()
            await _typing(bot, user_id)
            reply = await asyncio.to_thread(service.report_for_object, user_id, cb.object_id)
            if message is not None:
                await message.answer(reply.text, reply_markup=reply.keyboard)
                await _send_venue(message.answer_venue, reply)
            else:
                await bot.send_message(user_id, reply.text, reply_markup=reply.keyboard)
                await _send_venue(partial(bot.send_venue, user_id), reply)
            return

        if cb.action == "forget":
            await query.answer()
            if cb.context == "yes":
                reply = await asyncio.to_thread(service.forget, user_id)
            else:
                reply = None
            if message is not None:
                with suppress(TelegramBadRequest):
                    await message.edit_text(
                        reply.text if reply else texts.esc("Хорошо, ничего не удаляю.")
                    )

    return root


async def _send_venue(send, reply: Reply) -> None:
    # Текст уже ушёл: сбой подписи точки не должен ронять хендлер.
    if reply.venue is not None:
        v = reply.venue
        with suppress(TelegramBadRequest):
            await send(latitude=v.lat, longitude=v.lon, title=v.title, address=v.address)


async def _edit_markup(message: Message, markup) -> None:
    # «message is not modified» и устаревшие сообщения — не ошибка для пользователя.
    with suppress(TelegramBadRequest):
        await message.edit_reply_markup(reply_markup=markup)
