# Поиск квартир в боте — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Человек в боте задаёт кнопками бюджет, комнаты, район, площадь и получает квартиры со всех четырёх площадок, каждую один раз, сначала выгодные, с выводом о цене, деталями и фактами из описания продавца.

**Architecture:** Сборщик начинает сохранять в `snapshots.extra` детали квартиры и коды фактов из описания (текст описания не хранится). `analytics/search.py` строит в памяти индекс активных квартир с готовым выводом о цене и умеет фильтровать, сортировать и подсказывать при пустой выдаче. Бот (`bot/search.py`) ведёт анкету в одном сообщении, держит индекс в кэше, который сбрасывается после каждого прохода, и шлёт карточки с превью ссылки площадки. Попутно из отчёта и с сайта уходят минусы, плюсы и торг, а у новостроек пропадает вывод «подозрительно дёшево».

**Tech Stack:** Python 3.12, SQLAlchemy 2 + Alembic, aiogram 3.31 (`LinkPreviewOptions`), FastAPI + Jinja2, pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-flat-search-design.md`

## Global Constraints

- Тесты без сети: `PYTHONUTF8=1 .venv/Scripts/python -m pytest -q`, `ruff check .`. Известный посторонний сбой: `tests/test_matching.py::test_concurrent_processing_is_skipped_not_failed`.
- **Текст описания продавца не хранится, не логируется и не попадает в фикстуры.** Он живёт только внутри `to_raw` адаптера: там из него извлекаются коды фактов, а сам текст удаляется из `payload`. Тексты в тестах — придуманные.
- Персональных данных (телефоны, имена) в новых полях, таблицах и фикстурах нет. В новых таблицах — только `users.id`, `objects.id`, параметры запроса.
- Запросов к площадкам не больше, чем сейчас: новые поля берутся из ответов выдачи, которые сборщик уже получает. Onliner не трогаем.
- Коды значений полей площадок не придумываются: Kufar отдаёт подписи (`vl`, `_label`), подписи Realt берутся из данных Realt (Task 0). Поле без известной подписи не показываем.
- Пороги групп — существующие `AnalyticsConfig.group_suspicious_pct = -25.0`, `group_below_pct = -10.0`, `group_above_pct = 10.0`. Новых порогов нет.
- Тексты для людей — из спеки, разделы 3, 5, 6. Тон — как в нынешних текстах бота (`bot/texts.py`).
- `callback_data` ≤ 64 байт; параметры анкеты хранятся в боте, не в кнопках.
- Docker, живая база, выкатка и push — только в Task 0 (чтение) и Task 8 (с согласия владельца). Коммиты с `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Своя сумма бюджета — мусор:** `abc`, `0`, `-5`, `90-70`, `1 000 000`, `85 тыс`, `$85000`. Ожидание: пробелы, `$`, «тыс», «к» понимаются; перевёрнутый диапазон переворачивается; ноль, отрицательное и нечисло → вежливая просьба ввести ещё раз, анкета не теряется. Тест в Task 4 (`parse_budget`) и Task 6 (ответ бота).
2. **Бот перезапустился посреди анкеты или выдачи** — кнопка шага, «Ещё 5» или подсказки нажата, а черновика в памяти нет. Ожидание: «Кнопка устарела, начните поиск заново» и кнопка «🔍 Найти квартиру», без исключения. Тест в Task 6.
3. **Индекс перестроился между страницами** (закончился проход). Ожидание: «Ещё 5» берёт следующую страницу из нового индекса по тому же запросу и порядку; если квартир стало меньше смещения — «Больше квартир нет», без исключения. Тест в Task 7.
4. **Цена без пересчёта в USD или без площади.** Ожидание: квартира без USD не попадает ни в какой бюджет (сравнить нельзя); без площади — не проходит фильтр площади, но при «Неважно» показывается в группе «без сравнения». Тест в Task 4.
5. **Пока идёт анкета, человек присылает ссылку на объявление.** Ожидание: обычный отчёт /check, черновик анкеты не сбрасывается; текст, не похожий на сумму, на шаге «своя сумма» — тоже ссылка или подсказка, как сейчас. Тест в Task 6.

---

### Task 0: Разведка (без кода продукта)

**Files:**
- Create: `scripts/search_recon.py` (разовый, сеть и база)
- Modify: этот план — раздел «Результаты разведки» в конце

