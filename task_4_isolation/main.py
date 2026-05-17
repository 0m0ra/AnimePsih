"""Run all four isolation-anomaly demos and write per-demo logs."""
from __future__ import annotations

from pathlib import Path

from anomalies import dirty_read, lost_update, non_repeatable_read, phantom_read

RESULTS = Path(__file__).parent / "results"


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    demos = {
        "dirty_read.log":          dirty_read.run,
        "non_repeatable_read.log": non_repeatable_read.run,
        "phantom_read.log":        phantom_read.run,
        "lost_update.log":         lost_update.run,
    }
    for fname, fn in demos.items():
        text = fn()
        (RESULTS / fname).write_text(text + "\n")
        print(f"\n>>> wrote {RESULTS / fname}")


if __name__ == "__main__":
    main()