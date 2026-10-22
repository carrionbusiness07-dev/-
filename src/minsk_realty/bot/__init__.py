"""Telegram-бот «Хроники» (aiogram 3).

    python -m minsk_realty.bot          # long polling, токен из TELEGRAM_BOT_TOKEN

Устройство: хендлеры (`handlers`) тонкие — достают telegram_id и текст, зовут синхронный
`BotService` в отдельном потоке и отправляют готовый текст с клавиатурой. Тексты
(`texts`), клавиатуры (`keyboards`), разбор ссылок (`links`), отбор событий для рассылки
(`notify`) и недельная сводка (`weekly`) — обычные функции, их проверяют тесты на SQLite.
Цифры отчётов считает аналитика (`minsk_realty.analytics`), бот их только показывает.
"""
