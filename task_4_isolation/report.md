# Аномалии изоляции в SQL — практический разбор

## Что сделано

Поднят `PostgreSQL 15` в `docker-compose.yml`, на нём воспроизведены **все
четыре** классические аномалии параллельных транзакций:

| Аномалия              | Уровень изоляции, на котором показано | Результат                |
| --------------------- | ------------------------------------- | ------------------------ |
| `dirty read`          | `READ UNCOMMITTED`                    | предотвращено самим PG   |
| `non-repeatable read` | `READ COMMITTED`                      | воспроизведено           |
| `phantom read`        | `READ COMMITTED`                      | воспроизведено           |
| `lost update`         | `READ COMMITTED`                      | воспроизведено           |

Для каждой аномалии также показано, **как её избежать** — переключением
уровня изоляции либо явной блокировкой `SELECT ... FOR UPDATE`.

## Стек / структура

```
task_4_isolation/
├── docker-compose.yml         # PostgreSQL 15
├── sql/init.sql               # таблицы accounts, products + сид
├── anomalies/
│   ├── _common.py             # Logger, conn(), reset_*()
│   ├── dirty_read.py
│   ├── non_repeatable_read.py
│   ├── phantom_read.py
│   └── lost_update.py
├── main.py                    # запускает все 4 демо
├── results/                   # логи прогонов (см. ниже)
└── report.md
```

Запуск:

```bash
docker compose up -d
./.venv/bin/pip install "psycopg[binary]"
./.venv/bin/python main.py
```

Каждое демо — это две настоящие параллельные транзакции в отдельных
потоках Python, синхронизированных через `threading.Event`, чтобы шаги
T1/T2 шли в нужном порядке. Все запросы и метки времени пишутся в
`results/<имя>.log`.

## Тестовые данные

`sql/init.sql`:

```sql
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    balance INTEGER NOT NULL
);
INSERT INTO accounts VALUES
    (1, 'Alice', 1000), (2, 'Bob', 500), (3, 'Carol', 200);

CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    price INTEGER NOT NULL
);
INSERT INTO products (category, name, price) VALUES
    ('books','SQL Internals',100),
    ('books','Designing DB',150),
    ('books','Postgres Up&Running',200),
    ('electronics','Laptop',500),
    ('electronics','Phone',800);
```

---

## 1. Dirty read

**Идея.** T2 читает данные, которые T1 *ещё не закоммитил* (а потом T1
делает `ROLLBACK`).

**Шаги воспроизведения:**

| шаг | T1                                                | T2                                |
| --- | ------------------------------------------------- | --------------------------------- |
| 1   | `BEGIN ISOLATION LEVEL READ UNCOMMITTED;`         |                                   |
| 2   | `UPDATE accounts SET balance=9999 WHERE id=1;`    |                                   |
| 3   |                                                   | `BEGIN ISOLATION LEVEL READ UNCOMMITTED;` |
| 4   |                                                   | `SELECT balance FROM accounts WHERE id=1;` |
| 5   | `ROLLBACK;`                                       |                                   |

**Результат (лог `results/dirty_read.log`):**

```
======================================================================
DIRTY READ — PostgreSQL @ READ UNCOMMITTED
======================================================================
[   10.9 ms]  T1 | BEGIN (READ UNCOMMITTED)
[   11.7 ms]  T1 | UPDATE balance=9999 WHERE id=1 -> 9999 (NOT committed)
[   19.5 ms]  T2 | BEGIN (READ UNCOMMITTED)
[   20.3 ms]  T2 | SELECT balance WHERE id=1 -> 1000
[   20.5 ms]  T2 | verdict: PREVENTED (no dirty read)
[   20.6 ms]  T1 | ROLLBACK (discarding update)
[   27.6 ms]  -- | final committed balance = 1000 (T1 rolled back -> 1000 expected)
```

**Замечание про PostgreSQL.** В PG `READ UNCOMMITTED` намеренно поднят до
`READ COMMITTED` — стандарт SQL это явно разрешает. Поэтому даже когда
мы прямо просим этот уровень, T2 видит **1000** (последнее
закоммиченное значение), а не **9999**. То же поведение наблюдается в
Oracle. Грязное чтение «по-настоящему» можно увидеть, например, в
`MySQL/InnoDB` при `SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED`.

