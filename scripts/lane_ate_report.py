#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from orxaq_autonomy.phase2_core import lane_ate, read_events


def render_markdown(report: dict) -> str:
    lines = [
        "# Lane ATE Report",
        "",
        f"Control lane: `{report['control_lane']}`",
        f"Treatment lane: `{report['treatment_lane']}`",
        "",
        "| task_type | n_control | n_treatment | ate_success | ate_duration_ms | ate_cost_usd |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for task_type, row in report.get("task_types", {}).items():
        lines.append(
            f"| {task_type} | {row['n_control']} | {row['n_treatment']} | {row['ate_success']} | {row['ate_duration_ms']} | {row['ate_cost_usd']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate lane ATE report from swarm events.")
    parser.add_argument("--events", default="artifacts/swarm_events.jsonl")
    parser.add_argument("--output", default="artifacts/lane_ate.md")
    args = parser.parse_args()

    events = read_events(args.events)
    report = lane_ate(events)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
