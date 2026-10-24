# Оценка квартиры в отчёте — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** В отчёте `/check` и на странице квартиры в кабинете появляются вывод «стоит ли смотреть» по сравнению с похожими квартирами рядом, минусы и плюсы, метро в минутах, «кому подойдёт», «ещё в этом доме», а также карта (кабинет) и точка на карте (бот).

**Architecture:** Вся логика живёт в `analytics`: `geo.py` (расстояния, метро), `comparables.py` (точки сравнения в срезе рынка, поиск похожих), `assessment.py` (вывод, минусы, плюсы, аудитория). `build_check_report` кладёт результаты в новые поля `CheckReport`, а бот и кабинет только показывают их. Миграций нет: год постройки берётся из `snapshots.extra`, координаты из `snapshots.lat/lon`.

**Tech Stack:** Python 3.12, SQLAlchemy 2, FastAPI + Jinja2, aiogram 3, pytest. Leaflet 1.9.4 (лежит в репозитории, `web/static/leaflet/`).

**Spec:** `docs/superpowers/specs/2026-10-24-flat-assessment-design.md`

## Global Constraints

- Сетевые запросы в тестах запрещены. Сеть нужна только двум разовым скриптам Task 1, их запускают руками.
- Никаких телефонов, имён и фото в новых данных и фикстурах.
- Сборщик, склейку, `analytics/hypothesis.py` и недельную сводку не трогаем.
- Все пороги лежат в `AnalyticsConfig` (`analytics/config.py`) и помечены комментарием «допущение, не откалибровано».
- Радиусы 1,5 км → 3 км, минимум 10 точек после отсечения выбросов (`cfg.min_sample`), отсечение через `robust_summary` с `cfg.ppm_bounds_usd` и `cfg.iqr_k`.
- Эпохи: `pre1970` (≤ 1969), `1970_1990`, `1991_2010`, `post2010` (2011 – текущий год), `building` (год > текущего).
- Группы по `diff_pct`: ≤ −15 `suspicious`; (−15, −5] `below`; (−5, +5) `market`; ≥ +5 `above`.
- Минуты пешком = расстояние_м × 1,3 / 83, округление до 5, не меньше 5; дальше 3000 м — «Метро дальше 3 км».
- Тексты для людей — дословно из спеки, разделы 4.1–4.5. Сноска в конце отчёта: «Цены объявлений, а не сделок. Это рекомендация, а не гарантия.»
- Новые файлы данных и статики должны попасть в `[tool.setuptools.package-data]`, иначе их не будет в Docker-образе.
- Тесты запускаются так: `.venv/Scripts/python -m pytest …` (venv создаётся в Task 1, шаг 1).

## Review Focus

1. **Мусорные координаты** — `0,0`, перепутанные lat/lon, точка за пределами Минска. Ожидание: считаем, что координат нет (нет карты, метро и сравнения по радиусу). Тест в Task 2.
2. **Мусорный год постройки** — `0`, `1700`, `2100`, строка `"1978"`, `None`. Ожидание: строка с цифрами разбирается в число; значения вне 1850…(текущий год + 10) считаются неизвестными. Тест в Task 3.
3. **Цена только в BYN без пересчёта или без площади** (`usd_per_m2 is None`). Ожидание: сравнения нет, вывод `unknown`, отчёт строится. Тест в Task 4.
4. **Отчёт по снятому объявлению или объекту без активных объявлений.** Ожидание: сравнение считается по координатам и году, сам объект в выборку не попадает, ничего не падает. Тест в Task 6.
5. **«Ещё в этом доме» без номера дома или с другими объявлениями того же объекта.** Ожидание: без номера дома блока нет; другие объявления той же квартиры соседями не считаются. Тест в Task 5.

---

### Task 1: Окружение, точность координат, станции метро (Sam)

**Files:**
- Create: `scripts/check_coords.py`, `scripts/fetch_metro_stations.py`, `src/minsk_realty/analytics/metro_stations.json`
- Modify: `src/minsk_realty/analytics/config.py` (поле `approx_coord_sources`), `pyproject.toml` (package-data)
- Test: `tests/test_geo.py`