**Как избежать (если БД его допускает):**

* использовать уровень `READ COMMITTED` или выше — это же делает PG по
  умолчанию;
* никогда не строить логику на чтении незакоммиченных данных, даже если
  СУБД их выдаёт.

---

## 2. Non-repeatable read

**Идея.** T1 читает строку дважды в рамках одной транзакции. Между
чтениями T2 успевает изменить строку и закоммитить — T1 на втором
чтении видит другое значение.

**Шаги воспроизведения (на `READ COMMITTED`):**

| шаг | T1                                                       | T2                                     |
| --- | -------------------------------------------------------- | -------------------------------------- |
| 1   | `BEGIN ISOLATION LEVEL READ COMMITTED;`                  |                                        |
| 2   | `SELECT balance FROM accounts WHERE id=1;` → **1000**    |                                        |
| 3   |                                                          | `BEGIN; UPDATE accounts SET balance=1500 WHERE id=1; COMMIT;` |
| 4   | `SELECT balance FROM accounts WHERE id=1;` → **1500** ⚠ |                                        |
| 5   | `COMMIT;`                                                |                                        |

**Результат — анамалия видна (лог `results/non_repeatable_read.log`):**

```
======================================================================
NON-REPEATABLE READ — T1 @ READ COMMITTED
======================================================================
[   14.9 ms]  T1 | BEGIN (READ COMMITTED)
[   15.7 ms]  T1 | SELECT balance WHERE id=1 -> 1000
[   23.6 ms]  T2 | BEGIN (READ COMMITTED)
[   24.5 ms]  T2 | UPDATE balance=1500 WHERE id=1
[   26.4 ms]  T2 | COMMIT
[   26.9 ms]  T1 | SELECT balance WHERE id=1 -> 1500 (re-read)
[   27.1 ms]  T1 | COMMIT
[   27.2 ms]  -- | ANOMALY: first=1000 second=1500
```

**Тот же сценарий, T1 на `REPEATABLE READ` — анамалия исчезает:**

```
======================================================================
NON-REPEATABLE READ — T1 @ REPEATABLE READ
======================================================================
[   44.3 ms]  T1 | BEGIN (REPEATABLE READ)
[   45.0 ms]  T1 | SELECT balance WHERE id=1 -> 1000
[   52.0 ms]  T2 | BEGIN (READ COMMITTED)
[   52.9 ms]  T2 | UPDATE balance=1500 WHERE id=1
[   54.5 ms]  T2 | COMMIT
[   55.0 ms]  T1 | SELECT balance WHERE id=1 -> 1000 (re-read)
[   55.2 ms]  T1 | COMMIT
[   55.3 ms]  -- | PREVENTED: both reads = 1000 (snapshot held)
```

**Как избежать.**

* `REPEATABLE READ` или `SERIALIZABLE` — T1 будет видеть снимок,
  зафиксированный на старте транзакции.
* Если устраивает блокировка — `SELECT ... FOR SHARE` на `READ
  COMMITTED`: T2 не сможет обновить строку, пока T1 не закоммитит.

---

## 3. Phantom read

**Идея.** T1 дважды агрегирует / выбирает диапазон строк. Между
чтениями T2 вставляет новую строку, попадающую в условие — она
«призрачно» появляется во втором чтении.

**Шаги воспроизведения (на `READ COMMITTED`):**

| шаг | T1                                                          | T2                                                    |
| --- | ----------------------------------------------------------- | ----------------------------------------------------- |
| 1   | `BEGIN ISOLATION LEVEL READ COMMITTED;`                     |                                                       |
| 2   | `SELECT COUNT(*) FROM products WHERE category='books';` → **3** |                                                   |
| 3   |                                                             | `BEGIN; INSERT INTO products(...,'books',...); COMMIT;` |
| 4   | `SELECT COUNT(*) FROM products WHERE category='books';` → **4** ⚠ |                                                   |
| 5   | `COMMIT;`                                                   |                                                       |

