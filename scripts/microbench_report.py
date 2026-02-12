#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render microbench report markdown.")
    parser.add_argument("--input", default="artifacts/microbench_results.json")
    parser.add_argument("--output", default="artifacts/microbench_report.md")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    results = payload.get("results", [])
    total = len(results)
    passed = sum(1 for row in results if row.get("passed"))
    avg_duration = round(sum(int(row.get("duration_ms", 0)) for row in results) / max(1, total), 2)
    avg_cost = round(sum(float(row.get("cost_usd", 0.0)) for row in results) / max(1, total), 6)

    lines = [
        "# Microbench Report",
        "",
        f"Seed: `{payload.get('seed')}`",
        f"Lane: `{payload.get('lane')}`",
        f"Pass rate: `{passed}/{total}`",
        f"Average duration ms: `{avg_duration}`",
        f"Average cost usd: `{avg_cost}`",
    ]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
