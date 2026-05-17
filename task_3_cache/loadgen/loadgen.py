"""HTTP load generator with configurable read/write ratio and concurrency.

Usage:
    python -m loadgen.loadgen --url http://localhost:8000 \
        --duration 20 --workers 16 --read-ratio 0.8 --keys 1000

Outputs JSON with both client-side and server-side metrics so the report can
compare strategies on identical inputs.
"""

import argparse
import json
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import httpx


@dataclass
class ClientStats:
    requests: int = 0
    reads: int = 0
    writes: int = 0
    errors: int = 0
    not_found: int = 0
    latencies_ms: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, kind: str, latency_ms: float, status: int):
        with self._lock:
            self.requests += 1
            self.latencies_ms.append(latency_ms)
            if kind == "read":
                self.reads += 1
            else:
                self.writes += 1
            if status == 404:
                self.not_found += 1
            elif status >= 500 or status == 0:
                self.errors += 1


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round((p / 100.0) * (len(s) - 1)))
    return s[k]


def run_load(
    url: str,
    duration: float,
    workers: int,
    read_ratio: float,
    keys: int,
    seed: int = 42,
) -> ClientStats:
    stats = ClientStats()
    deadline = time.time() + duration

    def worker(worker_id: int):
        rng = random.Random(seed + worker_id)
        with httpx.Client(base_url=url, timeout=10.0) as client:
            while time.time() < deadline:
                key = rng.randint(1, keys)
                is_read = rng.random() < read_ratio
                started = time.perf_counter()
                status = 0
                try:
                    if is_read:
                        resp = client.get(f"/items/{key}")
                    else:
                        value = f"v{key}-{rng.randint(0, 10**6)}"
                        resp = client.put(f"/items/{key}", json={"value": value})
                    status = resp.status_code
                except Exception:
                    status = 0
                latency_ms = (time.perf_counter() - started) * 1000
                stats.record("read" if is_read else "write", latency_ms, status)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, i) for i in range(workers)]
        for f in as_completed(futures):
            f.result()

    return stats


def fetch_server_metrics(url: str) -> dict:
    with httpx.Client(base_url=url, timeout=10.0) as client:
        return client.get("/metrics").json()


def reset_server(url: str):
    with httpx.Client(base_url=url, timeout=30.0) as client:
        r = client.post("/admin/reset", json={"flush_redis": True, "truncate_db": True})
        r.raise_for_status()


def seed_server(url: str, count: int, warm_cache: bool):
    with httpx.Client(base_url=url, timeout=60.0) as client:
        r = client.post(
            "/admin/seed",
            json={"count": count, "value_prefix": "v", "warm_cache": warm_cache},
        )
        r.raise_for_status()


def flush_writeback(url: str) -> int:
    with httpx.Client(base_url=url, timeout=120.0) as client:
        r = client.post("/admin/flush_writeback")
        r.raise_for_status()
        return r.json().get("flushed", 0)


def writeback_pending(url: str) -> int:
    with httpx.Client(base_url=url, timeout=10.0) as client:
        r = client.get("/admin/writeback_pending")
        r.raise_for_status()
        return r.json()["pending_dirty_keys"]


def summary(stats: ClientStats, duration: float) -> dict:
    if stats.latencies_ms:
        avg = statistics.fmean(stats.latencies_ms)
        p50 = percentile(stats.latencies_ms, 50)
        p95 = percentile(stats.latencies_ms, 95)
        p99 = percentile(stats.latencies_ms, 99)
        mx = max(stats.latencies_ms)
    else:
        avg = p50 = p95 = p99 = mx = 0.0
    return {
        "client_requests": stats.requests,
        "client_reads": stats.reads,
        "client_writes": stats.writes,
        "client_errors": stats.errors,
        "client_not_found": stats.not_found,
        "client_throughput_rps": stats.requests / duration if duration else 0.0,
        "client_avg_latency_ms": avg,
        "client_p50_latency_ms": p50,
        "client_p95_latency_ms": p95,
        "client_p99_latency_ms": p99,
        "client_max_latency_ms": mx,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--read-ratio", type=float, default=0.8)
    p.add_argument("--keys", type=int, default=1000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--no-reset", action="store_true")
    p.add_argument("--no-seed", action="store_true")
    p.add_argument("--no-warm", action="store_true")
    p.add_argument("--out", default=None)
    p.add_argument("--label", default="run")
    args = p.parse_args()

    if not args.no_reset:
        print(f"[loadgen] resetting server at {args.url}")
        reset_server(args.url)
    if not args.no_seed:
        print(f"[loadgen] seeding {args.keys} items, warm_cache={not args.no_warm}")
        seed_server(args.url, args.keys, warm_cache=not args.no_warm)
    # zero metrics after seeding so warm-up traffic doesn't pollute the run
    with httpx.Client(base_url=args.url, timeout=10.0) as client:
        client.post("/admin/reset", json={"flush_redis": False, "truncate_db": False})

    print(
        f"[loadgen] running label={args.label} duration={args.duration}s "
        f"workers={args.workers} read_ratio={args.read_ratio} keys={args.keys}"
    )
    started = time.perf_counter()
    stats = run_load(
        args.url, args.duration, args.workers, args.read_ratio, args.keys, args.seed
    )
    elapsed = time.perf_counter() - started

    server_pending_before_flush = writeback_pending(args.url)
    server = fetch_server_metrics(args.url)
    flushed = flush_writeback(args.url)
    server_after = fetch_server_metrics(args.url)

    result = {
        "label": args.label,
        "config": {
            "url": args.url,
            "duration_sec_target": args.duration,
            "workers": args.workers,
            "read_ratio": args.read_ratio,
            "keys": args.keys,
            "seed": args.seed,
        },
        "client": summary(stats, elapsed),
        "server_during": server,
        "server_after_flush": server_after,
        "writeback_pending_before_flush": server_pending_before_flush,
        "writeback_flushed_post_run": flushed,
        "wall_clock_sec": elapsed,
    }
    print(json.dumps(result, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[loadgen] wrote {args.out}")


if __name__ == "__main__":
    main()