**Interfaces:**
- Produces: `metro_stations.json` — список `{"name": str, "lat": float, "lon": float}`; `AnalyticsConfig.approx_coord_sources: frozenset[str]`; в `scripts/check_coords.py` чистая функция `approx_share(rows: Iterable[tuple[str, float, float, str | None, str | None]]) -> dict[str, float]` (строки: source, lat, lon, street, house → доля объявлений площадки, чьи координаты с точностью до 5 знаков совпадают с объявлением на другой улице или в другом доме).

- [ ] **Step 1: Окружение.** `python -m venv .venv`, `.venv/Scripts/pip install -e ".[dev]"`, затем `.venv/Scripts/python -m pytest -q`. Ожидаемо: все существующие тесты зелёные. Если pip на локальном Python не ставит зависимости, тесты запускать в контейнере: `docker run --rm -v D:\minsk-realty:/app -w /app python:3.12-slim sh -c "pip install -q -e .[dev] && pytest -q"`, и дальше по плану везде так же.
- [ ] **Step 2: Падающий тест** `tests/test_geo.py::test_approx_share_flags_shared_points`: три объявления `kufar` в точке (53.9, 27.5) на домах 1, 3 и 5 и одно объявление `realt` в отдельной точке → `approx_share(rows) == {"kufar": 1.0, "realt": 0.0}`.
- [ ] **Step 3: Реализовать `approx_share`** и `main()`: читает последний снимок каждого активного объявления из БД (`make_session_factory(make_engine())`) и печатает таблицу «площадка / объявлений / доля общих точек».
- [ ] **Step 4: Тест проходит.** `.venv/Scripts/python -m pytest tests/test_geo.py -v` → PASS.
- [ ] **Step 5: Прогон на живой базе.** `docker compose exec collector python /app/scripts/check_coords.py` (если `scripts` нет в образе, после `docker compose up -d --build` он появится: `COPY scripts` уже в Dockerfile). Площадки с долей > 0,10 записать в `approx_coord_sources` с комментарием-датой и цифрами. Таблицу показать владельцу.
- [ ] **Step 6: Станции.** `scripts/fetch_metro_stations.py` один раз запрашивает Overpass API (`https://overpass-api.de/api/interpreter`, запрос: `node["railway"="station"]["station"="subway"](53.80,27.35,54.00,27.80); out;`). Узлы с одинаковым `name` объединяются в один (координаты усредняются). Результат сортируется по имени и пишется в `src/minsk_realty/analytics/metro_stations.json`. Запустить скрипт, файл закоммитить. Станции по памяти не дописывать.
- [ ] **Step 7: Тест данных** `test_metro_stations_file`: файл читается, станций ≥ 25, имена уникальны, все точки внутри 53.80–54.00 / 27.35–27.80.
- [ ] **Step 8: package-data.** Добавить `"analytics/*.json"` в `[tool.setuptools.package-data]`, прогнать тесты (PASS).
- [ ] **Step 9: Commit** `feat: metro stations and coordinate accuracy check`.

### Task 2: `analytics/geo.py` — расстояния и метро (Leo)

**Files:**
- Create: `src/minsk_realty/analytics/geo.py`
- Test: `tests/test_geo.py`

**Interfaces:**
- Consumes: `metro_stations.json` (Task 1).
- Produces:
  - `Station(name: str, lat: float, lon: float)` — frozen dataclass;
  - `load_stations() -> tuple[Station, ...]` — с `functools.cache`;
  - `valid_point(lat: float | None, lon: float | None) -> bool` — True только внутри Минска (53.80–54.00, 27.35–27.80);
  - `distance_m(lat1, lon1, lat2, lon2) -> float` — гаверсинус, R = 6 371 000;
  - `walk_minutes(distance_m: float) -> int`;
  - `Location(lat, lon, approx: bool, station: Station | None, station_m: float | None, walk_min: int | None)` — frozen dataclass; `station is None` значит «метро дальше 3 км»; при `approx=True` поле `walk_min` равно None;
  - `locate(lat, lon, *, approx: bool, stations: tuple[Station, ...] | None = None) -> Location | None` — None, если `not valid_point`.

- [ ] **Step 1: Падающие тесты:**
  - `test_distance_known_pair`: `distance_m(53.9, 27.5, 53.9, 27.6)` ≈ 6 560 м (±1%);
  - `test_walk_minutes`: 600 → 10, 100 → 5, 1500 → 25;
  - `test_locate_nearest_and_far`: на списке из двух станций выбирается ближняя; точка в 3,5 км от обеих → `station is None`;
  - `test_locate_approx_has_no_minutes`: `approx=True` → станция есть, `walk_min is None`;
  - `test_locate_rejects_garbage` (Review Focus 1): `(0, 0)`, `(27.5, 53.9)`, `(None, 27.5)` → None.
