"""Non-repeatable Read demo.

Scenario:
  T1 reads balance, T2 updates+commits, T1 reads again -> different value.

Demonstrated at READ COMMITTED (anomaly happens) and at REPEATABLE READ
(anomaly is prevented — T1 sees a snapshot from its first statement).
"""
from __future__ import annotations

import threading

from psycopg import IsolationLevel

from ._common import Logger, banner, conn, reset_accounts


def _scenario(level: IsolationLevel, label: str, log: Logger) -> tuple[int, int]:
    reset_accounts()
    log.log("--", banner(f"NON-REPEATABLE READ — T1 @ {label}"))

    t1_first_read = threading.Event()
    t2_committed = threading.Event()
    seen: dict[str, int] = {}

    def t1():
        with conn(level) as c:
            with c.cursor() as cur:
                log.log("T1", f"BEGIN ({label})")
                cur.execute("SELECT balance FROM accounts WHERE id = 1")
                v1 = cur.fetchone()[0]
                seen["first"] = v1
                log.log("T1", f"SELECT balance WHERE id=1 -> {v1}")
            t1_first_read.set()
            t2_committed.wait(timeout=5)
            with c.cursor() as cur:
                cur.execute("SELECT balance FROM accounts WHERE id = 1")
                v2 = cur.fetchone()[0]
                seen["second"] = v2
                log.log("T1", f"SELECT balance WHERE id=1 -> {v2} (re-read)")
            c.commit()
            log.log("T1", "COMMIT")

    def t2():
        t1_first_read.wait(timeout=5)
        with conn(IsolationLevel.READ_COMMITTED) as c:
            with c.cursor() as cur:
                log.log("T2", "BEGIN (READ COMMITTED)")
                cur.execute("UPDATE accounts SET balance = 1500 WHERE id = 1")
                log.log("T2", "UPDATE balance=1500 WHERE id=1")
            c.commit()
            log.log("T2", "COMMIT")
        t2_committed.set()

    th1 = threading.Thread(target=t1)
    th2 = threading.Thread(target=t2)
    th1.start(); th2.start()
    th1.join();  th2.join()

    if seen["first"] != seen["second"]:
        log.log("--", f"ANOMALY: first={seen['first']} second={seen['second']}")
    else:
        log.log("--", f"PREVENTED: both reads = {seen['first']} (snapshot held)")
    return seen["first"], seen["second"]


def run() -> str:
    log = Logger()
    _scenario(IsolationLevel.READ_COMMITTED, "READ COMMITTED", log)
    _scenario(IsolationLevel.REPEATABLE_READ, "REPEATABLE READ", log)
    return log.dump()


if __name__ == "__main__":
    run()