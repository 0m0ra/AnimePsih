# Сравнение типов кеширования (`Cache-Aside` / `Write-Through` / `Write-Back`)

## Что собрано

Одно и то же приложение (FastAPI + PostgreSQL + Redis), три варианта стратегии
работы с кешем. Стратегия выбирается переменной окружения `CACHE_STRATEGY`,
поэтому код приложения остаётся идентичным во всех трёх прогонах — отличается
только класс стратегии (`app/strategies.py`).

| Компонент       | Реализация                                              |
| --------------- | ------------------------------------------------------- |
| `application`   | `FastAPI` + `uvicorn`, см. `app/main.py`                |
| `cache`         | `Redis 7`                                               |
| `БД`            | `PostgreSQL 15`                                         |
| `load-generator`| Самописный многопоточный, `loadgen/loadgen.py`          |
| `runner`        | `loadgen/run_all.py` — гонит все 9 комбинаций           |
| `tests`         | `pytest`, 8 тестов, `tests/test_strategies.py`          |

API:

* `GET  /items/{id}` — чтение (через стратегию).
* `PUT  /items/{id}` — запись (через стратегию).
* `GET  /metrics` — серверные метрики (hit/miss, db read/write, throughput, latency).
* `POST /admin/seed` — посеять `count` ключей и прогреть кеш.
* `POST /admin/reset` — сбросить redis + БД + метрики между прогонами.
* `POST /admin/flush_writeback` — принудительный сброс write-back буфера.
* `GET  /admin/writeback_pending` — сколько dirty-ключей ждёт записи.

## Описание тестов

Условия идентичны для всех трёх стратегий:

* `keys = 1000` — пространство ключей (одно и то же сидируется в БД и прогревается в кеш).
* `duration = 15 секунд` на прогон.
* `workers = 16` параллельных HTTP-клиентов.
* `seed = 42` — детерминированный выбор ключей и операций.
* Между прогонами:
  1. `flushdb` Redis,
  2. `TRUNCATE items` в Postgres,
  3. перезаливка `keys` ключей в БД,
  4. прогрев кеша `keys` ключами,
  5. сброс счётчиков метрик.

Три профиля нагрузки (одинаковые для всех стратегий):

* `read_heavy` — `80% read / 20% write`,
* `balanced` — `50% read / 50% write`,
* `write_heavy` — `20% read / 80% write`.

Команда запуска (стек поднят `docker compose up -d`):

```bash
python -m loadgen.run_all --duration 15 --workers 16 --keys 1000 --out results
```

Сырые данные: `results/summary.json`, `results/summary.csv`, и
`results/<strategy>__<workload>.json` для каждого прогона.

## Метрики (что и как считаем)

| Метрика               | Откуда берётся                                                                                  |
| --------------------- | ----------------------------------------------------------------------------------------------- |
| `throughput (req/s)`  | client-side: `client_requests / wall_clock_sec`                                                 |
| Средняя задержка      | client-side: средняя HTTP-latency от `httpx` по всем запросам                                   |
| Обращений в БД        | server-side: `db_reads + db_writes`, инкрементятся в `app/metrics.py` на каждый `SELECT/INSERT/UPDATE` |
| Hit rate кеша         | server-side: `cache_hits / (cache_hits + cache_misses)`                                         |
| Pending writeback     | `SCARD writeback:dirty` в Redis в момент окончания прогона                                      |
| Flushed post-run      | сколько ключей слил финальный `flush_writeback` после прогона                                   |

## Таблица результатов

```
strategy,workload,read_ratio,throughput_rps,avg_latency_ms,p95_latency_ms,cache_hits,cache_misses,hit_rate,db_reads,db_writes,db_total,writeback_pending_pre_flush,writeback_flushed_post,errors
cache_aside,read_heavy,0.8,1328.70,12.019,21.385,12840,3062,0.8074,3062,4038,7100,0,0,0
cache_aside,balanced,0.5,935.35,17.058,26.917,3717,3293,0.5302,3293,7030,10323,0,0,0
cache_aside,write_heavy,0.2,722.40,22.108,32.026,608,1552,0.2815,1552,8689,10241,0,0,0
write_through,read_heavy,0.8,1300.37,12.283,22.947,15577,0,1.0000,0,3937,3937,0,0,0
write_through,balanced,0.5,1045.68,15.265,26.259,7807,0,1.0000,0,7886,7886,0,0,0
write_through,write_heavy,0.2,822.15,19.424,29.219,2446,0,1.0000,0,9904,9904,0,0,0
write_back,read_heavy,0.8,1585.39,10.075,20.905,18987,0,1.0000,0,1400,1400,625,625,0
write_back,balanced,0.5,1512.65,10.556,20.405,11212,0,1.0000,0,1400,1400,795,795,0
write_back,write_heavy,0.2,1448.70,11.023,20.426,4296,0,1.0000,0,1200,1200,802,802,0
```

