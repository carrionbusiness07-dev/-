"""Настройки бота из переменных окружения (см. .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta


class ConfigError(RuntimeError):
    """Бот нельзя запустить с такими настройками (текст — для человека)."""


def chat_ref(value: str | None) -> int | str | None:
    """«-1001234567890» → int, «@channel» → str, пусто → None."""
    value = (value or "").strip()
    if not value:
        return None
    return int(value) if value.lstrip("-").isdigit() else value


@dataclass(frozen=True)
class BotConfig:
    token: str = ""
    admin_chat_id: int | str | None = None
    channel_id: int | str | None = None
    # Сколько квартир один пользователь может держать в списке.
    watch_limit: int = 10
    # Как часто искать новые события для рассылки.
    notify_every: timedelta = timedelta(minutes=10)
    # Не чаще одного уведомления в час по одному объекту одному пользователю.
    notify_min_gap: timedelta = timedelta(hours=1)
    # События, замеченные раньше, не рассылаем: бот мог долго лежать, старые новости
    # пользователю уже не нужны (а в отчёте они есть).
    notify_max_age: timedelta = timedelta(days=3)
    # Сообщений за один проход рассылки (остальные — в следующий проход).
    notify_batch: int = 200
    # Медианы рынка для /check пересчитываются не чаще раза в этот срок.
    baseline_ttl: timedelta = timedelta(hours=1)
    # Недельная сводка: понедельник, это время по Минску. Догоняем пропущенную не дольше
    # weekly_catch_up (если бот лежал в понедельник).
    weekly_weekday: int = 0
    weekly_hour: int = 10
    weekly_catch_up: timedelta = timedelta(days=2)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BotConfig:
        env = dict(os.environ if env is None else env)
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN не задан. Создайте бота у @BotFather и впишите токен в .env "
                "(см. docs/setup-home-computer.md)."
            )
        try:
            limit = int(env.get("BOT_WATCH_LIMIT") or cls.watch_limit)
            every = int(env.get("BOT_NOTIFY_EVERY_MIN") or 10)
        except ValueError as exc:
            raise ConfigError("BOT_WATCH_LIMIT и BOT_NOTIFY_EVERY_MIN — целые числа") from exc
        if limit < 1 or every < 1:
            raise ConfigError("BOT_WATCH_LIMIT и BOT_NOTIFY_EVERY_MIN должны быть больше нуля")
        return cls(
            token=token,
            admin_chat_id=chat_ref(env.get("ADMIN_CHAT_ID")),
            channel_id=chat_ref(env.get("CHANNEL_ID")),
            watch_limit=limit,
            notify_every=timedelta(minutes=every),
        )
