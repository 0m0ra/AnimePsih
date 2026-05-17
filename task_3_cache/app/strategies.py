"""Three caching strategies: cache_aside, write_through, write_back."""

import threading
import time
from typing import Optional

import redis
from sqlalchemy import select

from app.config import Config
from app.db import Item, get_session
from app.metrics import metrics

ITEM_KEY = "item:{id}"
DIRTY_SET_KEY = "writeback:dirty"


def _key(item_id: int) -> str:
    return ITEM_KEY.format(id=item_id)


class BaseStrategy:
    name = "base"

    def __init__(self, r: redis.Redis):
        self.r = r

    def get(self, item_id: int) -> Optional[str]:
        raise NotImplementedError

    def set(self, item_id: int, value: str) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        pass


class CacheAsideStrategy(BaseStrategy):
    """Lazy Loading. Read: cache → DB → cache. Write: DB only (write-around)."""

    name = "cache_aside"

    def get(self, item_id: int) -> Optional[str]:
        cached = self.r.get(_key(item_id))
        if cached is not None:
            metrics.hit()
            return cached.decode() if isinstance(cached, bytes) else cached
        metrics.miss()
        with get_session() as s:
            row = s.execute(select(Item).where(Item.id == item_id)).scalar_one_or_none()
            metrics.db_read()
            if row is None:
                return None
            self.r.setex(_key(item_id), Config.CACHE_TTL_SECONDS, row.value)
            return row.value

    def set(self, item_id: int, value: str) -> None:
        with get_session() as s:
            existing = s.get(Item, item_id)
            if existing is None:
                s.add(Item(id=item_id, value=value))
            else:
                existing.value = value
            s.commit()
            metrics.db_write()
        # write-around: invalidate cached entry so the next read repopulates.
        self.r.delete(_key(item_id))


class WriteThroughStrategy(BaseStrategy):
    """Read: cache → DB → cache. Write: DB + cache atomically (caller-perceived)."""

    name = "write_through"

    def get(self, item_id: int) -> Optional[str]:
        cached = self.r.get(_key(item_id))
        if cached is not None:
            metrics.hit()
            return cached.decode() if isinstance(cached, bytes) else cached
        metrics.miss()
        with get_session() as s:
            row = s.execute(select(Item).where(Item.id == item_id)).scalar_one_or_none()
            metrics.db_read()
            if row is None:
                return None
            self.r.setex(_key(item_id), Config.CACHE_TTL_SECONDS, row.value)
            return row.value

    def set(self, item_id: int, value: str) -> None:
        with get_session() as s:
            existing = s.get(Item, item_id)
            if existing is None:
                s.add(Item(id=item_id, value=value))
            else:
                existing.value = value
            s.commit()
            metrics.db_write()
        self.r.setex(_key(item_id), Config.CACHE_TTL_SECONDS, value)


class WriteBackStrategy(BaseStrategy):
    """Read: cache → DB → cache. Write: cache only; flush to DB asynchronously."""

    name = "write_back"

    def __init__(self, r: redis.Redis):
        super().__init__(r)
        self._stop = threading.Event()
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()

    def get(self, item_id: int) -> Optional[str]:
        cached = self.r.get(_key(item_id))
        if cached is not None:
            metrics.hit()
            return cached.decode() if isinstance(cached, bytes) else cached
        metrics.miss()
        with get_session() as s:
            row = s.execute(select(Item).where(Item.id == item_id)).scalar_one_or_none()
            metrics.db_read()
            if row is None:
                return None
            self.r.setex(_key(item_id), Config.CACHE_TTL_SECONDS, row.value)
            return row.value

    def set(self, item_id: int, value: str) -> None:
        pipe = self.r.pipeline()
        pipe.setex(_key(item_id), Config.CACHE_TTL_SECONDS, value)
        pipe.sadd(DIRTY_SET_KEY, str(item_id))
        pipe.execute()

    def _flush_loop(self):
        while not self._stop.is_set():
            try:
                self.flush_once()
            except Exception as e:  # noqa: BLE001
                print(f"[writeback] flush error: {e}")
            self._stop.wait(Config.WRITEBACK_FLUSH_INTERVAL)

    def flush_once(self) -> int:
        """Drain up to WRITEBACK_BATCH_SIZE dirty keys and persist them to the DB."""
        ids: list[str] = []
        for _ in range(Config.WRITEBACK_BATCH_SIZE):
            popped = self.r.spop(DIRTY_SET_KEY)
            if popped is None:
                break
            ids.append(popped.decode() if isinstance(popped, bytes) else popped)

        if not ids:
            return 0

        # Read current cached values for those ids in one round trip.
        keys = [_key(int(i)) for i in ids]
        values = self.r.mget(keys)

        pairs = []
        for sid, val in zip(ids, values):
            if val is None:
                # value was evicted before we could flush — re-mark dirty so we keep trying.
                self.r.sadd(DIRTY_SET_KEY, sid)
                continue
            pairs.append((int(sid), val.decode() if isinstance(val, bytes) else val))

        if not pairs:
            return 0

        with get_session() as s:
            existing_ids = {
                row.id
                for row in s.execute(
                    select(Item).where(Item.id.in_([p[0] for p in pairs]))
                ).scalars()
            }
            for item_id, val in pairs:
                if item_id in existing_ids:
                    s.query(Item).filter(Item.id == item_id).update({Item.value: val})
                else:
                    s.add(Item(id=item_id, value=val))
            s.commit()
            metrics.db_write(len(pairs))

        metrics.writeback_batch(len(pairs))
        return len(pairs)

    def shutdown(self):
        self._stop.set()
        # final drain so no buffered writes are lost
        try:
            while self.flush_once() > 0:
                pass
        except Exception as e:  # noqa: BLE001
            print(f"[writeback] final drain error: {e}")
        self._flusher.join(timeout=5)


def build_strategy(name: str, r: redis.Redis) -> BaseStrategy:
    if name == "cache_aside":
        return CacheAsideStrategy(r)
    if name == "write_through":
        return WriteThroughStrategy(r)
    if name == "write_back":
        return WriteBackStrategy(r)
    raise ValueError(f"unknown strategy: {name}")