- [ ] **Step 2: Запустить** `.venv/Scripts/python -m pytest tests/test_geo.py -v` → FAIL (нет модуля).
- [ ] **Step 3: Реализовать `geo.py`** по интерфейсам выше; станции читаются через `importlib.resources.files("minsk_realty.analytics") / "metro_stations.json"`.
- [ ] **Step 4: Тесты** → PASS.
- [ ] **Step 5: Commit** `feat: distance and nearest metro`.

### Task 3: Год постройки и координаты в срезе рынка (Leo)

**Files:**
- Modify: `src/minsk_realty/analytics/data.py`
- Test: `tests/test_assessment.py` (новый)

**Interfaces:**
- Produces:
  - `SnapRow.lat: float | None = None`, `SnapRow.lon: float | None = None` (в конце, со значениями по умолчанию); `load_market` выбирает `Snapshot.lat, Snapshot.lon`;
  - `parse_year(value: Any, now_year: int) -> int | None`;
  - `building_years(session, object_ids: Iterable[int] | None = None) -> dict[int, int]` — object_id → год из самого свежего снимка, где год есть (Kufar `extra["year_built"]`, Realt `extra["buildingYear"]`); запрос в стиле `platform_reported_drops`;
  - `MarketData.building_years: dict[int, int] = field(default_factory=dict)`; `load_market` заполняет его всегда (с фильтром `object_ids`, если он передан);
  - `MarketData.object_point(object_id: int, approx_sources: frozenset[str]) -> tuple[float, float, bool] | None` — координаты из последнего снимка надёжных объявлений объекта: сначала с площадок не из `approx_sources` (approx=False), иначе любые (approx=True); точки, не прошедшие `geo.valid_point`, пропускаются;
  - `MarketData.listing_point(listing_id: int, approx_sources) -> tuple[float, float, bool] | None` — то же для одного объявления (нужно для отчёта со `scope == "listing"`).

- [ ] **Step 1: Падающие тесты:**
  - `test_parse_year` (Review Focus 2): `1978 → 1978`, `"1978" → 1978`, `0`, `1700`, `2100`, `None`, `"abc"` → None; при `now_year=2026` значение `2030` → 2030;
  - `test_building_years_latest_wins` на БД (фикстура `session` как в `test_analytics.py`): два снимка Kufar с `extra={"year_built": 1975}` и позже `1976` → `{oid: 1976}`;
  - `test_object_point_prefers_precise_source`: объект с объявлениями kufar и realt, `approx_sources={"kufar"}` → координаты realt и `approx=False`; только kufar → `approx=True`.
- [ ] **Step 2: Запустить** → FAIL.
- [ ] **Step 3: Реализовать** по интерфейсам.
- [ ] **Step 4: Тесты + весь набор** `.venv/Scripts/python -m pytest -q` → PASS (поля добавлены со значениями по умолчанию, старые позиционные конструкторы работают). `test_report_builds_fast_enough` тоже должен остаться зелёным.
- [ ] **Step 5: Commit** `feat: building year and coordinates in market data`.

### Task 4: `analytics/comparables.py` — похожие квартиры (Leo)

**Files:**
- Create: `src/minsk_realty/analytics/comparables.py`
- Modify: `src/minsk_realty/analytics/medians.py` (`MarketBaseline.points`, заполнение в `compute_baseline`), `src/minsk_realty/analytics/config.py`
- Test: `tests/test_assessment.py`

