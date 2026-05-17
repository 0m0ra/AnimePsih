"""Shared helpers for isolation anomaly demos."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

import psycopg
from psycopg import IsolationLevel

DSN = "host=localhost port=5432 user=postgres password=postgres dbname=isolation_demo"


@dataclass
class Logger:
    """Thread-safe ordered logger that prints + buffers log lines."""

    lines: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _t0: float = field(default_factory=time.monotonic)

    def log(self, who: str, msg: str) -> None:
        ts = (time.monotonic() - self._t0) * 1000
        line = f"[{ts:7.1f} ms] {who:>3} | {msg}"
        with self._lock:
            self.lines.append(line)
            print(line, flush=True)

    def dump(self) -> str:
        return "\n".join(self.lines)


@contextmanager
def conn(isolation: IsolationLevel | None = None) -> Iterator[psycopg.Connection]:
    """Open a fresh connection with autocommit OFF (so BEGIN starts a tx)."""
    c = psycopg.connect(DSN, autocommit=False)
    if isolation is not None:
        c.isolation_level = isolation
    try:
        yield c
    finally:
        c.close()


def reset_accounts() -> None:
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("UPDATE accounts SET balance = 1000 WHERE id = 1")
        c.execute("UPDATE accounts SET balance = 500  WHERE id = 2")
        c.execute("UPDATE accounts SET balance = 200  WHERE id = 3")


def reset_products() -> None:
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DELETE FROM products WHERE id > 5")


def banner(title: str) -> str:
    bar = "=" * 70
    return f"\n{bar}\n{title}\n{bar}"