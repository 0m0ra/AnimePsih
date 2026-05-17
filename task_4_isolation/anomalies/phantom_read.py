"""Phantom Read demo.

Scenario:
  T1 counts books. T2 inserts a new book and commits. T1 counts again.
  At READ COMMITTED — the count differs (phantom row appears).
  At REPEATABLE READ — Postgres uses snapshot isolation, so phantoms
  are also prevented (this is stricter than the SQL standard).
"""
from __future__ import annotations

import threading

from psycopg import IsolationLevel

from ._common import Logger, banner, conn, reset_products


def _scenario(level: IsolationLevel, label: str, log: Logger) -> tuple[int, int]:
    reset_products()
    log.log("--", banner(f"PHANTOM READ — T1 @ {label}"))

    t1_first_read = threading.Event()
    t2_committed = threading.Event()
    seen: dict[str, int] = {}

    def t1():
        with conn(level) as c:
            with c.cursor() as cur:
                log.log("T1", f"BEGIN ({label})")
                cur.execute("SELECT COUNT(*) FROM products WHERE category = 'books'")
                c1 = cur.fetchone()[0]
                seen["first"] = c1
                log.log("T1", f"SELECT COUNT(*) WHERE category='books' -> {c1}")
            t1_first_read.set()
            t2_committed.wait(timeout=5)
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM products WHERE category = 'books'")
                c2 = cur.fetchone()[0]
                seen["second"] = c2
                log.log("T1", f"SELECT COUNT(*) WHERE category='books' -> {c2} (re-read)")
            c.commit()
            log.log("T1", "COMMIT")

    def t2():
        t1_first_read.wait(timeout=5)
        with conn(IsolationLevel.READ_COMMITTED) as c:
            with c.cursor() as cur:
                log.log("T2", "BEGIN (READ COMMITTED)")
                cur.execute(
                    "INSERT INTO products (category, name, price) VALUES ('books', 'Phantom Book', 99)"
                )
                log.log("T2", "INSERT INTO products ('books','Phantom Book',99)")
            c.commit()
            log.log("T2", "COMMIT")
        t2_committed.set()

    th1 = threading.Thread(target=t1)
    th2 = threading.Thread(target=t2)
    th1.start(); th2.start()
    th1.join();  th2.join()

    if seen["first"] != seen["second"]:
        log.log("--", f"ANOMALY: first={seen['first']} second={seen['second']} (phantom appeared)")
    else:
        log.log("--", f"PREVENTED: both counts = {seen['first']} (snapshot held)")
    return seen["first"], seen["second"]


def run() -> str:
    log = Logger()
    _scenario(IsolationLevel.READ_COMMITTED, "READ COMMITTED", log)
    _scenario(IsolationLevel.REPEATABLE_READ, "REPEATABLE READ", log)
    return log.dump()


if __name__ == "__main__":
    run()