**Interfaces:**
- Consumes: `geo.distance_m`, `MarketData.building_years`, `MarketData.object_point`, `rooms_bucket`, `robust_summary`, `MedianCell`.
- Produces:
  - `epoch(year: int | None, now_year: int) -> str | None`;
  - `ComparePoint(object_id: int, lat: float, lon: float, rooms: int, epoch: str | None, usd_per_m2: float, house_key: str | None)`, где `house_key = f"{street_norm}|{house}"`, если оба известны, иначе None;
  - `MarketBaseline.points: tuple[ComparePoint, ...] = ()` — активные объекты с ценой, площадью в `cfg.area_bounds_m2` и координатами. Точки с неточных площадок тоже входят: для радиуса 1,5 км точность «до улицы» годится, неточным бывает только целевой объект;
  - `Comparison(step: Literal["near", "wide", "district"], radius_km: float | None, epoch: str | None, rooms: int | None, n: int, median: float, usd_per_m2: float, diff_pct: float, group: Literal["suspicious", "below", "market", "above"], year_known: bool, district: str | None, points: tuple[ComparePoint, ...])` — `points` пусты для `district`;
  - `find_comparison(points, *, object_id: int | None, point: tuple[float, float, bool] | None, rooms: int | None, year: int | None, usd_per_m2: float | None, district_cell: MedianCell | None, now_year: int, cfg) -> Comparison | None`;
  - `price_group(diff_pct: float, cfg) -> str`;
  - конфиг: `compare_radii_km: tuple[float, float] = (1.5, 3.0)`, `group_suspicious_pct: float = -15.0`, `group_below_pct: float = -5.0`, `group_above_pct: float = 5.0`.

Алгоритм `find_comparison`: если `usd_per_m2 is None` → None. Если точки нет или `approx` → сразу шаг `district`. Иначе для каждого радиуса берутся точки с тем же `rooms_bucket`, тем же `epoch` (фильтр пропускается, если год неизвестен), `object_id` не равен своему и расстояние ≤ радиуса. Затем `robust_summary(...)`: если `median is not None`, это и есть результат. Шаг `district`: `district_cell` с `enough`, иначе None.

- [ ] **Step 1: Падающие тесты** (точки строятся вручную, без БД):
  - `test_epoch_bounds`: 1969 → `pre1970`, 1970 → `1970_1990`, 1990 → `1970_1990`, 2010 → `1991_2010`, 2026 → `post2010`, 2027 → `building` (now_year=2026), None → None;
  - `test_comparison_near`: 12 точек той же эпохи в 1 км → `step == "near"`, `n == 12`;
  - `test_comparison_widens_to_3km`: 5 в 1 км + 7 в 2,5 км → `"wide"`;
  - `test_comparison_falls_back_to_district`: 3 точки рядом и `district_cell` с медианой → `"district"`, `points == ()`;
  - `test_comparison_skips_other_epochs_and_self`: 12 точек `pre1970` и цель `post2010` → не `near`; свой `object_id` в выборку не входит;
  - `test_comparison_without_year`: год None → эпоха не фильтруется, `year_known is False`;
  - `test_approx_point_goes_to_district`;
  - `test_no_price_no_comparison` (Review Focus 3): `usd_per_m2=None` → None;
  - `test_price_group_bounds`: −15 → `suspicious`, −14.9 → `below`, −5 → `below`, −4.9 → `market`, 4.9 → `market`, 5 → `above`;
  - `test_baseline_has_points`: `compute_baseline` на `_market(12)` с координатами в снимках → 12 точек.
- [ ] **Step 2: Запустить** → FAIL.
- [ ] **Step 3: Реализовать** `comparables.py`, поле `points` и его заполнение в `compute_baseline` (в том же цикле по объектам, где считается `price`; `now_year = at.year`).
- [ ] **Step 4: Тесты + весь набор** → PASS.
- [ ] **Step 5: Commit** `feat: compare price with similar flats nearby`.

### Task 5: `analytics/assessment.py` — вывод, минусы, плюсы, аудитория, дом (Leo)

**Files:**
- Create: `src/minsk_realty/analytics/assessment.py`
- Test: `tests/test_assessment.py`

**Interfaces:**
- Consumes: `Comparison`, `Location`, уровень торга `Bargaining.level` (`"high" | "medium" | "low" | "unknown"`), `ComparePoint`.
- Produces:
  - `Minus(text: str, hint: str | None)`;
  - `Assessment(kind: Literal["suspicious", "below", "market", "above", "unknown"], verdict: str, minuses: tuple[Minus, ...], pluses: tuple[str, ...], audience: tuple[str, ...])`;
  - `assess(*, comparison: Comparison | None, bargaining_level: str, rooms: int | None, area: float | None, floor: int | None, floors_total: int | None, year: int | None, now_year: int, location: Location | None) -> Assessment`;
  - `same_house(points, *, object_id: int | None, house_key: str | None) -> tuple[float, ...]` — цены за м² соседей по возрастанию; пусто, если `house_key is None`.

