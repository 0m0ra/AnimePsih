import time
from contextlib import asynccontextmanager
from typing import Optional

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.config import VALID_STRATEGIES, Config
from app.db import init_engine, truncate_items
from app.metrics import metrics
from app.strategies import DIRTY_SET_KEY, ITEM_KEY, build_strategy

state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if Config.CACHE_STRATEGY not in VALID_STRATEGIES:
        raise RuntimeError(
            f"invalid CACHE_STRATEGY={Config.CACHE_STRATEGY}; expected one of {VALID_STRATEGIES}"
        )
    init_engine(Config.DATABASE_URL)
    r = redis.Redis.from_url(Config.REDIS_URL)
    r.ping()
    state["redis"] = r
    state["strategy"] = build_strategy(Config.CACHE_STRATEGY, r)
    print(f"[app] strategy={Config.CACHE_STRATEGY} ready")
    try:
        yield
    finally:
        try:
            state["strategy"].shutdown()
        except Exception as e:  # noqa: BLE001
            print(f"[app] shutdown error: {e}")


app = FastAPI(title="cache-strategies-demo", lifespan=lifespan)


class ItemPayload(BaseModel):
    value: str


@app.get("/healthz")
def healthz():
    return {"ok": True, "strategy": Config.CACHE_STRATEGY}


@app.get("/items/{item_id}")
def get_item(item_id: int):
    started = time.perf_counter()
    try:
        value = state["strategy"].get(item_id)
        if value is None:
            raise HTTPException(status_code=404, detail="not found")
        return {"id": item_id, "value": value}
    finally:
        metrics.request((time.perf_counter() - started) * 1000)


@app.put("/items/{item_id}")
def put_item(item_id: int, payload: ItemPayload):
    started = time.perf_counter()
    try:
        state["strategy"].set(item_id, payload.value)
        return {"id": item_id, "value": payload.value}
    finally:
        metrics.request((time.perf_counter() - started) * 1000)


@app.get("/metrics")
def get_metrics():
    snap = metrics.snapshot()
    snap["strategy"] = Config.CACHE_STRATEGY
    return snap


class ResetOptions(BaseModel):
    flush_redis: bool = True
    truncate_db: bool = True


@app.post("/admin/reset")
def reset(opts: Optional[ResetOptions] = None):
    """Reset the run between benchmarks: clear redis, truncate DB, zero metrics."""
    opts = opts or ResetOptions()
    r: redis.Redis = state["redis"]
    if opts.flush_redis:
        r.flushdb()
    if opts.truncate_db:
        truncate_items()
    metrics.reset()
    return {"ok": True}


class SeedPayload(BaseModel):
    count: int = 1000
    value_prefix: str = "v"
    warm_cache: bool = True


@app.post("/admin/seed")
def seed(payload: SeedPayload):
    """Populate the DB with `count` items so reads can hit existing rows."""
    from sqlalchemy.dialects.postgresql import insert
    from app.db import Item, get_session

    rows = [{"id": i, "value": f"{payload.value_prefix}{i}"} for i in range(1, payload.count + 1)]
    with get_session() as s:
        stmt = insert(Item).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Item.id], set_={"value": stmt.excluded.value}
        )
        s.execute(stmt)
        s.commit()

    r: redis.Redis = state["redis"]
    if payload.warm_cache:
        pipe = r.pipeline()
        for row in rows:
            pipe.setex(ITEM_KEY.format(id=row["id"]), Config.CACHE_TTL_SECONDS, row["value"])
        pipe.execute()
    return {"ok": True, "seeded": len(rows), "warmed": payload.warm_cache}


@app.post("/admin/flush_writeback")
def flush_writeback():
    """For write-back: force a synchronous drain so the report can show the final DB state."""
    from app.strategies import WriteBackStrategy

    strat = state["strategy"]
    if not isinstance(strat, WriteBackStrategy):
        return {"ok": True, "flushed": 0, "note": "strategy is not write_back"}
    flushed = 0
    while True:
        n = strat.flush_once()
        flushed += n
        if n == 0:
            break
    return {"ok": True, "flushed": flushed}


@app.get("/admin/writeback_pending")
def writeback_pending():
    r: redis.Redis = state["redis"]
    return {"pending_dirty_keys": r.scard(DIRTY_SET_KEY)}