- [ ] **Step 1: Скрипт** `scripts/search_recon.py` с подкомандами:
  - `kufar-text` — одна страница выдачи Kufar (`kufar.SEARCH_URL`, `SEARCH_PARAMS`, `size=20`) через `PoliteClient`; печатает для каждого объявления только длины `body` и `body_short` и признак обрыва (заканчивается на `…`/`...`). Сам текст не печатать и не сохранять.
  - `realt-labels` — из `extract_page_props` страницы выдачи Realt найти справочники вариантов для `balconyType`, `wallMaterial`, `heating` (по образцу того, как получены `REALT_REPAIR_LABELS`; см. `tests/fixtures/realt/repair_state_options.2026-10-24.json`) и напечатать пары «код → подпись». Если справочника в данных нет — так и напечатать.
  - `kufar-labels` — по той же странице Kufar напечатать встретившиеся пары `v → vl` для `bathroom`, `balcony`, `flat_windows_side`, `possible_exchange`.
- [ ] **Step 2: Время индекса.** В контейнере (`docker compose exec -T web …`) замерить `time python -m minsk_realty.analytics assessment` — это та же работа, что построение индекса (загрузка рынка + `find_comparison` по всем активным объектам). Записать секунды и пик памяти (`docker stats --no-stream` во время прогона).
- [ ] **Step 3: Превью ссылок.** Через Bot API (`BOT_TOKEN` из окружения контейнера `bot`, не печатать) отправить в `ADMIN_CHAT_ID` 4 сообщения: по одной ссылке на активное объявление каждой площадки (ссылки взять из базы), с `link_preview_options={"prefer_large_media": true, "show_above_text": true}`. Владелец смотрит, у каких есть фото. Записать результат.
- [ ] **Step 4: Записать результаты** в раздел «Результаты разведки» этого плана: длина описания Kufar (полное или обрезанное, какое поле брать), подписи Realt и Kufar (таблицей), время и память индекса, площадки без фото в превью. **Если индекс строится дольше 60 с** — остановиться и обсудить ускорение с владельцем до Task 4.
- [ ] **Step 5: Commit** — `git add scripts/search_recon.py docs/superpowers/plans/2026-10-30-flat-search.md`, `chore: search recon script and findings`.

---

### Task 1: Факты из описания

**Files:**
- Create: `src/minsk_realty/analytics/facts.py`, `tests/test_facts.py`

**Interfaces:**
- Produces:
  - `FACT_LABELS: dict[str, str]` — код → подпись по-русски с маленькой буквы.
  - `WARNING_FACTS: frozenset[str]` — тревожные коды.
  - `extract_facts(text: str | None) -> list[str]` — коды в порядке `FACT_LABELS`, без повторов; `None`/пусто → `[]`.
  - `facts_line(codes: Iterable[str], limit: int | None = 5) -> str | None` — тревожные первыми; `"⚠️ продаётся доля · ⚠️ прописаны люди\n📝 Из описания: торг, с мебелью"`; при `limit` общее число фактов не больше `limit`; нет фактов → `None`.

Коды и подписи (спека 7.2): тревожные `share` «продаётся доля», `dormitory` «общежитие», `registered` «прописаны люди», `encumbrance` «залог или арест», `no_mortgage` «ипотека не подходит», `cash_only` «только наличные»; сделка `bargain` «торг», `urgent` «срочно», `exchange` «обмен», `clean_sale` «чистая продажа», `alternative` «встречная покупка», `mortgage` «ипотека или кредит», `installment` «рассрочка», `docs_ready` «документы готовы»; квартира `furniture` «с мебелью», `appliances` «с техникой», `fresh_repair` «свежий ремонт», `needs_repair` «требует ремонта», `replanning` «перепланировка», `storage` «кладовка», `courtyard_windows` «окна во двор», `warm_floor` «тёплый пол»; дом `elevator` «лифт», `gated` «закрытая территория», `parking` «парковка», `major_repair` «капремонт».

- [ ] **Step 1: Тесты** `tests/test_facts.py` (фразы придуманные):
  - `test_bargain_and_negations`: `"Торг уместен"` → `["bargain"]`; `"без торга"`, `"торг не уместен"`, `"Торга нет"` → `[]`.
  - `test_share_is_warning_and_first`: `facts_line(extract_facts("Продаётся 1/2 доли, мебель остаётся"))` начинается с `"⚠️ продаётся доля"` и содержит `"с мебелью"`.
  - `test_yo_and_case`: `"ТЁПЛЫЙ ПОЛ"` и `"теплый пол"` → `["warm_floor"]`.
  - `test_mortgage_vs_no_mortgage`: `"возможна ипотека"` → `["mortgage"]`; `"под ипотеку не подходит"` → `["no_mortgage"]` (и без `mortgage`).
  - `test_clean_sale_and_alternative`: `"чистая продажа"` → `clean_sale`; `"альтернатива"` / `"встречная покупка"` → `alternative`.
  - `test_limit_and_empty`: `facts_line([], 5) is None`; при 7 кодах в строке 5 подписей.
  - `test_every_code_has_label`: каждый код, который может вернуть `extract_facts`, есть в `FACT_LABELS`.
