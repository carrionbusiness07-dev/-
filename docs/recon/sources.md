# Разведка площадок (задача №1, Стас)

Дата: 22.10.2026.

**Как проверялось.** Сеть рабочего контейнера не пускает на kufar.by и realt.by. Поэтому эндпоинты и поля сверены двумя способами:
1. по **рабочему open-source коду** ботов, которые сейчас ходят на эти площадки (ссылки ниже);
2. **одним пробным запросом** через внешний сервис загрузки страниц.

Пока ни один ответ площадки не сохранён живьём в фикстуру. Первые живые фикстуры снимает `scripts/capture_fixture.py` на сервере в Беларуси.

## Главный вывод для инфраструктуры

**Kufar закрыт для запросов не из Беларуси.** На пробный запрос к поисковому API пришёл ответ `403` со страницей «Доступ ограничен — Вы вошли из локации, где доступ к сайту ограничен». Отсюда:
- **сборщик нужно запускать на сервере в Беларуси** (белорусский VPS или домашний сервер);
- подменять геолокацию через прокси не будем: это обход ограничения площадки (см. SKILL.md, «Бережность к площадкам»).

Realt.by из-за рубежа отвечает `200`.

## Kufar

| Что | Значение | Статус |
|---|---|---|
| Поиск | `GET https://api.kufar.by/search-api/v2/search/rendered-paginated` | Код в репозиториях [re_kufar][re_kufar], [kufar-parser-bot][kpb], [aptrentmsq][apt] |
| Параметры | `cat=1010` (квартиры), `typ=sell` (продажа; `let` — аренда), `cur=USD`, `gtsy=country-belarus~province-minsk~locality-minsk`, `lang=ru`, `size` (до 200), `sort=lst.d` | В коде; `typ=sell` взят из разбора ссылок «kupit» в [kufar-parser-bot][kpb] |
| Пагинация | Ответ `pagination.pages[]`, у элемента `label == "next"` берётся `token` и передаётся параметром `cursor` | В коде [re_kufar][re_kufar] (statistics.ts) |
| Объявление | `ad_id`, `list_id`, `ad_link`, `list_time` (время публикации или поднятия), `company_ad` (агентство), `price_usd` и `price_byn` (строки в центах и копейках), `currency`, `images[].path`, `account_id`, `ad_parameters[]`, `account_parameters[]` | Типы в [re_kufar][re_kufar] (types.ts) и фикстура аренды в [room-rental-parser][rrp] |
| `ad_parameters` (`p`) | `rooms`, `size` (общая площадь), `region` (vl «Минск»), `area` (район), `coordinates` `[lon, lat]`, `metro` | Фикстура аренды [room-rental-parser][rrp] |
| `account_parameters` (`p`) | `address` (адрес объекта), **`name` (имя, персональные данные, вырезается)** | Там же |
| Фото | `https://rms.kufar.by/v1/gallery/<path>` | В коде |
| Ссылка | `https://re.kufar.by/vi/<ad_id>` | В коде |

**Не подтверждено (UNVERIFIED):**
- ключи этажа и этажности в продаже;
- значения поля `currency` (`USD`/`BYR`?);
- есть ли признак перевыкладывания или поднятия (`list_time` меняется при поднятии?).

Без этажа склейка уровня 1 по Kufar пока слабая, так что первая живая фикстура — приоритет.

## Realt.by

| Что | Значение | Статус |
|---|---|---|
| Выдача | `GET https://realt.by/sale/flats/?page=N`, страница Next.js | Ответ `200` из-за рубежа, проверено 22.10 |
| Данные | `<script id="__NEXT_DATA__">` → `props.pageProps.objects[]`, `props.pageProps.pagination.totalCount` | Код и фикстура аренды в [room-rental-parser][rrp] |
| Объект | `code` (id в ссылке), `uuid`, `price`, `priceCurrency` (ISO 4217: 840 USD, 933 BYN), `priceRates` (пересчёт по валютам), **`priceChangeDate`, `priceChangeDirection`**, `rooms`, `areaTotal`, `storey`, `storeys`, `streetName`, `houseNumber`, `buildingNumber`, `address`, `townName`, `location` `[lon, lat]`, `agencyName`/`agencyUuid`/`companyName`, `createdAt`, `updatedAt`, `images[]` (URL), `userUuid` | Там же |
| Персональные данные | `contactPhones`, `contactName`, `contactEmail`, `userUuid` — вырезаются, `userUuid` перед этим превращается в псевдоним `seller_key` | — |
| Ссылка | `https://realt.by/sale-flats/object/<code>/` | Видна на главной realt.by |

**Находка:** Realt сам отдаёт `priceChangeDate` и `priceChangeDirection`, то есть дату и направление последнего изменения цены. Это бесплатная часть истории ещё до того, как мы её накопили.

**Не подтверждено:**
- поля продажи на живой странице (структура взята из аренды);
- входят ли в `/sale/flats/` объекты Минского района (адаптер оставляет только `townName == "Минск"`);
- поле городского района, в образце его не было.

## Onliner и Domovita (следующий этап)

- Onliner: в рабочем коде используется `https://ak.api.onliner.by/search/apartments`, объекты — `https://r.onliner.by/ak/apartments/<id>` ([kufar-parser-bot][kpb], [room-rental-parser][rrp]). В коде это аренда (`ak`). Для продажи (`pk`) эндпоинт не проверен.
- Domovita: не исследовалась.

## Решение по продавцу: псевдоним вместо id

Для события `relisted` («перевыложил тот же продавец») нужен признак «тот же продавец». Поэтому `account_id` (Kufar) и `userUuid` (Realt) хранятся только как `seller_key = sha256(соль + площадка + id)[:32]`. Соль задаётся переменной окружения `SELLER_KEY_SALT`. Исходный id в БД не попадает.

## Что сделано в коде

- `src/minsk_realty/sources/kufar.py`, `realt.py` — адаптеры по контракту `search` / `parse` / `resolve_url`.
- `src/minsk_realty/sources/base.py` — `PoliteClient`: паузы 2–5 с между страницами, backoff на 403/429, остановка источника после трёх отказов подряд.
- `src/minsk_realty/privacy.py` — вырезание персональных данных и псевдоним продавца.
- `tests/` — 14 тестов на синтетических фикстурах, без сети.
- `scripts/capture_fixture.py` — снять первую живую фикстуру на сервере в Беларуси.

## Следующие шаги

1. Арендовать VPS в Беларуси, запустить `capture_fixture.py kufar` и `realt`, закрыть пункты UNVERIFIED. Ответственные: Глеб (сервер) и Стас (поля).
2. Схема БД и миграции (задача №2).
3. Сборщик по расписанию раз в 30–60 минут (задача №3).

[re_kufar]: https://github.com/anyw1n/re_kufar
[kpb]: https://github.com/mavkasgit/kufar-parser-bot
[apt]: https://github.com/hellbowe/aptrentmsq
[rrp]: https://github.com/MikeBor-gif/room-rental-parser