Тексты и условия — дословно из спеки: 4.1 (вывод и добавка «торг реален» / «большой скидки не ждите»), 4.2 (минусы — не больше 4, плюсы — не больше 3, порядок как в таблицах), 4.4 (аудитория — 1–2 первых подходящих). «Далеко от метро»: `location` есть и (`location.station is None` или `walk_min > 20`).

- [ ] **Step 1: Падающие тесты**, параметризованные по таблицам спеки:
  - `test_verdict_texts`: по одному случаю на каждую группу и `unknown`; для `market` + `high` → «✅»-текст содержит «торг реален», для `market` + `low` → «большой скидки не ждите»; для `suspicious` уровень торга не добавляется;
  - `test_minuses_rules`: первый этаж; последний (`floor == floors_total == 9`); `floors_total=5, year=1985` → «Скорее всего без лифта» и «Дом 1985 г.»; `year=2028` → «Дом ещё строится» с «сдача в 2028»; метро дальше 3 км; группа `above`; одноэтажный дом (`floors_total=1, floor=1`) → «первый этаж» есть, «последний» нет;
  - `test_minuses_capped_at_four`, `test_pluses_rules` (метро ≤ 10 мин, `post2010`, `below`);
  - `test_audience_rules`: 1 комната / 40 м² / 10 мин → «одному или под сдачу»; 2 комнаты → «паре или небольшой семье»; 3 комнаты, метро дальше 3 км → «семье с машиной»; 3 комнаты / 8 мин → «семье с детьми»;
  - `test_same_house` (Review Focus 5): `house_key=None` → `()`; точки того же `object_id` не считаются; три соседа → цены по возрастанию.
- [ ] **Step 2: Запустить** → FAIL.
- [ ] **Step 3: Реализовать** `assessment.py`.
- [ ] **Step 4: Тесты** → PASS.
- [ ] **Step 5: Commit** `feat: verdict, minuses, pluses and audience`.

### Task 6: Новые поля отчёта и торг по похожим (Leo)

**Files:**
- Modify: `src/minsk_realty/analytics/report.py`, `src/minsk_realty/analytics/bargaining.py` (только текст `median_scope`, если понадобится), `src/minsk_realty/analytics/__init__.py` (экспорт новых типов)
- Test: `tests/test_assessment.py`, `tests/test_analytics.py`

**Interfaces:**
- Consumes: всё из Task 2–5.
- Produces (`CheckReport`, все поля необязательные): `location: Location | None = None`, `comparison: Comparison | None = None`, `assessment: Assessment | None = None`, `same_house: tuple[float, ...] = ()`; `FlatInfo.year: int | None = None`, `FlatInfo.house_key: str | None = None`.

Сборка в `build_check_report` после `current` и `flat`:
- `point = data.object_point(oid, cfg.approx_coord_sources)` для `scope == "object"`, иначе `data.listing_point(target.id, …)`;
- `location = geo.locate(*point[:2], approx=point[2])`, если точка есть;
- `comparison = find_comparison(baseline.points, …, district_cell=median_cell)`;
- в `BargainInputs`: `vs_median_pct = comparison.diff_pct`, если сравнение есть, иначе как сейчас; `median_scope = "похожих квартир"` для шагов `near`/`wide`, иначе `_scope_word(...)` как сейчас;
- `assessment = assess(...)` после `bargaining`;
- `same_house = same_house(baseline.points, object_id=oid, house_key=flat.house_key)`.

- [ ] **Step 1: Падающие тесты** на сценарии `docs`. Фоновые объекты и снимки сценария получают `extra={"year_built": 1978}` и координаты в пределах 1 км; если `add_background` их не задаёт, нужно добавить параметры в `tests/analytics_data.py`:
  - `test_report_has_comparison_and_assessment`: `report.comparison.step == "near"`, `report.assessment.kind` в ожидаемой группе, `report.location.station` из подставленного списка станций (через `monkeypatch` на `geo.load_stations`);
  - `test_bargaining_uses_similar_flats`: причина торга про медиану содержит «похожих квартир»;
  - `test_report_for_removed_listing_still_compares` (Review Focus 4): все объявления объекта неактивны → отчёт `ok`, `comparison` посчитан, объект в `comparison.points` не входит;
  - `test_report_without_coordinates`: снимки без lat/lon → `location is None`, сравнение по району;
  - `test_check_by_listing_id_and_jsonable` (существующий) продолжает проходить: новые поля сериализуются через `to_jsonable`.