- [ ] **Step 2: Запуск** `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/test_facts.py -q` → FAIL (нет модуля).
- [ ] **Step 3: Реализация.** Текст нормализуется: нижний регистр, `ё→е`, пробелы схлопнуты. Каждый код — список регулярных выражений «есть» и, где нужно, «отрицание рядом» (в пределах ~3 слов до/после: «без», «не», «нет»). `mortgage` не ставится, если найден `no_mortgage`.
- [ ] **Step 4: Запуск** тестов → PASS; `ruff check src/minsk_realty/analytics/facts.py tests/test_facts.py`.
- [ ] **Step 5: Commit** `feat: facts from seller descriptions (rules, no text stored)`.

---

### Task 2: Детали и факты в адаптерах

**Files:**
- Modify: `src/minsk_realty/sources/kufar.py` (`parse`, `to_raw`, `EXTRA_PARAMS`), `src/minsk_realty/sources/realt.py` (`parse`, `to_raw`), `src/minsk_realty/sources/domovita.py` (`compact`, `parse`)
- Test: `tests/test_kufar.py`, `tests/test_realt.py`, `tests/test_onliner_domovita.py`, `tests/test_privacy.py`

**Interfaces:**
- Consumes: `extract_facts` (Task 1); подписи из Task 0.
- Produces — ключи в `ListingSnapshot.extra` (только если значение известно):
  - `kitchen_m2: float`, `living_m2: float`, `ceiling_m: float`;
  - `bathroom: str`, `balcony: str`, `walls: str`, `windows_side: str` — готовые подписи по-русски с маленькой буквы;
  - `exchange: bool` (Kufar `possible_exchange`);
  - `facts: list[str]` — коды из описания плюс `furniture`/`appliances`, если Realt отдаёт `furniture`/`appliances` = истина.
  - В `RawListing.payload` после `to_raw` нет ключей `body`, `body_short`, `description`, `headline`, `title`, `comments`, `specialComment`, `ads_text`; вместо них `_facts: list[str]`.

- [ ] **Step 1: Тесты.**
  - Kufar: на `tests/fixtures/kufar/search_sell_minsk.2026-10-23.json` для объявления с `size_kitchen` → `extra["kitchen_m2"]` — число из фикстуры; `bathroom` → подпись из `vl`. Синтетическое объявление с `body = "Торг. Под ипотеку не подходит"` → `extra["facts"] == ["no_mortgage", "bargain"]` (порядок `FACT_LABELS`) и в `to_raw(...).payload` нет `body`/`body_short`.
  - Realt: `areaKitchen` → `kitchen_m2`; `wallMaterial` с кодом из таблицы Task 0 → `walls`; неизвестный код → ключа нет; `furniture: true` → `"furniture" in facts`; `description` удалён из payload.
  - Domovita: `compact({... "ads_text": "Срочно, торг"})` → в результате нет `ads_text`, есть `_facts == ["bargain", "urgent"]`; `parse` кладёт `facts`.
  - `tests/test_privacy.py::test_free_text_never_reaches_snapshot`: для каждого из трёх адаптеров текст с `+375000000000` и «звоните, Анна» не встречается ни в `payload`, ни в `snapshot.model_dump_json()`.
- [ ] **Step 2: Запуск** этих тестов → FAIL.
- [ ] **Step 3: Реализация.** Текст: Kufar — поле из Task 0 (`body`, иначе `body_short`), Realt — `headline` + `description`, Domovita — `ads_text`. Факты считаются в `to_raw` (Domovita — в `compact`), затем текстовые ключи удаляются из payload. `parse` переносит `payload["_facts"]` в `extra["facts"]` (если не пусто). Механизм «`extra` изменился — обновить на месте» уже есть, его не трогать.
- [ ] **Step 4: Запуск** всех тестов адаптеров и `tests/test_live_fixtures.py` → PASS; `ruff check .`.
- [ ] **Step 5: Commit** `feat(collector): flat details and description facts in snapshot extra`.

---

### Task 3: Без минусов, плюсов и торга; новостройки без «подозрительно дёшево»; фото на сайте

