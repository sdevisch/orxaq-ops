#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from orxaq_autonomy.phase2_core import seeded_shuffle


def _load_suite(path: str) -> list[dict]:
    # JSON is valid YAML, keeps parser deterministic without extra deps.
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _score(task: dict, lane: str) -> dict:
    task_id = str(task["id"])
    seed = int(hashlib.sha256(f"{task_id}:{lane}".encode("utf-8")).hexdigest()[:8], 16)
    passed = (seed % 100) >= 10
    duration_ms = 50 + (seed % 250)
    cost_usd = 0.0 if lane in {"L0", "L1"} else round(0.001 + (seed % 40) / 10000.0, 6)
    return {
        "id": task_id,
        "task_type": task["task_type"],
        "lane": lane,
        "passed": passed,
        "duration_ms": duration_ms,
        "cost_usd": cost_usd,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic microbench dry-run.")
    parser.add_argument("--suite", default="config/microbench_suite.yaml")
    parser.add_argument("--output", default="artifacts/microbench_results.json")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--lane", default="L0")
    args = parser.parse_args()

    suite = _load_suite(args.suite)
    ordered = seeded_shuffle(suite, seed=args.seed)
    results = [_score(item, args.lane) for item in ordered]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"seed": args.seed, "lane": args.lane, "results": results}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