В компактном виде:

| Стратегия       | Профиль        | RPS        | avg latency, ms | p95, ms | Hit rate | DB reads | DB writes | Σ DB ops |
| --------------- | -------------- | ---------- | --------------- | ------- | -------- | -------- | --------- | -------- |
| `cache_aside`   | read_heavy     | **1329**   | 12.0            | 21.4    | 0.81     | 3062     | 4038      | 7100     |
| `cache_aside`   | balanced       | 935        | 17.1            | 26.9    | 0.53     | 3293     | 7030      | 10323    |
| `cache_aside`   | write_heavy    | 722        | 22.1            | 32.0    | 0.28     | 1552     | 8689      | 10241    |
| `write_through` | read_heavy     | 1300       | 12.3            | 22.9    | **1.00** | 0        | 3937      | 3937     |
| `write_through` | balanced       | 1046       | 15.3            | 26.3    | **1.00** | 0        | 7886      | 7886     |
| `write_through` | write_heavy    | 822        | 19.4            | 29.2    | **1.00** | 0        | 9904      | 9904     |
| `write_back`    | read_heavy     | **1585**   | **10.1**        | **20.9**| **1.00** | 0        | 1400      | **1400** |
| `write_back`    | balanced       | **1513**   | **10.6**        | **20.4**| **1.00** | 0        | 1400      | **1400** |
| `write_back`    | write_heavy    | **1449**   | **11.0**        | **20.4**| **1.00** | 0        | 1200      | **1200** |

Полужирным — лидер строки/колонки.

### Поведение `Write-Back` при накоплении записей

При тех же входных данных write-back делает кардинально меньше обращений в БД,
потому что запись идёт сначала в кеш, а в Postgres попадает асинхронным фоновым
flusher-ом раз в `WRITEBACK_FLUSH_INTERVAL = 2.0` сек батчами по
`WRITEBACK_BATCH_SIZE = 200`. Несколько обновлений одного ключа в пределах
интервала схлопываются в одну запись — потому что dirty-список — это `SET` в
Redis (`writeback:dirty`).

Из таблицы:

* `write_heavy`: клиент сделал ~17 452 запроса на запись, в БД ушло **1200** во
  время прогона + **802** догнало финальным `flush_writeback`. Итого ~2002 DB
  writes на ~17 452 клиентских записи — **~8.7× меньше** обращений в БД.
* `balanced`: ~11 343 клиентских записи → 1400 + 795 = 2195 DB writes
  (~5.2× меньше).
* `read_heavy`: ~4038 клиентских записи → 1400 + 625 = 2025 DB writes (примерно
  столько же, потому что 1000 ключей и поток успевает записать «почти всё»).

Поле `writeback_pending_before_flush` в JSON (например, 802 в write_heavy)
наглядно показывает «что висит в кеше, но ещё не доехало в БД» в момент,
когда нагрузка прекратилась — это та самая «накопленная очередь записей»
из условия задачи. Если в этот момент потеряется Redis, эти записи будут
утеряны — это известная цена write-back.

## Выводы

### Что лучше для чтения (`read_heavy`, 80/20)

`Write-Back` ≈ `Cache-Aside` по latency для самих чтений, но `Write-Back` всё
равно даёт лучший throughput (`1585` против `1329` rps) — потому что **20%
записей** в cache-aside вынуждены идти в БД и инвалидировать кеш, что
снижает hit rate до 0.81 и тянет латентность вверх. `Write-Through` ведёт
себя посередине: hit rate 1.0, но запись синхронно идёт в БД, поэтому RPS
ниже write-back'а.

→ **Лучший для чтения:** `Write-Back`. Если боимся потерять буфер — `Write-Through`.

### Что лучше для записи (`write_heavy`, 20/80)

Здесь разрыв максимальный:

* `cache_aside` — **722 rps**, **10241** DB ops, hit rate 0.28 (записи
  инвалидировали кеш быстрее, чем читатели его прогревали);
