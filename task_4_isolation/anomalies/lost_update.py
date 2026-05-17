"""Lost Update demo.

Scenario (classic read-modify-write race):
  T1 reads balance=1000, T2 reads balance=1000, both compute new value
  on the application side, both UPDATE. The later commit overwrites the
  earlier one — the first deposit is lost.

Demonstrated:
  1. READ COMMITTED + plain SELECT then UPDATE -> ANOMALY (update lost).
  2. READ COMMITTED + SELECT ... FOR UPDATE     -> PREVENTED (T2 blocks
     until T1 commits, then re-reads the new value).
"""
from __future__ import annotations

import threading

import psycopg
from psycopg import IsolationLevel

from ._common import DSN, Logger, banner, conn, reset_accounts


def _scenario(use_for_update: bool, log: Logger) -> int:
    reset_accounts()
    label = "with SELECT ... FOR UPDATE" if use_for_update else "plain SELECT (race)"
    log.log("--", banner(f"LOST UPDATE — {label}"))

    t1_read_done = threading.Event()
    t2_read_done = threading.Event()

    def worker(name: str, deposit: int, peer_done: threading.Event,
               my_done: threading.Event):
        with conn(IsolationLevel.READ_COMMITTED) as c:
            with c.cursor() as cur:
                log.log(name, "BEGIN (READ COMMITTED)")
                sql = "SELECT balance FROM accounts WHERE id = 1"
                if use_for_update:
                    sql += " FOR UPDATE"
                cur.execute(sql)
                bal = cur.fetchone()[0]
                log.log(name, f"{sql} -> {bal}")
                # Make sure both transactions read before any of them writes
                # so the race is real (only meaningful for plain SELECT).
                my_done.set()
                if not use_for_update:
                    peer_done.wait(timeout=5)
                new_bal = bal + deposit
                cur.execute("UPDATE accounts SET balance = %s WHERE id = 1", (new_bal,))
                log.log(name, f"UPDATE balance = {bal} + {deposit} = {new_bal}")
            c.commit()
            log.log(name, "COMMIT")

    th1 = threading.Thread(
        target=worker, args=("T1", 100, t2_read_done, t1_read_done)
    )
    th2 = threading.Thread(
        target=worker, args=("T2",  50, t1_read_done, t2_read_done)
    )
    th1.start()
    # Stagger so T1 reads first; T2 reads a tick later.
    t1_read_done.wait(timeout=5)
    th2.start()
    th1.join()
    th2.join()

    with psycopg.connect(DSN, autocommit=True) as c:
        final = c.execute("SELECT balance FROM accounts WHERE id=1").fetchone()[0]

    expected = 1000 + 100 + 50  # both deposits should land
    if final == expected:
        log.log("--", f"PREVENTED: final balance = {final} (= 1000 + 100 + 50)")
    else:
        log.log("--", f"ANOMALY:   final balance = {final}, expected {expected} (an update was lost)")
    return final


def run() -> str:
    log = Logger()
    _scenario(use_for_update=False, log=log)
    _scenario(use_for_update=True,  log=log)
    return log.dump()


if __name__ == "__main__":
    run()