**Результат — анамалия видна (лог `results/phantom_read.log`):**

```
======================================================================
PHANTOM READ — T1 @ READ COMMITTED
======================================================================
[   13.5 ms]  T1 | BEGIN (READ COMMITTED)
[   14.5 ms]  T1 | SELECT COUNT(*) WHERE category='books' -> 3
[   20.7 ms]  T2 | BEGIN (READ COMMITTED)
[   21.2 ms]  T2 | INSERT INTO products ('books','Phantom Book',99)
[   22.7 ms]  T2 | COMMIT
[   23.0 ms]  T1 | SELECT COUNT(*) WHERE category='books' -> 4 (re-read)
[   23.1 ms]  T1 | COMMIT
[   23.2 ms]  -- | ANOMALY: first=3 second=4 (phantom appeared)
```

**Тот же сценарий, T1 на `REPEATABLE READ` — фантом не появляется:**

```
======================================================================
PHANTOM READ — T1 @ REPEATABLE READ
======================================================================
[   36.2 ms]  T1 | BEGIN (REPEATABLE READ)
[   37.0 ms]  T1 | SELECT COUNT(*) WHERE category='books' -> 3
[   44.3 ms]  T2 | BEGIN (READ COMMITTED)
[   45.1 ms]  T2 | INSERT INTO products ('books','Phantom Book',99)
[   46.7 ms]  T2 | COMMIT
[   47.2 ms]  T1 | SELECT COUNT(*) WHERE category='books' -> 3 (re-read)
[   47.3 ms]  T1 | COMMIT
[   47.4 ms]  -- | PREVENTED: both counts = 3 (snapshot held)
```

> По стандарту SQL `REPEATABLE READ` от фантомов не защищает (нужен
> `SERIALIZABLE`), но реализация в PostgreSQL построена на снапшот-
> изоляции, поэтому фантомов на этом уровне уже нет. В MySQL/InnoDB
> для защиты от фантомов используется gap-locking — тоже работает на
> `REPEATABLE READ`.

**Как избежать.**

* `REPEATABLE READ` (PostgreSQL/InnoDB) либо `SERIALIZABLE`.
* На `READ COMMITTED`: явно лочить диапазон — `SELECT ... FOR UPDATE`
  по предикату (но в PG это лочит только существующие строки, поэтому
  чисто как защита от фантома не работает; нужен `SERIALIZABLE`).

---

## 4. Lost update

**Идея.** Классическая гонка «прочитал → посчитал → записал». T1 и T2
читают `balance=1000`, оба считают новый баланс на стороне приложения,
оба пишут — последний коммит затирает первый, инкремент первой
транзакции теряется.

**Шаги воспроизведения (на `READ COMMITTED`, обычный SELECT):**

| шаг | T1                                                   | T2                                                  |
| --- | ---------------------------------------------------- | --------------------------------------------------- |
| 1   | `BEGIN; SELECT balance FROM accounts WHERE id=1;` → **1000** |                                             |
| 2   |                                                      | `BEGIN; SELECT balance FROM accounts WHERE id=1;` → **1000** |
| 3   |                                                      | `UPDATE accounts SET balance=1050 WHERE id=1; COMMIT;` |
| 4   | `UPDATE accounts SET balance=1100 WHERE id=1; COMMIT;` |                                                   |

Ожидание (если бы оба депозита легли): `1000 + 100 + 50 = 1150`.
Факт: `1100` — депозит T2 потерян.

**Результат — анамалия видна (лог `results/lost_update.log`):**

```
======================================================================
LOST UPDATE — plain SELECT (race)
======================================================================
[   15.1 ms]  T1 | BEGIN (READ COMMITTED)
[   15.9 ms]  T1 | SELECT balance FROM accounts WHERE id = 1 -> 1000
[   21.6 ms]  T2 | BEGIN (READ COMMITTED)
[   21.9 ms]  T2 | SELECT balance FROM accounts WHERE id = 1 -> 1000
[   22.2 ms]  T2 | UPDATE balance = 1000 + 50 = 1050
[   23.6 ms]  T1 | UPDATE balance = 1000 + 100 = 1100
[   23.6 ms]  T2 | COMMIT
[   24.1 ms]  T1 | COMMIT
[   30.9 ms]  -- | ANOMALY:   final balance = 1100, expected 1150 (an update was lost)
```