**Files:**
- Modify: `src/minsk_realty/analytics/assessment.py`, `comparables.py` (`price_group`, `find_comparison`), `report.py` (`housing_label`, сбор отчёта, `CheckReport.bargaining`), `render.py` (`_minus_plus_lines`, `_verdict_lines`, `web_blocks`), `assessment_stats.py`, `__init__.py`; `src/minsk_realty/web/templates/object.html`, `web/static/khronika.css`; `DESIGN.md` (если описывает эти блоки)
- Delete: `src/minsk_realty/analytics/bargaining.py` (если после правок нигде не используется)
- Test: `tests/test_assessment.py`, `tests/test_analytics.py`, `tests/test_web.py`, `tests/test_bot.py`

**Interfaces:**
- Produces:
  - `is_new_building(year: int | None, new_flag: bool | None, now_year: int) -> bool` в `report.py`; `housing_label` начинается с «новостройка» ровно тогда, когда `is_new_building` истинно.
  - `price_group(diff_pct, cfg, *, new_building: bool = False)` и `find_comparison(..., new_building: bool = False)`: при `new_building` группа `suspicious` заменяется на `below`.
  - `Assessment(kind: str, verdict: str)` — без `minuses`, `pluses`; `assess(*, comparison, usd_per_m2) -> Assessment`; в тексте вывода нет хвостов про торг.
  - `CheckReport` без поля `bargaining`; `suggest_watch = data_level != "full"`.

- [ ] **Step 1: Тесты.**
  - `test_new_building_never_suspicious`: `price_group(-31, cfg, new_building=True) == "below"`, `price_group(-31, cfg) == "suspicious"`.
  - `test_report_new_building_verdict`: отчёт по объекту с годом `now_year + 1` и ценой на 30% ниже похожих → `assessment.kind == "below"`, текст начинается с «Стоит смотреть».
  - `test_no_minus_plus_bargain_anywhere`: в `render_check_text` и в HTML страницы квартиры нет «Минусы», «Плюсы», «Торг», «торг реален», «скидки не ждите».
  - `test_photo_button_height`: в `khronika.css` есть правило `.photo button` с `height: auto`.
  - Обновить закреплённые тексты отчёта в существующих тестах.
- [ ] **Step 2: Запуск** → FAIL.
- [ ] **Step 3: Реализация.** Передавать `new_building` во все вызовы `find_comparison`: `report.py` — из `is_new_building(flat.year, data.new_buildings.get(oid), now.year)`; `assessment_stats.py` — так же по `data.building_years` и `data.new_buildings`. Удалить код минусов, плюсов, торга и связанные константы и тесты. CSS: `.photo button { height: auto; }`.
- [ ] **Step 4: Запуск** полного набора тестов → PASS (кроме известного); `ruff check .`.
- [ ] **Step 5: Commit** `feat: drop minuses/pluses/bargaining, no "suspicious" for new builds, fix photo strip`.

---

### Task 4: Детали в данных и отчёте; индекс поиска

**Files:**
- Modify: `src/minsk_realty/analytics/data.py` (`FlatDetails`, `listing_details`, поле `MarketData.details`), `report.py` (`FlatInfo.details`), `render.py` (строки деталей и фактов), `web/templates/object.html`; `src/minsk_realty/web/views.py` → `data_version` переезжает в `src/minsk_realty/db/repository.py` (в `views` остаётся импорт)
- Create: `src/minsk_realty/analytics/search.py`, `tests/test_search.py`

