# Отчёт: Сравнение стратегий кеширования

**Задание 3 | Распределённые системы**

---

## 1. Описание системы

Реализованы три стратегии кеширования в рамках единой системы:

- **Стек:** Python (FastAPI) + Redis + PostgreSQL + самописный load generator
- **Инфраструктура:** Docker Compose
- **Источник данных:** таблица `items` (1000 ключей)
- **Параметры теста:** 16 воркеров, 15 секунд на каждый прогон

### Реализованные стратегии

**Cache-Aside (Lazy Loading / Write-Around)**
- Чтение: сначала проверяется кеш, при промахе — данные берутся из БД и кладутся в кеш
- Запись: данные идут напрямую в БД, кеш инвалидируется

**Write-Through**
- Чтение: сначала проверяется кеш, при промахе — из БД
- Запись: данные одновременно записываются и в БД, и в кеш

**Write-Back**
- Чтение: сначала проверяется кеш, при промахе — из БД
- Запись: данные сначала попадают только в кеш, в БД сбрасываются асинхронно фоновым потоком (batch flush каждые N секунд)

---

## 2. Описание тестов

Для всех трёх стратегий применялся одинаковый тест с тремя профилями нагрузки:

| Профиль | Чтение | Запись |
|---|---|---|
| read-heavy | 80% | 20% |
| balanced | 50% | 50% |
| write-heavy | 20% | 80% |

- Одинаковый набор данных (1000 ключей)
- Одинаковое число воркеров (16)
- Одинаковая длительность (15 секунд)

---

## 3. Результаты тестирования

### 3.1 Throughput (req/sec)

| Профиль | Cache-Aside | Write-Through | Write-Back |
|---|---|---|---|
| read-heavy | 1 328 | 1 300 | **1 585** |
| balanced | 935 | **1 046** | 1 513 |
| write-heavy | 722 | 822 | **1 449** |

### 3.2 Средняя задержка (мс, client-side)

| Профиль | Cache-Aside | Write-Through | Write-Back |
|---|---|---|---|
| read-heavy | 12.0 | 12.3 | **10.1** |
| balanced | 17.1 | 15.3 | **10.6** |
| write-heavy | 22.1 | 19.4 | **11.0** |

### 3.3 Обращения в БД

| Профиль | Cache-Aside | Write-Through | Write-Back |
|---|---|---|---|
| read-heavy | 7 100 | 3 937 | **2 025** |
| balanced | 10 323 | 7 886 | **2 195** |
| write-heavy | 10 241 | 9 904 | **2 202** |

> Для Write-Back указано финальное значение (включая отложенный flush после теста).

### 3.4 Hit Rate кеша

| Профиль | Cache-Aside | Write-Through | Write-Back |
|---|---|---|---|
| read-heavy | 80.7% | **100%** | **100%** |
| balanced | 53.0% | **100%** | **100%** |
| write-heavy | 28.1% | **100%** | **100%** |

### 3.5 Поведение Write-Back при накоплении записей

Write-Back накапливает записи в Redis (dirty set) и сбрасывает их в БД батчами. По итогам тестов:

| Профиль | Pending перед flush | Записей после flush |
|---|---|---|
| read-heavy | 625 | 2 025 |
| balanced | 795 | 2 195 |
| write-heavy | 802 | 2 202 |

Это означает: на момент завершения теста в кеше оставались несброшенные ключи. После явного финального flush они были записаны в БД. В production при внезапном падении сервиса эти данные могут быть потеряны — ключевой компромисс Write-Back.

---

## 4. Скриншоты логов

### Cache-Aside — read-heavy
```
strategy=cache_aside | requests=19940 | throughput=1328 rps
cache_hits=12840 | cache_misses=3062 | hit_rate=80.7%
db_reads=3062 | db_writes=4038 | db_total=7100
avg_latency=12.0 ms | p95=21.4 ms | p99=28.5 ms
```

### Write-Through — read-heavy
```
strategy=write_through | requests=19514 | throughput=1300 rps
cache_hits=15577 | cache_misses=0 | hit_rate=100%
db_reads=0 | db_writes=3937 | db_total=3937
avg_latency=12.3 ms | p95=22.9 ms | p99=30.7 ms
```

### Write-Back — read-heavy
```
strategy=write_back | requests=23791 | throughput=1585 rps
cache_hits=18987 | cache_misses=0 | hit_rate=100%
db_reads=0 | db_writes=2025 (after flush) | db_total=2025
writeback_flushes=11 | writeback_pending_before_flush=625
avg_latency=10.1 ms | p95=20.9 ms | p99=28.3 ms
```

### Write-Back — write-heavy
```
strategy=write_back | requests=21748 | throughput=1449 rps
cache_hits=4296 | cache_misses=0 | hit_rate=100%
db_reads=0 | db_writes=2202 (after flush) | db_total=2202
writeback_flushes=12 | writeback_pending_before_flush=802
avg_latency=11.0 ms | p95=20.4 ms | p99=26.2 ms
```

---

## 5. Выводы

### Для read-heavy нагрузки
**Лучший вариант: Write-Back**
- Наибольший throughput (1585 rps vs 1328 у Cache-Aside)
- Наименьшая задержка (10.1 мс)
- 100% hit rate, минимум обращений в БД

Write-Through также хорош — 100% hit rate, но немного медленнее из-за синхронной записи в БД.

Cache-Aside проигрывает из-за cache miss при записи (write-around инвалидирует кеш).

### Для write-heavy нагрузки
**Лучший вариант: Write-Back**
- В 2 раза больше throughput, чем Cache-Aside (1449 vs 722 rps)
- Задержка 11 мс против 22 мс у Cache-Aside
- Число обращений в БД в 4–5 раз меньше

Cache-Aside крайне неэффективен при write-heavy: каждая запись инвалидирует кеш, hit rate падает до 28%.

Write-Through лучше Cache-Aside, но всё равно пишет синхронно в БД при каждом запросе.

### Для balanced нагрузки
**Лучший вариант: Write-Back**
- Throughput 1513 rps (Cache-Aside: 935, Write-Through: 1046)
- Задержка 10.6 мс (вдвое ниже Cache-Aside)

### Итоговая таблица

| Критерий | Лучший вариант |
|---|---|
| Максимальный throughput | Write-Back |
| Минимальная задержка | Write-Back |
| Минимум обращений в БД | Write-Back |
| Hit rate | Write-Through = Write-Back (оба 100%) |
| Надёжность данных | Write-Through |
| Простота реализации | Cache-Aside |

**Write-Back** показывает наилучшую производительность во всех сценариях, но требует учёта риска потери данных при сбое до завершения flush. **Write-Through** — лучший выбор, когда важна гарантия сохранности данных при высоком read-heavy трафике. **Cache-Aside** подходит для простых сценариев с преимущественно read-heavy нагрузкой, но неэффективен при частых записях.
