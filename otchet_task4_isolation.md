# Отчёт: Аномалии изоляции в SQL

**Задание 4 | Распределённые системы**

---

## 1. Описание системы

- **СУБД:** PostgreSQL (Docker Compose)
- **Язык:** Python + psycopg (нативный драйвер PostgreSQL)
- **Параллелизм:** threading (две параллельные транзакции на каждую аномалию)

### Тестовая схема

```sql
CREATE TABLE accounts (
    id      INTEGER PRIMARY KEY,
    owner   TEXT    NOT NULL,
    balance INTEGER NOT NULL
);
INSERT INTO accounts VALUES (1,'Alice',1000),(2,'Bob',500),(3,'Carol',200);

CREATE TABLE products (
    id       SERIAL PRIMARY KEY,
    category TEXT NOT NULL,
    name     TEXT NOT NULL,
    price    INTEGER NOT NULL
);
INSERT INTO products VALUES
  ('books','SQL Internals',100),
  ('books','Designing DB',150),
  ('books','Postgres Up&Running',200),
  ('electronics','Laptop',500),
  ('electronics','Phone',800);
```

---

## 2. Выбранные аномалии

Были реализованы и продемонстрированы все четыре аномалии:

1. Dirty Read
2. Non-Repeatable Read
3. Phantom Read
4. Lost Update

---

## 3. Аномалия 1: Dirty Read

### Описание

Транзакция T2 читает данные, которые изменила T1, но ещё не закоммитила. Если T1 откатится — T2 прочитала несуществующее значение.

### Транзакции

| Шаг | T1 | T2 |
|---|---|---|
| 1 | BEGIN (READ UNCOMMITTED) | |
| 2 | UPDATE balance=9999 WHERE id=1 (не коммитит) | |
| 3 | | BEGIN (READ UNCOMMITTED) |
| 4 | | SELECT balance WHERE id=1 |
| 5 | ROLLBACK | |

### Шаги воспроизведения

1. T1 открывает транзакцию с уровнем `READ UNCOMMITTED` и обновляет баланс с 1000 до 9999, не коммитя
2. T2 открывает транзакцию и читает баланс того же аккаунта
3. T1 откатывает изменения

### Результат (лог)

```
[    0.0 ms]  -- | DIRTY READ — PostgreSQL @ READ UNCOMMITTED
[   10.9 ms]  T1 | BEGIN (READ UNCOMMITTED)
[   11.7 ms]  T1 | UPDATE balance=9999 WHERE id=1 -> 9999 (NOT committed)
[   19.5 ms]  T2 | BEGIN (READ UNCOMMITTED)
[   20.3 ms]  T2 | SELECT balance WHERE id=1 -> 1000
[   20.5 ms]  T2 | verdict: PREVENTED (no dirty read)
[   20.6 ms]  T1 | ROLLBACK (discarding update)
[   27.6 ms]  -- | final committed balance = 1000 (T1 rolled back -> 1000 expected)
```

### Вывод

Аномалия **предотвращена PostgreSQL**. PostgreSQL не поддерживает настоящий `READ UNCOMMITTED` — он молча повышает его до `READ COMMITTED`. T2 прочитала значение 1000 (закоммиченное), а не 9999 (незакоммиченное).

### Как избежать

В стандарте SQL `READ COMMITTED` и выше предотвращают грязное чтение. PostgreSQL автоматически гарантирует это начиная с минимального уровня изоляции. В других СУБД (MySQL, SQL Server) следует явно использовать `READ COMMITTED` или выше.

---

## 4. Аномалия 2: Non-Repeatable Read

### Описание

T1 дважды читает одну и ту же строку в рамках одной транзакции и получает разные значения, потому что между чтениями T2 изменила и закоммитила данные.

### Транзакции

| Шаг | T1 | T2 |
|---|---|---|
| 1 | BEGIN (READ COMMITTED) | |
| 2 | SELECT balance → 1000 | |
| 3 | | BEGIN, UPDATE balance=1500, COMMIT |
| 4 | SELECT balance → 1500 (другое значение!) | |
| 5 | COMMIT | |

### Результат (лог) — воспроизведение аномалии