**Interfaces:**
- Consumes: `extra`-ключи Task 2; `is_new_building`, `find_comparison(new_building=…)` Task 3; `facts_line` Task 1.
- Produces:
  - `FlatDetails` (frozen dataclass): `kitchen_m2: float | None`, `living_m2`, `ceiling_m`, `bathroom: str | None`, `balcony`, `walls`, `windows_side`, `exchange: bool | None`, `facts: tuple[str, ...]`. `listing_details(session, object_ids=None) -> dict[int, FlatDetails]` — по последним снимкам надёжных объявлений объекта, свежий снимок важнее; факты — объединение.
  - `details_line(d: FlatDetails | None) -> str | None` в `render.py`: `"Кухня 9 м² · санузел раздельный · балкон застеклён · кирпич · потолки 2,7"`.
  - `FlatInfo.details: FlatDetails | None`; отчёт «Подробно» и страница квартиры показывают `details_line` и `facts_line(..., limit=None)`.
  - `repository.data_version(session) -> tuple`.
  - В `search.py`:
    - `SearchItem` (frozen): `object_id, rooms, area, floor, floors_total, district, new_building: bool | None, price_amount: Decimal, price_currency: str, price_usd: float | None, usd_per_m2: float | None, group: str | None ("below"|"market"|"suspicious"|"above"|None), diff_pct: float | None, metro: str | None, metro_min: int | None, days: int | None, drops: int, sources: tuple[str, ...], owner: bool | None, url: str, url_source: str, listing_id: int, checked_at: datetime, first_seen: datetime, street: str | None, year: int | None, housing: str | None, details: FlatDetails | None`.
    - `build_index(data: MarketData, baseline: MarketBaseline, at: datetime, cfg=DEFAULT_ANALYTICS) -> tuple[SearchItem, ...]` — тот же набор объектов, что `views._object_rows` (активные, экспозиция не `removed`); цена из `data.object_price`; `url` — объявление с этой ценой.
    - `SearchQuery` (frozen): `price_min: int | None, price_max: int | None, rooms: frozenset[int]` (4 значит «4+», пусто — любые), `districts: frozenset[str]` (пусто — любой), `area_min: int | None`, `housing: Literal["new","old"] | None`, `not_first: bool`, `not_last: bool`; `to_json()`/`from_json()`; `label() -> str` — `"До $90 000 · 2-комн. · Фрунзенский, Центральный · от 50 м²"`.
    - `Sort = Literal["best", "cheap", "fresh"]`.
    - `search(index, query, sort="best") -> SearchResult(items: tuple[SearchItem, ...], counts: dict[str, int])` — `counts` по группам `below/market/suspicious/above/none`.
    - `suggestions(index, query, limit=2) -> list[tuple[str, SearchQuery, int]]` — (текст, ослабленный запрос, число), только с числом > 0, по убыванию числа.
    - `parse_budget(text: str) -> tuple[int | None, int | None] | None`.
    - `BUDGET_STEPS = (60_000, 80_000, 100_000, 130_000)`.
    - `summary_line(result, sort) -> str` — «Нашёл 23 квартиры: 5 дешевле похожих, 12 в рынке, 6 дороже. Показываю сначала выгодные.»

- [ ] **Step 1: Тесты** `tests/test_search.py` на синтетическом `MarketData` (по образцу `tests/analytics_data.py`):
  - `test_one_card_per_object`: объект с двумя объявлениями на разных площадках → один `SearchItem`, `sources == ("kufar", "realt")`, `url` — более дешёвого.
  - `test_filters`: бюджет, комнаты (`4` → 4 и 5), район по `object_district`, площадь, `housing`, `not_first`/`not_last` с неизвестным этажом → исключено.
  - `test_no_usd_never_in_budget` и `test_no_area_only_without_area_filter` (Review Focus 4).
  - `test_best_order`: группы `below → market → suspicious → above → None`; внутри по возрастанию `diff_pct`; `cheap` — по `price_usd`; `fresh` — по `first_seen` убыв.
  - `test_suggestions`: пустая выдача → подсказки «Если поднять бюджет до $100 000 — N квартир», «Если любой район — M»; не больше 2; нулевые не показываются.
  - `test_parse_budget`: `"85000"`→`(None, 85000)`; `"85"`→`(None, 85000)`; `"70-90"`→`(70000, 90000)`; `"90-70"`→`(70000, 90000)`; `"85 тыс"`, `"$85 000"`, `"85к"`→`(None, 85000)`; `"abc"`, `"0"`, `"-5"`→`None`.
  - `test_label_and_json_roundtrip`.
  - `test_details_merge`: два объявления, свежее с `kitchen_m2=9`, старое с `bathroom` → оба поля; факты объединены.
  - В `tests/test_analytics.py`: отчёт по объекту с деталями содержит `"Кухня 9 м²"` и строку фактов.
- [ ] **Step 2: Запуск** → FAIL.
- [ ] **Step 3: Реализация.** `build_index` повторяет цикл `assessment_stats` (точка `object_point(oid, cfg.approx_coord_sources)`, `find_comparison(..., new_building=…)`); метро и минуты — как в отчёте, только при точных координатах; дни и снижения — `compute_exposure` и события, как в `_object_rows`.
- [ ] **Step 4: Запуск** полного набора → PASS; `ruff check .`. Замерить `build_index` на демо-базе тестов (должен укладываться в оценку Task 0).
- [ ] **Step 5: Commit** `feat: flat details in report and site, in-memory search index`.

---

### Task 5: Таблицы поиска

**Files:**
- Create: `migrations/versions/0005_saved_searches.py`
- Modify: `src/minsk_realty/db/models.py`, `src/minsk_realty/bot/watchlist.py` (`has_seen_object`)
- Test: `tests/test_db.py`, `tests/test_bot.py`