**Тот же сценарий с `SELECT ... FOR UPDATE` — потерь нет:**

```
======================================================================
LOST UPDATE — with SELECT ... FOR UPDATE
======================================================================
[   46.9 ms]  T1 | BEGIN (READ COMMITTED)
[   47.6 ms]  T1 | SELECT balance FROM accounts WHERE id = 1 FOR UPDATE -> 1000
[   48.0 ms]  T1 | UPDATE balance = 1000 + 100 = 1100
[   48.6 ms]  T1 | COMMIT
[   54.6 ms]  T2 | BEGIN (READ COMMITTED)
[   55.6 ms]  T2 | SELECT balance FROM accounts WHERE id = 1 FOR UPDATE -> 1100
[   56.0 ms]  T2 | UPDATE balance = 1100 + 50 = 1150
[   56.7 ms]  T2 | COMMIT
[   65.3 ms]  -- | PREVENTED: final balance = 1150 (= 1000 + 100 + 50)
```

T2 в этом варианте упирается в блокировку строки, дожидается коммита
T1 и **перечитывает** уже новое значение `1100` — после чего корректно
прибавляет к нему свои `+50`.

**Как избежать.**

* Пессимистично: `SELECT ... FOR UPDATE` на нужной строке (показано
  выше).
* Оптимистично: `UPDATE accounts SET balance = balance + :delta WHERE
  id = :id` — единственный оператор, который сам перечитывает строку и
  применяет дельту атомарно. В роли «версии» можно держать
  `version`-колонку и обновлять `WHERE version = :v` с проверкой
  `rowcount`.
* Через изоляцию: `REPEATABLE READ` или `SERIALIZABLE` — второй коммит
  упадёт с `serialization_failure`, и приложение должен сделать retry.

---

## Финальное состояние БД после прогона

```
postgres=# SELECT * FROM accounts ORDER BY id;
 id | owner | balance
----+-------+---------
  1 | Alice |    1150     -- сценарий «lost update + FOR UPDATE» оставил 1150
  2 | Bob   |     500
  3 | Carol |     200

postgres=# SELECT * FROM products ORDER BY id;
 id |  category   |        name         | price
----+-------------+---------------------+-------
  1 | books       | SQL Internals       |   100
  2 | books       | Designing DB        |   150
  3 | books       | Postgres Up&Running |   200
  4 | electronics | Laptop              |   500
  5 | electronics | Phone               |   800
  7 | books       | Phantom Book        |    99   -- остался от REPEATABLE READ-сценария
```

(`id=6` пропущен потому, что `SERIAL` израсходовал значение в первом
прогоне phantom-read, после чего строка была удалена `reset_products()`
перед вторым прогоном.)

## Сводная таблица: чем закрывается каждая аномалия

| Аномалия              | Решение по умолчанию в PostgreSQL                       |
| --------------------- | ------------------------------------------------------- |
| `dirty read`          | уже невозможна — PG не отдаёт незакоммиченные данные    |
| `non-repeatable read` | `REPEATABLE READ` (или `SELECT ... FOR SHARE`)          |
| `phantom read`        | `REPEATABLE READ` в PG (snapshot iso) или `SERIALIZABLE`|
| `lost update`         | `SELECT ... FOR UPDATE`, либо атомарный `SET col=col+x`, либо `REPEATABLE READ` с retry |

## Файлы-артефакты

* `docker-compose.yml`, `sql/init.sql` — окружение и схема.
* `anomalies/dirty_read.py`, `non_repeatable_read.py`, `phantom_read.py`,
  `lost_update.py` — параллельные сценарии, по одному на аномалию.
* `results/dirty_read.log`, `non_repeatable_read.log`,
  `phantom_read.log`, `lost_update.log` — стенограммы прогонов с
  таймстемпами (используются как «скриншоты логов» в этом отчёте).
* `main.py` — раннер, прогоняет все четыре сценария.