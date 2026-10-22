"""Отправка того, что бот пишет сам: уведомления по списку и недельная сводка в канал.

`send` — корутина `(chat_id, text, keyboard) -> None`; в работе это `bot.send_message`,
в тестах — подделка. Ошибки Telegram разбираются так:
- TelegramRetryAfter — Telegram просит подождать, сообщение точно не ушло: резерв снимаем,
  остаток прохода откладываем;
- любая другая ошибка — сообщение помечаем failed и больше не шлём (пользователь
  заблокировал бота, неверный чат, сетевой сбой с неизвестным исходом). Повтор после
  неизвестного исхода мог бы прислать то же событие дважды.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from minsk_realty.analytics.fmt import date_full
from minsk_realty.bot import keyboards, texts
from minsk_realty.bot.config import BotConfig
from minsk_realty.bot.notify import Outcome, Outgoing
from minsk_realty.bot.service import BotService

log = logging.getLogger(__name__)

Send = Callable[[Any, str, Any], Awaitable[None]]
SEND_PAUSE = 0.05  # ≈ 20 сообщений в секунду: ниже общего лимита Telegram (30/с)


def bot_sender(bot: Bot) -> Send:
    async def send(chat_id, text: str, markup) -> None:
        await bot.send_message(chat_id, text, reply_markup=markup)

    return send


async def notify_once(send: Send, service: BotService) -> int:
    """Один проход рассылки. → сколько сообщений ушло."""
    outgoing = await asyncio.to_thread(service.prepare_notifications)
    results: list[tuple[Outgoing, Outcome]] = []
    done: set[int] = set()
    sent = 0
    try:
        for i, o in enumerate(outgoing):
            try:
                await send(o.telegram_id, o.text, keyboards.news_keyboard(o.object_id, o.url))
            except TelegramRetryAfter as exc:
                log.warning("Telegram просит подождать %s с, рассылка отложена", exc.retry_after)
                break
            except asyncio.CancelledError:
                # Остановка бота посреди запроса: сообщение могло уйти. Не повторяем.
                results.append((o, "failed"))
                done.add(i)
                raise
            except TelegramForbiddenError:
                # Пользователь заблокировал бота: убрать его список, чтобы не биться в 403.
                results.append((o, "blocked"))
            except Exception as exc:  # noqa: BLE001 — исход неизвестен: не повторяем
                log.error("уведомление не отправлено: %s", type(exc).__name__)
                results.append((o, "failed"))
            else:
                results.append((o, "sent"))
                sent += 1
            done.add(i)
            await asyncio.sleep(SEND_PAUSE)
    finally:
        # Точно не отправленное (RetryAfter, остановка до запроса) — снять резерв.
        results += [(o, "release") for i, o in enumerate(outgoing) if i not in done]
        # shield: запись итогов должна дойти до БД и при повторной отмене задачи.
        await asyncio.shield(asyncio.to_thread(service.finish_notifications, results))
    return sent


@dataclass(frozen=True)
class Published:
    status: Literal["sent", "skipped", "failed"]
    text: str  # короткий итог для админа и лога


async def publish_weekly(
    bot_or_send: Bot | Send,
    service: BotService,
    cfg: BotConfig,
    *,
    force: bool = False,
    scheduled: bool = False,
) -> Published:
    """Опубликовать сводку за прошлую неделю в CHANNEL_ID."""
    if cfg.channel_id is None:
        return Published("skipped", "CHANNEL_ID не задан — публиковать некуда.")
    send = bot_sender(bot_or_send) if isinstance(bot_or_send, Bot) else bot_or_send
    r = await asyncio.to_thread(service.reserve_weekly, force=force, scheduled=scheduled)
    if r.status == "skipped":
        return Published("skipped", "Сводку по расписанию сейчас публиковать не нужно.")
    if r.status == "in_progress":
        return Published(
            "skipped", f"Сводка {r.period} публикуется прямо сейчас — подождите пару минут."
        )
    if r.status == "sent":
        when = f" {date_full(r.sent_at)}" if r.sent_at else ""
        return Published(
            "skipped",
            f"Сводка {r.period} уже опубликована{when}. Ещё раз: /weekly publish force",
        )
    period, text = r.period, r.text or ""
    try:
        await send(cfg.channel_id, text, None)
    except Exception as exc:  # noqa: BLE001 — любая ошибка: сводка failed, админ повторит
        await asyncio.to_thread(service.finish_weekly, period, False)
        log.error("сводка %s не опубликована: %s", period, type(exc).__name__)
        return Published(
            "failed",
            f"Не удалось опубликовать сводку {period} ({type(exc).__name__}). Проверьте, что "
            "бот — администратор канала и CHANNEL_ID верный, и повторите: /weekly publish",
        )
    await asyncio.to_thread(service.finish_weekly, period, True)
    log.info("сводка %s опубликована", period)
    return Published("sent", f"Сводка {period} опубликована в канал.")


async def scheduled_weekly(send: Send, service: BotService, cfg: BotConfig) -> Published:
    """Проход расписания: публикация в окне понедельника; о сбое — сообщение админу."""
    result = await publish_weekly(send, service, cfg, scheduled=True)
    if result.status == "failed" and cfg.admin_chat_id is not None:
        try:
            await send(cfg.admin_chat_id, f"⚠️ {texts.esc(result.text)}", None)
        except Exception as exc:  # noqa: BLE001 — алерт не важнее самого бота
            log.error("алерт о сводке не отправлен: %s", type(exc).__name__)
    return result