**Interfaces:**
- Produces:
  - `SavedSearch` (`saved_searches`): `user_id` PK, FK `users.id ON DELETE CASCADE`; `params: JSON`; `updated_at`.
  - `SearchShown` (`search_shown`): PK (`user_id`, `object_id`), FK с `ON DELETE CASCADE` на `users` и `objects`; `shown_at`.
  - `save_search(session, telegram_id, params: dict) -> None`, `last_search(session, telegram_id) -> dict | None`, `mark_shown(session, telegram_id, object_ids: Iterable[int]) -> None` в `bot/watchlist.py`.
  - `has_seen_object` истинно и при строке в `search_shown`.

- [ ] **Step 1: Тесты:** `test_forget_removes_search`: после `save_search` и `mark_shown` → `forget_user` → строк нет. `test_seen_via_search`: `mark_shown` → `has_seen_object` истинно. `test_save_search_upsert`: повторный `save_search` заменяет параметры.
- [ ] **Step 2: Запуск** → FAIL.
- [ ] **Step 3: Реализация** по образцу `0004` (описание в docstring миграции, новые таблицы, существующие данные не трогаются).
- [ ] **Step 4: Запуск** тестов, в том числе `tests/test_matching_migration.py` → PASS.
- [ ] **Step 5: Commit** `feat(db): saved searches and shown objects`.

---

### Task 6: Анкета в боте

**Files:**
- Create: `src/minsk_realty/bot/search.py` (роутер, черновики, клавиатуры шагов, разбор кнопок)
- Modify: `src/minsk_realty/bot/app.py` (подключить роутер), `handlers.py` (`/start`, `/help`, текст на шаге «своя сумма»), `texts.py`, `service.py` (кэш индекса)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `SearchQuery`, `parse_budget`, `BUDGET_STEPS`, `MINSK_DISTRICTS`, `save_search`, `last_search` (Tasks 4–5).
- Produces:
  - `SearchDraft` (в памяти, `dict[int, SearchDraft]` в `BotService`): `query: SearchQuery`, `step: Literal["budget","budget_custom","rooms","district","area","more"]`, `message_id: int | None`.
  - `parse_search_callback(data: str | None) -> SearchCallback | None`; префикс `q:` — шаги анкеты, `s:` — выдача (Task 7). Формат (все ≤ 64 байт): `q:b:<0..4>` бюджет-кнопка (4 = «больше 130»), `q:bc` своя сумма, `q:r:<1..4>` комнаты, `q:d:<0..8>` район по индексу `MINSK_DISTRICTS`, `q:da` любой район, `q:a:<0|30|40|50|60|80>` площадь, `q:h:<new|old|any>`, `q:f1`, `q:fl`, `q:ok` «Готово», `q:back`, `q:go` «Показать квартиры», `q:rep` повтор, `q:new` начать заново.
  - `IndexCache` в `service.py`: `get(session, now) -> tuple[SearchItem, ...]`, перестраивает при смене `repository.data_version` (как `market_snapshot` в кабинете), потокобезопасно.
  - `BotService.search_start(telegram_id) -> Reply`, `search_button(telegram_id, cb) -> Reply`, `search_text(telegram_id, text) -> Reply | None` (None — текст не для анкеты).

- [ ] **Step 1: Тесты** (`tests/test_bot.py`, как существующие тесты сервиса):
  - `test_start_has_search_button`: `/start` и `/help` содержат кнопку `q:new` с текстом «🔍 Найти квартиру».
  - `test_wizard_steps_and_header`: бюджет `q:b:2` → комнаты `q:r:2` → `q:ok` → районы `q:d:<Фрунзенский>`, `q:d:<Центральный>` → `q:ok` → `q:a:50` → текст сообщения начинается с `"До $100 000 · 2-комн. · Фрунзенский, Центральный · от 50 м²"`; на каждом шаге есть `q:back`, с шага площади есть `q:go`.
  - `test_back_keeps_answers`.
  - `test_custom_budget`: `q:bc` → `search_text("70-90")` → в шапке `"$70 000–90 000"`; `search_text("abc")` → просьба ввести ещё раз, шаг не меняется (Review Focus 1).
  - `test_stale_button`: `search_button` без черновика → «Кнопка устарела, начните поиск заново» и кнопка `q:new` (Review Focus 2).
  - `test_link_during_wizard`: на шаге «своя сумма» текст со ссылкой Kufar → обычный отчёт /check, черновик на месте (Review Focus 5).
  - `test_repeat_last_search`: после `q:go` (Task 7 подменить заглушкой результата) `search_start` показывает `q:rep` с подписью «Повторить: <label>».