- [ ] **Step 2: Запустить** → FAIL.
- [ ] **Step 3: Реализовать.**
- [ ] **Step 4: Весь набор** → PASS, кроме текстовых проверок отчёта, которые Task 7 переписывает (`test_check_text_docs_scenario`, `test_first_time_listing_suggests_watch`). Если они падают уже здесь, пометить `xfail` с причиной «Task 7» и снять пометку в Task 7.
- [ ] **Step 5: Commit** `feat: assessment fields in the check report`.

### Task 7: Текст отчёта для бота (Leo)

**Files:**
- Modify: `src/minsk_realty/analytics/render.py`
- Test: `tests/test_analytics.py` (обновить `test_check_text_docs_scenario`, `test_first_time_listing_suggests_watch`), `tests/test_assessment.py`

**Interfaces:**
- Consumes: поля `CheckReport` из Task 6.
- Produces: `render_check_text` выводит блоки в порядке спеки, раздел 4. Новые иконки в `_ICONS`: `"good": "✅"`, `"warn": "⚠️"`, `"meh": "🟡"`, `"place": "📍"`, `"who": "👥"`, `"plus": "👍"`, `"house": "🏢"`. Вывод — `suspicious` → warn, `below`/`market` → good, `above` → meh, `unknown` → info. Старый «💬 Вывод:» становится «💬 Торг:». Строка `_median_lines` заменяется строкой сравнения (формат спеки, раздел 3), шаг `district` пишется как «медиана района». Пустые блоки не выводятся.

- [ ] **Step 1: Обновить и добавить тесты:**
  - `test_check_text_docs_scenario`: строки в нужном порядке — шапка, «Сейчас», вывод, «📍 Метро», «👥 Подойдёт», «⚠️ Минусы» (если есть), «📊 Цена: … ниже/выше похожих», история, «💬 Торг:», сноска «Цены объявлений, а не сделок. Это рекомендация, а не гарантия.», уверенность склейки, «Наблюдаем рынок с»;
  - `test_first_time_listing_suggests_watch`: вывод и «📊 Цена» стоят до строки «Эту квартиру мы видим впервые»;
  - `test_text_without_location`: нет «📍» и нет «👍 Плюсы: метро рядом»;
  - `test_text_approx_location`: «Метро «…»» без «мин»;
  - `test_text_metro_far`: «Метро дальше 3 км»;
  - `test_text_without_year`: `comparison.year_known is False` → в строке сравнения есть «без учёта возраста дома», а в строке метро нет «Дом … г.»;
  - `test_text_house_line`: при известном годе и этажности строка метро заканчивается «Дом 1978 г., 9 этажей».
- [ ] **Step 2: Запустить** → FAIL.
- [ ] **Step 3: Реализовать** в `render.py`, снять `xfail` из Task 6.
- [ ] **Step 4: Весь набор** → PASS (`tests/test_bot.py` проверяет `startswith("<b>🏠")` — шапка не меняется).
- [ ] **Step 5: Commit** `feat: assessment blocks in the check text`.

### Task 8: Страница квартиры в кабинете и карта (Leo)

**Files:**
- Create: `src/minsk_realty/web/static/leaflet/` (`leaflet.js`, `leaflet.css`, `images/*` из официального архива Leaflet 1.9.4 — скачать один раз, закоммитить)
- Modify: `src/minsk_realty/web/templates/object.html`, `src/minsk_realty/web/views.py` (функция данных для карты), `src/minsk_realty/web/static/khronika.css`, `pyproject.toml` (package-data: `"web/static/leaflet/*"`, `"web/static/leaflet/images/*"`)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `report.location`, `report.comparison`, `report.assessment`, `report.same_house`.
- Produces: `views.map_data(report) -> dict | None` → `{"flat": [lat, lon], "station": {"name", "lat", "lon"} | None, "similar": [[lat, lon, usd_per_m2], …]}`; None, если `location is None` или `location.approx`.

Шаблон: вывод (спека 4.1) — секция над «Торг». Блок «Где находится» — `<div id="map">` высотой 300 px, данные для карты в `<script type="application/json" id="map-data">` (`tojson`). Небольшой inline-скрипт рисует Leaflet-карту: подложка `https://tile.openstreetmap.org/{z}/{x}/{y}.png` с подписью `© OpenStreetMap`, маркер квартиры, кружок станции, серые `circleMarker` похожих квартир с `bindTooltip` цены за м². Под картой адрес, район, строка метро, ссылки `https://yandex.by/maps/?pt={lon},{lat}&z=16` и `https://www.google.com/maps?q={lat},{lon}`. После фактов идут минусы, плюсы, «Кому подойдёт», «Ещё в этом доме». Без `map_data` блок карты не выводится, а строки с адресом и метро остаются.

