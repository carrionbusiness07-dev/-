"""Сборка бота: Bot, Dispatcher, фоновые задачи (рассылка и недельная сводка)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from minsk_realty.bot import search, texts
from minsk_realty.bot.config import BotConfig
from minsk_realty.bot.handlers import build_router
from minsk_realty.bot.publish import bot_sender, notify_once, scheduled_weekly
from minsk_realty.bot.ratelimit import RateLimitMiddleware, TokenBucket
from minsk_realty.bot.service import BotService
from minsk_realty.db.session import make_engine, make_session_factory

log = logging.getLogger("minsk_realty.bot")

COMMANDS = [
    BotCommand(command="start", description="Что умеет бот"),
    BotCommand(command="search", description="Найти квартиру"),
    BotCommand(command="check", description="Отчёт по ссылке на объявление"),
    BotCommand(command="list", description="Мои квартиры"),
    BotCommand(command="watch", description="Следить за квартирой по ссылке"),
    BotCommand(command="unwatch", description="Перестать следить"),
    BotCommand(command="help", description="Помощь"),
]
WEEKLY_TICK = timedelta(minutes=10)


def make_bot(cfg: BotConfig) -> Bot:
    return Bot(
        cfg.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def build_dispatcher(service: BotService, cfg: BotConfig) -> Dispatcher:
    dp = Dispatcher()
    dp["service"] = service
    limiter = RateLimitMiddleware(TokenBucket(cfg.rate_burst, cfg.rate_per_minute))
    dp.message.outer_middleware(limiter)
    dp.callback_query.outer_middleware(limiter)
    dp.include_router(search.build_router())  # до общего: у того кнопки без фильтра
    dp.include_router(build_router(cfg))
    return dp


async def every(period: timedelta, job: Callable[[], Awaitable[object]], name: str) -> None:
    """Запускать `job` раз в `period`. Сбой одного прохода не останавливает цикл."""
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — сбой прохода не роняет бота; без traceback (токен)
            log.error("%s: сбой прохода: %s", name, type(exc).__name__)
        await asyncio.sleep(period.total_seconds())


async def run(cfg: BotConfig) -> None:
    engine = make_engine()
    service = BotService(make_session_factory(engine), cfg)
    bot = make_bot(cfg)
    dp = build_dispatcher(service, cfg)

    recovered = await asyncio.to_thread(service.recover)
    if recovered:
        log.warning(
            "уведомлений с неизвестным исходом после перезапуска: %s (не повторяем)", recovered
        )
    with suppress(Exception):
        await bot.set_my_commands(COMMANDS)

    send = bot_sender(bot)
    for period in await asyncio.to_thread(service.recover_weekly):
        log.warning("сводка %s: бот остановился во время публикации", period)
        if cfg.admin_chat_id is not None:
            with suppress(Exception):
                await send(cfg.admin_chat_id, texts.weekly_interrupted_text(period), None)
    tasks = [
        asyncio.create_task(asyncio.to_thread(service.warm_up)),
        asyncio.create_task(
            every(cfg.notify_every, lambda: notify_once(send, service), "уведомления")
        ),
    ]
    if cfg.channel_id is not None:
        tasks.append(
            asyncio.create_task(
                every(
                    WEEKLY_TICK,
                    lambda: scheduled_weekly(send, service, cfg),
                    "недельная сводка",
                )
            )
        )
    else:
        log.info("CHANNEL_ID не задан — недельная сводка в канал не публикуется")
    log.info(
        "бот стартует: рассылка раз в %s мин, лимит списка %s",
        int(cfg.notify_every.total_seconds() // 60),
        cfg.watch_limit,
    )
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        engine.dispose()