* `write_through` — 822 rps, 9904 DB ops, hit rate 1.0;
* `write_back` — **1449 rps**, **1200** DB ops в рантайме (+802 в финальном
  drain).

→ **Лучший для записи:** `Write-Back`. Он коалесцирует повторные апдейты
одного ключа и не блокирует клиента на синхронной записи в БД. Цена —
возможные потери при сбое и согласованность «eventually» относительно БД.

### Что лучше для смешанной нагрузки (`balanced`, 50/50)

* `cache_aside` страдает (935 rps, hit rate 0.53) — пишутся те же ключи,
  что и читаются, кеш постоянно инвалидируется;
* `write_through` стабилен (1046 rps, hit rate 1.0) и даёт сильную
  гарантию консистентности кеша и БД;
* `write_back` лидирует (1513 rps, 1400 DB ops против 7886) ценой задержки
  записи в БД.

→ **Лучший для смешанной нагрузки:** `Write-Through`, если важна
консистентность; `Write-Back`, если важен throughput и допустим оконный
риск потери последних апдейтов.

### Сводно

| Цель                                            | Победитель        |
| ----------------------------------------------- | ----------------- |
| Минимум обращений в БД                          | `Write-Back`      |
| Максимум throughput на любой нагрузке           | `Write-Back`      |
| Hit rate под нагрузкой со смешением read/write  | `Write-Through` / `Write-Back` (оба 1.0) |
| Консистентность кеш↔БД сразу после ответа клиенту | `Write-Through` |
| Простота реализации                             | `Cache-Aside`     |
| Устойчивость к сбоям без рисков потери записи   | `Cache-Aside` / `Write-Through` |

`Cache-Aside` оказывается худшим в любой смешанной нагрузке именно из-за
write-around инвалидации: каждый PUT обнуляет запись в кеше, и следующий
читатель идёт в БД. Это видно по `cache_misses` в таблице — у cache_aside
они ненулевые во всех прогонах, у двух остальных — ровно ноль.

## Тесты

`pytest tests/` (8 тестов, проходят на поднятом docker-compose):

```
tests/test_strategies.py::test_cache_aside_miss_then_hit              PASSED
tests/test_strategies.py::test_cache_aside_write_around_invalidates   PASSED
tests/test_strategies.py::test_write_through_writes_to_both           PASSED
tests/test_strategies.py::test_write_through_update_overwrites_cache  PASSED
tests/test_strategies.py::test_write_back_writes_only_cache_initially PASSED
tests/test_strategies.py::test_write_back_eventually_persists         PASSED
tests/test_strategies.py::test_write_back_batches_many_writes         PASSED
tests/test_strategies.py::test_write_back_read_after_write_hits_cache PASSED
8 passed in 0.89s
```

Тесты подтверждают именно стратегические инварианты, а не просто «не упало»:

* cache_aside: после PUT кеш инвалидируется (`redis.get(key) is None`), а
  следующий GET репопулирует;
* write_through: после PUT в кеше и БД лежит одно и то же значение;
* write_back: сразу после PUT БД ещё пуста, а кеш уже наполнен; через
  несколько интервалов flusher-а БД догоняет; 100 PUT'ов сливаются меньше,
  чем за 100 транзакций (`writeback_flushes < 100`).

## Как воспроизвести

```bash
# 1. поднять стек
docker compose up -d --build

# 2. (опционально) тесты
pip install -r requirements.txt
PYTHONPATH=. pytest -v

# 3. полный бенчмарк
PYTHONPATH=. python -m loadgen.run_all --duration 15 --workers 16 --keys 1000 --out results

# 4. смотреть results/summary.csv и results/<strategy>__<workload>.json
```

## Логи прогонов

Скрин консоли запуска `run_all` (в формате текста, чтобы коммитить в репозиторий):

```
[runner] restarting app with strategy=cache_aside
=== cache_aside__read_heavy ===
=== cache_aside__balanced ===
=== cache_aside__write_heavy ===
[runner] restarting app with strategy=write_through
=== write_through__read_heavy ===
=== write_through__balanced ===
=== write_through__write_heavy ===
[runner] restarting app with strategy=write_back
=== write_back__read_heavy ===
=== write_back__balanced ===
=== write_back__write_heavy ===
[runner] wrote results/summary.json
[runner] wrote results/summary.csv
```

Для просмотра логов сервиса в момент прогона:

```bash
docker compose logs -f app
```