```
[    8.1 ms]  -- | NON-REPEATABLE READ — T1 @ READ COMMITTED
[   14.9 ms]  T1 | BEGIN (READ COMMITTED)
[   15.7 ms]  T1 | SELECT balance WHERE id=1 -> 1000
[   23.6 ms]  T2 | BEGIN (READ COMMITTED)
[   24.5 ms]  T2 | UPDATE balance=1500 WHERE id=1
[   26.4 ms]  T2 | COMMIT
[   26.9 ms]  T1 | SELECT balance WHERE id=1 -> 1500 (re-read)
[   27.1 ms]  T1 | COMMIT
[   27.2 ms]  -- | ANOMALY: first=1000 second=1500
```

### Результат (лог) — предотвращение

```
[   36.8 ms]  -- | NON-REPEATABLE READ — T1 @ REPEATABLE READ
[   44.3 ms]  T1 | BEGIN (REPEATABLE READ)
[   45.0 ms]  T1 | SELECT balance WHERE id=1 -> 1000
[   52.0 ms]  T2 | BEGIN (READ COMMITTED)
[   52.9 ms]  T2 | UPDATE balance=1500 WHERE id=1
[   54.5 ms]  T2 | COMMIT
[   55.0 ms]  T1 | SELECT balance WHERE id=1 -> 1000 (re-read)
[   55.1 ms]  T1 | COMMIT
[   55.3 ms]  -- | PREVENTED: both reads = 1000 (snapshot held)
```

### Вывод

При `READ COMMITTED` аномалия **воспроизводится**: второй SELECT вернул 1500 вместо 1000. При `REPEATABLE READ` аномалия **предотвращена**: PostgreSQL держит snapshot на момент начала транзакции.

### Как избежать

Использовать уровень изоляции `REPEATABLE READ` или `SERIALIZABLE`. PostgreSQL реализует их через MVCC (snapshot isolation), поэтому T1 видит данные на момент начала своей транзакции независимо от коммитов других транзакций.

---

## 5. Аномалия 3: Phantom Read

### Описание

T1 дважды выполняет одинаковый запрос с диапазонным условием и получает разный набор строк, потому что T2 вставила новую строку между двумя чтениями.

### Транзакции

| Шаг | T1 | T2 |
|---|---|---|
| 1 | BEGIN (READ COMMITTED) | |
| 2 | SELECT COUNT(*) WHERE category='books' → 3 | |
| 3 | | BEGIN, INSERT book 'Phantom Book', COMMIT |
| 4 | SELECT COUNT(*) WHERE category='books' → 4 (фантом!) | |
| 5 | COMMIT | |

### Результат (лог) — воспроизведение аномалии

```
[    6.9 ms]  -- | PHANTOM READ — T1 @ READ COMMITTED
[   13.5 ms]  T1 | BEGIN (READ COMMITTED)
[   14.5 ms]  T1 | SELECT COUNT(*) WHERE category='books' -> 3
[   20.7 ms]  T2 | BEGIN (READ COMMITTED)
[   21.2 ms]  T2 | INSERT INTO products ('books','Phantom Book',99)
[   22.7 ms]  T2 | COMMIT
[   23.0 ms]  T1 | SELECT COUNT(*) WHERE category='books' -> 4 (re-read)
[   23.1 ms]  T1 | COMMIT
[   23.2 ms]  -- | ANOMALY: first=3 second=4 (phantom appeared)
```

### Результат (лог) — предотвращение

```
[   29.6 ms]  -- | PHANTOM READ — T1 @ REPEATABLE READ
[   36.2 ms]  T1 | BEGIN (REPEATABLE READ)
[   37.0 ms]  T1 | SELECT COUNT(*) WHERE category='books' -> 3
[   44.3 ms]  T2 | BEGIN (READ COMMITTED)
[   45.1 ms]  T2 | INSERT INTO products ('books','Phantom Book',99)
[   46.7 ms]  T2 | COMMIT
[   47.2 ms]  T1 | SELECT COUNT(*) WHERE category='books' -> 3 (re-read)
[   47.3 ms]  T1 | COMMIT
[   47.4 ms]  -- | PREVENTED: both counts = 3 (snapshot held)
```

### Вывод