- [ ] **Step 2: Запуск** → FAIL.
- [ ] **Step 3: Реализация.** Шаги правят одно сообщение (`edit_message_text`); ответы-переключатели отмечаются «✓». Тексты кнопок — из спеки, раздел 3.
- [ ] **Step 4: Запуск** полного набора → PASS; `ruff check .`.
- [ ] **Step 5: Commit** `feat(bot): search wizard`.

---

### Task 7: Выдача и карточки

**Files:**
- Modify: `src/minsk_realty/bot/search.py`, `texts.py`, `service.py`, `keyboards.py` (кнопки карточки)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `search`, `suggestions`, `summary_line`, `IndexCache`, `mark_shown`, `save_search`; существующие `r:<object_id>`, `w:<listing_id>`, `u:<object_id>:r<listing_id>`.
- Produces:
  - `search_card_text(item: SearchItem, now: datetime) -> str` (HTML для Telegram, экранирование `esc`) — строки спеки, раздел 6, в том же порядке; строки без данных пропускаются; «проверено N мин/ч назад».
  - `search_card_keyboard(item, *, watching: bool) -> InlineKeyboardMarkup` — «Подробно» (`r:<object_id>`), «👁 Следить» / «✓ Слежу», «Открыть на <площадка> ↗» (`url`).
  - Кнопки выдачи: `s:<best|cheap|fresh>:<offset>`; `q:x:<0|1>` — применить подсказку.
  - `BotService.search_page(telegram_id, sort, offset) -> SearchPage(summary: str | None, cards: list[Card], more: InlineKeyboardMarkup | None)`, `Card(text, keyboard, preview_url)`.
  - Отправка карточки: `send_message(..., link_preview_options=LinkPreviewOptions(url=card.preview_url, prefer_large_media=True, show_above_text=True))`.

- [ ] **Step 1: Тесты:**
  - `test_card_text`: синтетический `SearchItem` (2 комн., 54 м², 7/9, Притыцкого, Фрунзенский, вторичка 1985, $86 500, −14%, «Кунцевщина» 6 мин, 74 дня, 2 снижения, kufar+realt, собственник, проверено 40 мин назад, кухня 9, факты `bargain`, `share`) → строки в порядке спеки; `"⚠️ продаётся доля"` выше `"📝 Из описания: торг"`; `"🟢 Стоит смотреть: на 14% дешевле похожих"`.
  - `test_card_suspicious_and_above`: «🔴 Подозрительно дёшево: на 31% ниже похожих — узнайте причину», «🟠 Дороже похожих на 18%».
  - `test_card_without_metro_for_approx_sources`: Realt/Domovita точка — нет строки 🚇.
  - `test_first_page`: 23 подходящих → `summary` «Нашёл 23 квартиры: …», 5 карточек, кнопка `s:best:5`; сортировки `s:cheap:0`, `s:fresh:0` в первом сообщении.
  - `test_last_page_no_more`, `test_index_shrank_between_pages` → «Больше квартир нет» (Review Focus 3).
  - `test_empty_with_suggestions`: 0 → текст подсказок и кнопки `q:x:0`, `q:x:1`; нажатие применяет ослабленный запрос.
  - `test_shown_objects_allow_details`: после показа страницы `r:<object_id>` отдаёт отчёт, а не «Кнопка устарела».
  - `test_saved_after_go`: `q:go` сохраняет запрос (`last_search`).
- [ ] **Step 2: Запуск** → FAIL.
- [ ] **Step 3: Реализация.** Хендлер шлёт `summary` (с кнопками сортировки), затем карточки по одной, последней — кнопку «Ещё 5». Отправка — через тот же `bot`, без новых ограничителей: 6 сообщений подряд укладываются в лимиты Telegram на чат.
- [ ] **Step 4: Запуск** полного набора → PASS; `ruff check .`.
- [ ] **Step 5: Commit** `feat(bot): search results with cards and link previews`.

---

### Task 8: Документы, ревью Grant, выкатка

**Files:**
- Modify: `docs/01-idea.md` (раздел 3; раздел 9, проверка 2), `.claude/skills/minsk-realty/SKILL.md` («Границы продукта»), `docs/status.md`

