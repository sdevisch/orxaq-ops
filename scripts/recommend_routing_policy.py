#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def recommend(results: list[dict]) -> dict:
    by_type: dict[str, list[dict]] = {}
    for row in results:
        by_type.setdefault(str(row.get("task_type", "unknown")), []).append(row)

    task_types: dict[str, dict] = {}
    for task_type, rows in sorted(by_type.items()):
        pass_rate = sum(1 for row in rows if row.get("passed")) / max(1, len(rows))
        avg_duration = sum(int(row.get("duration_ms", 0)) for row in rows) / max(1, len(rows))
        if pass_rate >= 0.9 and avg_duration <= 220:
            lane = "L0"
            provider = "lmstudio"
            model = "local-small"
            reason = "high_pass_low_latency"
        else:
            lane = "L1"
            provider = "lmstudio"
            model = "local-strong"
            reason = "quality_or_latency_guardrail"
        task_types[task_type] = {
            "lane": lane,
            "provider": provider,
            "model": model,
            "reason": reason,
            "budget_usd": 0.01,
        }

    return {
        "default": {
            "lane": "L0",
            "provider": "lmstudio",
            "model": "local-small",
            "reason": "local_first_default",
            "budget_usd": 0.01,
        },
        "task_types": task_types,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Recommend routing policy from microbench results.")
    parser.add_argument("--input", default="artifacts/microbench_results.json")
    parser.add_argument("--output", default="artifacts/recommended_routing_policy.yaml")
    parser.add_argument("--diff-output", default="artifacts/routing_policy_diff.md")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rec = recommend(payload.get("results", []))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(rec, indent=2, sort_keys=True)
    out.write_text(rendered + "\n", encoding="utf-8")

    diff_lines = [
        "# Routing Policy Recommendation",
        "",
        "Generated from deterministic microbench input.",
        "",
        "## Changes",
    ]
    for task_type, cfg in rec.get("task_types", {}).items():
        diff_lines.append(f"- `{task_type}` -> lane `{cfg['lane']}` model `{cfg['model']}` ({cfg['reason']})")
    Path(args.diff_output).write_text("\n".join(diff_lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
