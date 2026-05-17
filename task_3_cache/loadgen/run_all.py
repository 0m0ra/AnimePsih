"""Orchestrate benchmarks across all 3 strategies x 3 workloads.

Assumes docker compose is up. The app container is restarted with a different
CACHE_STRATEGY env var for each strategy.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

from loadgen.loadgen import (
    fetch_server_metrics,
    flush_writeback,
    reset_server,
    run_load,
    seed_server,
    summary,
    writeback_pending,
)

STRATEGIES = ["cache_aside", "write_through", "write_back"]
WORKLOADS = [
    ("read_heavy", 0.8),
    ("balanced", 0.5),
    ("write_heavy", 0.2),
]


def restart_app_with_strategy(strategy: str, compose_file: str):
    env = os.environ.copy()
    env["CACHE_STRATEGY"] = strategy
    print(f"[runner] restarting app with strategy={strategy}")
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "up", "-d", "--no-deps", "--force-recreate", "app"],
        check=True,
        env=env,
    )


def wait_for_health(url: str, timeout: float = 60.0):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2.0) as c:
                resp = c.get(f"{url}/healthz")
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1)
    raise RuntimeError(f"app at {url} did not become healthy: {last_err}")


def run_one(url: str, label: str, duration: float, workers: int, keys: int,
            read_ratio: float, seed: int) -> dict:
    reset_server(url)
    seed_server(url, keys, warm_cache=True)
    with httpx.Client(base_url=url, timeout=10.0) as client:
        client.post("/admin/reset", json={"flush_redis": False, "truncate_db": False})
    started = time.perf_counter()
    stats = run_load(url, duration, workers, read_ratio, keys, seed)
    elapsed = time.perf_counter() - started
    pending = writeback_pending(url)
    server_during = fetch_server_metrics(url)
    flushed = flush_writeback(url)
    server_after = fetch_server_metrics(url)
    return {
        "label": label,
        "config": {
            "duration_sec_target": duration,
            "workers": workers,
            "keys": keys,
            "read_ratio": read_ratio,
        },
        "client": summary(stats, elapsed),
        "server_during": server_during,
        "server_after_flush": server_after,
        "writeback_pending_before_flush": pending,
        "writeback_flushed_post_run": flushed,
        "wall_clock_sec": elapsed,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--compose", default="docker-compose.yml")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--keys", type=int, default=1000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="results")
    args = p.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_results = {}
    for strategy in STRATEGIES:
        restart_app_with_strategy(strategy, args.compose)
        wait_for_health(args.url)
        all_results[strategy] = {}
        for wl_name, ratio in WORKLOADS:
            label = f"{strategy}__{wl_name}"
            print(f"\n=== {label} ===")
            res = run_one(
                args.url, label, args.duration, args.workers, args.keys, ratio, args.seed
            )
            all_results[strategy][wl_name] = res
            with open(out_dir / f"{label}.json", "w") as f:
                json.dump(res, f, indent=2)

    summary_path = out_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n[runner] wrote {summary_path}")

    # also emit a flat csv for the report
    csv_path = out_dir / "summary.csv"
    with open(csv_path, "w") as f:
        f.write(
            "strategy,workload,read_ratio,throughput_rps,avg_latency_ms,p95_latency_ms,"
            "cache_hits,cache_misses,hit_rate,db_reads,db_writes,db_total,"
            "writeback_pending_pre_flush,writeback_flushed_post,errors\n"
        )
        for strategy in STRATEGIES:
            for wl_name, ratio in WORKLOADS:
                r = all_results[strategy][wl_name]
                c = r["client"]
                s = r["server_during"]
                f.write(
                    f"{strategy},{wl_name},{ratio},"
                    f"{c['client_throughput_rps']:.2f},"
                    f"{c['client_avg_latency_ms']:.3f},"
                    f"{c['client_p95_latency_ms']:.3f},"
                    f"{s['cache_hits']},{s['cache_misses']},"
                    f"{s['cache_hit_rate']:.4f},"
                    f"{s['db_reads']},{s['db_writes']},{s['db_total']},"
                    f"{r['writeback_pending_before_flush']},"
                    f"{r['writeback_flushed_post_run']},"
                    f"{c['client_errors']}\n"
                )
    print(f"[runner] wrote {csv_path}")


if __name__ == "__main__":
    main()
