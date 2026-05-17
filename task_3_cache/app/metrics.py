import threading
import time
from dataclasses import dataclass, field


@dataclass
class Metrics:
    cache_hits: int = 0
    cache_misses: int = 0
    db_reads: int = 0
    db_writes: int = 0
    writeback_flushes: int = 0
    writeback_flushed_keys: int = 0
    request_count: int = 0
    latency_sum_ms: float = 0.0
    latency_max_ms: float = 0.0
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def hit(self):
        with self._lock:
            self.cache_hits += 1

    def miss(self):
        with self._lock:
            self.cache_misses += 1

    def db_read(self, n: int = 1):
        with self._lock:
            self.db_reads += n

    def db_write(self, n: int = 1):
        with self._lock:
            self.db_writes += n

    def writeback_batch(self, keys_count: int):
        with self._lock:
            self.writeback_flushes += 1
            self.writeback_flushed_keys += keys_count

    def request(self, latency_ms: float):
        with self._lock:
            self.request_count += 1
            self.latency_sum_ms += latency_ms
            if latency_ms > self.latency_max_ms:
                self.latency_max_ms = latency_ms

    def reset(self):
        with self._lock:
            self.cache_hits = 0
            self.cache_misses = 0
            self.db_reads = 0
            self.db_writes = 0
            self.writeback_flushes = 0
            self.writeback_flushed_keys = 0
            self.request_count = 0
            self.latency_sum_ms = 0.0
            self.latency_max_ms = 0.0
            self.started_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = max(time.time() - self.started_at, 1e-6)
            total_reads = self.cache_hits + self.cache_misses
            hit_rate = self.cache_hits / total_reads if total_reads else 0.0
            avg_latency = (
                self.latency_sum_ms / self.request_count if self.request_count else 0.0
            )
            return {
                "elapsed_sec": elapsed,
                "request_count": self.request_count,
                "throughput_rps": self.request_count / elapsed,
                "avg_latency_ms": avg_latency,
                "max_latency_ms": self.latency_max_ms,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "cache_hit_rate": hit_rate,
                "db_reads": self.db_reads,
                "db_writes": self.db_writes,
                "db_total": self.db_reads + self.db_writes,
                "writeback_flushes": self.writeback_flushes,
                "writeback_flushed_keys": self.writeback_flushed_keys,
            }


metrics = Metrics()