- [ ] **Step 1: Документы** — дословно по спеке, раздел 10: в `01-idea.md` §3 «поиск по запросу — делаем, рассылка новых объявлений быстрее всех — нет, аренда — следующий проект (правило «аренду не делаем» снято владельцем 30.10.2026)»; §9 проверка 2 — новая формулировка с датой решения 30.10.2026; то же в `SKILL.md`.
- [ ] **Step 2: Ревью Grant** (`grant-guard`) по всей ветке: персональные данные и текст описаний нигде не хранятся и не логируются, нагрузка на площадки не выросла, миграция `0005` безопасна. Найденное исправить.
- [ ] **Step 3: Показать владельцу** статистику `python -m minsk_realty.analytics assessment` после Task 3 и **спросить согласие на выкатку**.
- [ ] **Step 4: Выкатка (после «да»):** проверить, что проход не идёт (`source_runs` без `running`); `docker compose up -d --build migrate collector web bot`; проверить логи бота («Run polling»), `/objects/<id>` (фото целиком, нет минусов), первый проход сборщика (в `extra` появились `facts`, `kitchen_m2`).
- [ ] **Step 5: Владелец ищет в боте сам.** Обновить `docs/status.md`; коммит; push — после согласия владельца.

---

## Результаты разведки

**Индекс строится дольше 60 с (70 с). До Task 4 нужно обсудить ускорение с владельцем.**

Разведка выполнена 30.10.2026 скриптом `scripts/search_recon.py` (по одной странице выдачи на подкоманду, через `PoliteClient`). Текст описаний не печатался и не сохранялся.

### Описание Kufar

- Брать **`body_short`**: поле `body` в выдаче есть, но **пустое** (длина 0 у всех 20 объявлений страницы). Полного описания в выдаче нет.
- `body_short` у всех 20 объявлений ровно **150 символов**, то есть обрезан по длине; маркера обрыва (`…` / `...`) в конце нет. Это первые 150 символов, поиск по описанию идёт только по ним.

### Kufar: код `v` → подпись `vl`

| Параметр | Код → подпись |
|---|---|
| `bathroom` | 1 → Раздельный, 2 → Совмещенный, 3 → Два |
| `balcony` | 1 → Есть, 2 → Нет, 3 → Лоджия, 4 → Два |
| `flat_windows_side` (список, `v` и `vl` идут в одном порядке) | 1 → Во двор, 3 → В парк, 4 → На улицу, 5 → Юг, 7 → Восток, 8 → Запад |
| `possible_exchange` | булево: `v` = false, `vl` = «-» (на живой странице не встретилось, значение «true» не наблюдалось; по фикстуре 23.10) |

Живая страница: `bathroom` 1, 2; `balcony` 1, 3, 4; окна 1, 3, 4, 5, 7, 8. Коды 3 для `bathroom` и 2 для `balcony` взяты из фикстуры `search_sell_minsk.2026-10-23.json`. `possible_exchange` на живой странице отсутствует у всех 20 объявлений, в фикстуре присутствует только со значением false.

### Realt: код → подпись (`__NEXT_DATA__` страницы выдачи, `shortcuts`)

| Поле | Код → подпись |
|---|---|
| `balconyType` | 0 → Нет, 2 → Лоджия, 27 → Есть |
| `heating` | 0 → нет, 4 → электрическое, 5 → паровое на твёрдом топливе, 6 → паровое на газу, 7 → паровое на жидком топливе, 8 → паровое, 9 → печное, 10 → центральное |
| `wallMaterial` | **справочника в данных нет**; в объявлениях страницы значение `null` (в фикстуре 23.10 `null` у всех 360 объектов). Подписей не выдумывать: ключа `walls` из Realt не будет, пока Realt не отдаст справочник |

Справочники лежат в `pageProps.shortcuts[…].shortcuts` (`balconyType` — элемент 0, `heating` — элемент 6), формат `{label, value}`, как у `repairState`.

### Время индекса

`python -m minsk_realty.analytics assessment` в контейнере `web`: **70 с** (скрипт сам пишет 68.8 с; 14 880 активных объектов). Память контейнера `web`: до прогона 440 МиБ, **пик 710 МиБ** (из 3.8 ГиБ), то есть около +270 МиБ на построение.

### Превью ссылок

Отправлено 4 сообщения в `ADMIN_CHAT_ID` (по одному на площадку: kufar, realt, onliner, domovita; текст — название площадки и ссылка) с `link_preview_options {"prefer_large_media": true, "show_above_text": true}`. Все четыре ответа `ok=true`, в ответе API эхом пришли `url` (ссылка превью) и `show_above_text: true` (`prefer_large_media` Telegram в ответе не возвращает, это не ошибка). Есть ли у превью фото и большая картинка — **владелец смотрит сам**:

| Площадка | API | Фото в превью |
|---|---|---|
| kufar | ok | владелец подтверждает |
| realt | ok | владелец подтверждает |
| onliner | ok | владелец подтверждает |
| domovita | ok | владелец подтверждает |
