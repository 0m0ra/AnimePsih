"""Behavioral tests for each cache strategy.

Run requirements: a Postgres on localhost:5433 and a Redis on localhost:6380.
The docker-compose in the repo provides both. The tests use Redis logical DB 1
to stay out of the application's data.
"""

import time

from app.db import Item, get_session
from app.metrics import metrics
from app.strategies import DIRTY_SET_KEY, ITEM_KEY
from tests.conftest import _wait_until, db_value


# ---------- cache_aside ----------

def test_cache_aside_miss_then_hit(cache_aside, redis_client):
    with get_session() as s:
        s.add(Item(id=1, value="hello"))
        s.commit()

    assert cache_aside.get(1) == "hello"
    assert cache_aside.get(1) == "hello"

    snap = metrics.snapshot()
    assert snap["cache_misses"] == 1
    assert snap["cache_hits"] == 1
    assert snap["db_reads"] == 1
    assert redis_client.get(ITEM_KEY.format(id=1)) is not None


def test_cache_aside_write_around_invalidates(cache_aside, redis_client):
    with get_session() as s:
        s.add(Item(id=2, value="old"))
        s.commit()
    cache_aside.get(2)
    assert redis_client.get(ITEM_KEY.format(id=2)) is not None

    cache_aside.set(2, "new")
    # write-around: cache entry must be invalidated, not updated
    assert redis_client.get(ITEM_KEY.format(id=2)) is None
    assert db_value(2) == "new"

    # next read repopulates from DB
    assert cache_aside.get(2) == "new"


# ---------- write_through ----------

def test_write_through_writes_to_both(write_through, redis_client):
    write_through.set(10, "v1")
    assert db_value(10) == "v1"
    cached = redis_client.get(ITEM_KEY.format(id=10))
    assert cached == b"v1"

    # subsequent read is a hit, no extra DB reads
    metrics.reset()
    assert write_through.get(10) == "v1"
    snap = metrics.snapshot()
    assert snap["cache_hits"] == 1
    assert snap["db_reads"] == 0


def test_write_through_update_overwrites_cache(write_through, redis_client):
    write_through.set(11, "a")
    write_through.set(11, "b")
    assert db_value(11) == "b"
    assert redis_client.get(ITEM_KEY.format(id=11)) == b"b"


# ---------- write_back ----------

def test_write_back_writes_only_cache_initially(write_back, redis_client):
    metrics.reset()
    write_back.set(20, "vb")
    # cache populated immediately
    assert redis_client.get(ITEM_KEY.format(id=20)) == b"vb"
    # DB write may not have happened yet
    snap_immediate = metrics.snapshot()
    # the dirty set should reflect the buffered write
    assert b"20" in {x for x in redis_client.smembers(DIRTY_SET_KEY)}
    assert snap_immediate["db_writes"] == 0  # no DB write yet at the moment of set()


def test_write_back_eventually_persists(write_back, redis_client):
    write_back.set(21, "later")
    assert _wait_until(lambda: db_value(21) == "later", timeout=5.0)
    assert redis_client.scard(DIRTY_SET_KEY) == 0


def test_write_back_batches_many_writes(write_back, redis_client):
    metrics.reset()
    for i in range(100, 200):
        write_back.set(i, f"x{i}")
    # immediate snapshot: many cache writes, 0 DB writes
    snap_immediate = metrics.snapshot()
    assert snap_immediate["db_writes"] <= 50  # likely 0, allow timing slack

    # wait for the flusher to drain everything
    assert _wait_until(
        lambda: redis_client.scard(DIRTY_SET_KEY) == 0
        and all(db_value(i) == f"x{i}" for i in range(100, 200)),
        timeout=10.0,
    )
    snap_after = metrics.snapshot()
    assert snap_after["db_writes"] >= 100
    # batching: fewer flush operations than total writes
    assert snap_after["writeback_flushes"] < 100


def test_write_back_read_after_write_hits_cache(write_back, redis_client):
    write_back.set(30, "rb")
    metrics.reset()
    assert write_back.get(30) == "rb"
    snap = metrics.snapshot()
    assert snap["cache_hits"] == 1
    assert snap["db_reads"] == 0
