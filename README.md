# Квартирная История

История объявлений о продаже квартир в Минске (Kufar, Realt, Onliner, Domovita): срок экспозиции, снижения цены, перевыкладывания, дубли между площадками и вывод о торге. Всё это по одной ссылке на объявление.

- [Идея и обоснование](docs/01-idea.md)
- [Как это будет выглядеть: продукт, архитектура, команда](docs/02-product-and-architecture.md)
- Правила проекта для Claude: [.claude/skills/minsk-realty/SKILL.md](.claude/skills/minsk-realty/SKILL.md)
- Агенты: [.claude/agents/](.claude/agents/) — Стас (сбор), Вера (склейка), Лев (аналитика), Мила (бот), Глеб (контроль)

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest            # тесты без сети
ruff check .
```

## Запуск сборщика

Kufar не отвечает на запросы не из Беларуси, поэтому сборщик работает на сервере в Беларуси.

```bash
cp .env.example .env        # заполнить POSTGRES_PASSWORD и SELLER_KEY_SALT
docker compose up -d --build
docker compose logs -f collector
```

Сборщик обходит Kufar и Realt раз в 30–60 минут и записывает изменения объявлений.
Если площадка перестаёт отдавать данные, он присылает алерт в Telegram (`TELEGRAM_BOT_TOKEN`, `ADMIN_CHAT_ID`), не чаще раза в сутки на одну проблему.
После ответов 403/429 площадка останавливается. Запустить её снова после проверки причины:
`docker compose run --rm collector python -m minsk_realty.collector --once --unblock kufar`.
Первые живые фикстуры (сохранятся в `tests/fixtures/` на сервере):
`docker compose run --rm --user "$(id -u):$(id -g)" -v "$PWD/tests/fixtures:/app/tests/fixtures" collector python scripts/capture_fixture.py kufar`