При `READ COMMITTED` фантомное чтение **воспроизводится**: второй COUNT вернул 4 вместо 3. При `REPEATABLE READ` аномалия **предотвращена**: PostgreSQL удерживает snapshot, новые строки невидимы для T1.

### Как избежать

В PostgreSQL достаточно `REPEATABLE READ` (snapshot isolation предотвращает фантомы). В других СУБД (MySQL InnoDB) для предотвращения фантомов требуется `SERIALIZABLE` + next-key locking.

---

## 6. Аномалия 4: Lost Update

### Описание

Две транзакции читают одно и то же значение, вычисляют на его основе новое и записывают результат. Более поздняя запись перезаписывает результат первой — одно из обновлений теряется.

### Транзакции

| Шаг | T1 | T2 |
|---|---|---|
| 1 | BEGIN | |
| 2 | SELECT balance → 1000 | BEGIN |
| 3 | | SELECT balance → 1000 |
| 4 | | UPDATE balance = 1000+50 = 1050, COMMIT |
| 5 | UPDATE balance = 1000+100 = 1100, COMMIT | |
| 6 | **Итог: 1100 вместо 1150** | |

### Результат (лог) — воспроизведение аномалии

```
[    8.7 ms]  -- | LOST UPDATE — plain SELECT (race)
[   15.1 ms]  T1 | BEGIN (READ COMMITTED)
[   15.9 ms]  T1 | SELECT balance FROM accounts WHERE id = 1 -> 1000
[   21.6 ms]  T2 | BEGIN (READ COMMITTED)
[   21.9 ms]  T2 | SELECT balance FROM accounts WHERE id = 1 -> 1000
[   22.2 ms]  T2 | UPDATE balance = 1000 + 50 = 1050
[   23.6 ms]  T1 | UPDATE balance = 1000 + 100 = 1100
[   23.6 ms]  T2 | COMMIT
[   24.1 ms]  T1 | COMMIT
[   30.9 ms]  -- | ANOMALY: final balance = 1100, expected 1150 (an update was lost)
```

### Результат (лог) — предотвращение

```
[   40.6 ms]  -- | LOST UPDATE — with SELECT ... FOR UPDATE
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

### Вывод

Без блокировки аномалия **воспроизводится**: T2 обновление потеряно, финальный баланс 1100 вместо 1150. С `SELECT ... FOR UPDATE` аномалия **предотвращена**: T1 удерживает пессимистическую блокировку строки, T2 ждёт её освобождения и читает уже обновлённое значение 1100.

### Как избежать

Три подхода:
1. **SELECT ... FOR UPDATE** — пессимистическая блокировка: явно блокирует строку на время транзакции
2. **Optimistic locking** — версионирование: добавить колонку `version`, при UPDATE проверять `WHERE version = old_version`
3. **Уровень SERIALIZABLE** — PostgreSQL обнаруживает конфликт и откатывает одну из транзакций автоматически

---

## 7. Итоговая таблица аномалий

| Аномалия | Воспроизведена | Уровень изоляции | Предотвращение |
|---|---|---|---|
| Dirty Read | Нет (PostgreSQL предотвращает всегда) | READ UNCOMMITTED = READ COMMITTED | READ COMMITTED+ |
| Non-Repeatable Read | Да (READ COMMITTED) | REPEATABLE READ | REPEATABLE READ+ |
| Phantom Read | Да (READ COMMITTED) | REPEATABLE READ | REPEATABLE READ+ |
| Lost Update | Да (READ COMMITTED без блокировок) | READ COMMITTED | SELECT FOR UPDATE / SERIALIZABLE |

---

## 8. Общий вывод

Все четыре аномалии были воспроизведены (или продемонстрировано их предотвращение СУБД) с помощью параллельных транзакций на реальной PostgreSQL. Практика показала:

- PostgreSQL защищает от Dirty Read на уровне движка — явно задать READ UNCOMMITTED невозможно
- `REPEATABLE READ` в PostgreSQL (реализованный через MVCC) эффективно предотвращает как Non-Repeatable Read, так и Phantom Read
- Lost Update требует явного применения блокировок (`FOR UPDATE`) или оптимистической стратегии — уровень изоляции сам по себе не защищает при read-modify-write паттерне