- [ ] **Step 1: Падающие тесты:**
  - `test_object_page_has_map`: страница объекта сценария содержит `id="map-data"`, `leaflet.js`, вывод из 4.1;
  - `test_map_data_none_for_approx`: `views.map_data` на отчёте с `location.approx=True` → None;
  - `test_leaflet_static_served`: `GET /static/leaflet/leaflet.js` → 200.
- [ ] **Step 2: Запустить** → FAIL.
- [ ] **Step 3: Реализовать.** Стили — в духе `khronika.css` и `DESIGN.md`.
- [ ] **Step 4: Тесты** → PASS. Затем вручную: `docker compose up -d --build web`, открыть `http://localhost:8080/objects/<id>` — карта, точки похожих, подсказки с ценой.
- [ ] **Step 5: Commit** `feat: map and assessment on the object page`.

### Task 9: Точка на карте в боте (Mia)

**Files:**
- Modify: `src/minsk_realty/bot/service.py` (`Reply.location`, заполнение в `_report_reply`), `src/minsk_realty/bot/handlers.py` (оба места, где отвечают отчётом: `/check` и ссылка обычным сообщением)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `report.location`.
- Produces: `reply_location(report: CheckReport) -> tuple[float, float] | None` в `service.py` — координаты, только если `report.location` есть и `not approx`. `Reply.location: tuple[float, float] | None = None` заполняется этой функцией в `_report_reply`. После `message.answer(reply.text, …)` хендлер вызывает `await message.answer_location(lat, lon)`, если `reply.location` задан.

- [ ] **Step 1: Падающие тесты:**
  - `test_check_sends_location_after_report` через `_feed`/`FakeTelegram`: ссылка из сценария → в `calls` после `SendMessage` с отчётом идёт `SendLocation` с координатами квартиры;
  - `test_reply_location`: отчёт сценария → координаты; тот же отчёт с `dataclasses.replace(report, location=replace(report.location, approx=True))` → None; с `location=None` → None.
- [ ] **Step 2: Запустить** → FAIL.
- [ ] **Step 3: Реализовать.**
- [ ] **Step 4: Весь набор** `.venv/Scripts/python -m pytest -q` и `.venv/Scripts/ruff check .` → PASS.
- [ ] **Step 5: Commit** `feat: send location pin after /check`.

### Task 10: Прогон по всему рынку, ревью, выкатка (Leo → Grant)

**Files:**
- Modify: `src/minsk_realty/analytics/__main__.py` (подкоманда `assessment`)
- Test: `tests/test_analytics.py::test_cli` (добавить вызов `assessment`)

**Interfaces:**
- Produces: `python -m minsk_realty.analytics assessment` печатает по активным объектам: всего; доли групп `suspicious/below/market/above/unknown`; доли шагов `near/wide/district`; долю без координат и с неточной точкой; 10 случайных `object_id` для ручного просмотра.

- [ ] **Step 1: Падающий тест** в `test_cli`: `main(["assessment"])` на сценарии печатает строки «suspicious» и «near».
- [ ] **Step 2: Реализовать**, тест → PASS. Commit `feat: assessment stats command`.
- [ ] **Step 3: Выкатить на домашний ПК:** `docker compose up -d --build` (сборщик не трогаем, пересобираются образы). Затем `docker compose exec web python -m minsk_realty.analytics assessment`.
- [ ] **Step 4: Ворота владельца.** Показать владельцу цифры. Если `suspicious` > 10% активных объектов — остановиться и править пороги вместе с ним (только в `config.py`). Владелец открывает 10 случайных квартир из вывода в кабинете и говорит «ок».
- [ ] **Step 5: Ревью Grant** (`grant-guard`) всей ветки: персональные данные, тесты, package-data, ничего лишнего в сборщике и склейке.
- [ ] **Step 6: Обновить `docs/status.md`**: в «Что работает» — новая оценка в отчёте; в «Отложено» — окружение по OpenStreetMap, «что проверить на просмотре», расшифровка кодов Kufar. Commit и push.
