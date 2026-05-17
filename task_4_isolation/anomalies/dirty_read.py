"""Dirty Read demo.

Scenario: T1 updates Alice's balance but does NOT commit. T2 reads.
Anomaly (would happen at READ UNCOMMITTED in MySQL): T2 sees uncommitted value.
PostgreSQL behaviour: READ UNCOMMITTED is silently treated as READ COMMITTED,
so the anomaly is prevented even when explicitly requested.
"""
from __future__ import annotations

import threading

import psycopg
from psycopg import IsolationLevel

from ._common import DSN, Logger, banner, conn, reset_accounts


def run() -> str:
    reset_accounts()
    log = Logger()
    log.log("--", banner("DIRTY READ — PostgreSQL @ READ UNCOMMITTED"))

    t1_updated = threading.Event()
    t2_read = threading.Event()
    seen_value: dict[str, int] = {}

    def t1():
        with conn(IsolationLevel.READ_UNCOMMITTED) as c:
            with c.cursor() as cur:
                log.log("T1", "BEGIN (READ UNCOMMITTED)")
                cur.execute(
                    "UPDATE accounts SET balance = 9999 WHERE id = 1 RETURNING balance"
                )
                new_val = cur.fetchone()[0]
                log.log("T1", f"UPDATE balance=9999 WHERE id=1 -> {new_val} (NOT committed)")
            t1_updated.set()
            t2_read.wait(timeout=5)
            log.log("T1", "ROLLBACK (discarding update)")
            c.rollback()

    def t2():
        t1_updated.wait(timeout=5)
        with conn(IsolationLevel.READ_UNCOMMITTED) as c:
            with c.cursor() as cur:
                log.log("T2", "BEGIN (READ UNCOMMITTED)")
                cur.execute("SELECT balance FROM accounts WHERE id = 1")
                seen = cur.fetchone()[0]
                log.log("T2", f"SELECT balance WHERE id=1 -> {seen}")
            c.commit()
            seen_value["v"] = seen
            t2_read.set()
            verdict = "ANOMALY (dirty read)" if seen == 9999 else "PREVENTED (no dirty read)"
            log.log("T2", f"verdict: {verdict}")

    th1 = threading.Thread(target=t1)
    th2 = threading.Thread(target=t2)
    th1.start(); th2.start()
    th1.join();  th2.join()

    with psycopg.connect(DSN, autocommit=True) as c:
        bal = c.execute("SELECT balance FROM accounts WHERE id=1").fetchone()[0]
    log.log("--", f"final committed balance = {bal} (T1 rolled back -> 1000 expected)")
    return log.dump()


if __name__ == "__main__":
    run()