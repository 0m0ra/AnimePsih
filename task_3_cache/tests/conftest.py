"""Pytest fixtures: spin up real Redis + Postgres connections via env vars,
and reset state between tests so each strategy can be exercised in isolation.
"""

import os
import time

import pytest
import redis as _redis
from sqlalchemy import text

from app import config as app_config
from app.config import Config
from app.db import Item, get_session, init_engine
from app.metrics import metrics
from app.strategies import build_strategy

DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg2://app:app@localhost:5433/appdb")
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6380/1")  # test on a dedicated db


@pytest.fixture(scope="session", autouse=True)
def _engine():
    Config.DATABASE_URL = DB_URL
    Config.REDIS_URL = REDIS_URL
    Config.WRITEBACK_FLUSH_INTERVAL = 0.2
    Config.WRITEBACK_BATCH_SIZE = 50
    init_engine(DB_URL)
    yield


@pytest.fixture()
def redis_client():
    r = _redis.Redis.from_url(REDIS_URL)
    r.flushdb()
    yield r
    r.flushdb()


@pytest.fixture(autouse=True)
def _clean_db():
    with get_session() as s:
        s.execute(text("TRUNCATE items"))
        s.commit()
    metrics.reset()
    yield


def _wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def cache_aside(redis_client):
    s = build_strategy("cache_aside", redis_client)
    yield s
    s.shutdown()


@pytest.fixture()
def write_through(redis_client):
    s = build_strategy("write_through", redis_client)
    yield s
    s.shutdown()


@pytest.fixture()
def write_back(redis_client):
    s = build_strategy("write_back", redis_client)
    yield s
    s.shutdown()


def db_value(item_id: int):
    with get_session() as s:
        row = s.get(Item, item_id)
        return row.value if row else None


# expose helpers
__all__ = ["db_value", "_wait_until"]
