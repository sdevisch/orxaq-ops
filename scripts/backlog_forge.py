#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _todo_hits(root: Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "TODO" in text or "FIXME" in text:
            hits.append(str(path))
    return sorted(set(hits))[:30]


def build_issue_payloads(*, telemetry_path: Path, ci_failures: list[str], todo_paths: list[str]) -> list[dict]:
    payloads: list[dict] = []
    payloads.append(
        {
            "work_id": "phase2-ci-triage",
            "task_type": "verify",
            "risk_level": "low",
            "lane_hint": "L2",
            "title": "Triage CI failures into actionable fixes",
            "acceptance": ["Reproduce failure", "Root cause documented", "Fix task filed"],
            "evidence_required": ["ci_log_excerpt"],
            "stop_conditions": ["No unresolved CI failures older than 24h"],
            "details": ci_failures,
        }
    )
    if todo_paths:
        payloads.append(
            {
                "work_id": "phase2-todo-sweep",
                "task_type": "triage",
                "risk_level": "low",
                "lane_hint": "L0",
                "title": "Convert TODO/FIXME into typed backlog issues",
                "acceptance": ["Each TODO mapped to issue", "Issue has lane+risk labels"],
                "evidence_required": ["todo_scan_report"],
                "stop_conditions": ["No unlabeled TODO issues"],
                "details": todo_paths,
            }
        )
    if telemetry_path.exists():
        payloads.append(
            {
                "work_id": "phase2-telemetry-anomaly-check",
                "task_type": "research",
                "risk_level": "medium",
                "lane_hint": "L4",
                "title": "Investigate telemetry anomalies by task_type",
                "acceptance": ["Top failing task types identified", "Hypothesis issues opened"],
                "evidence_required": ["swarm_events.jsonl", "lane_ate.md"],
                "stop_conditions": ["At least one hypothesis issue created"],
                "details": [str(telemetry_path)],
            }
        )
    return payloads


def _render_markdown(payloads: list[dict]) -> str:
    lines = ["# Backlog Forge Output", ""]
    for item in payloads:
        lines.extend(
            [
                f"## {item['title']}",
                f"- work_id: `{item['work_id']}`",
                f"- task_type: `{item['task_type']}`",
                f"- risk_level: `{item['risk_level']}`",
                f"- lane_hint: `{item['lane_hint']}`",
                "- acceptance:",
            ]
        )
        for value in item["acceptance"]:
            lines.append(f"  - {value}")
        lines.append("- evidence_required:")
        for value in item["evidence_required"]:
            lines.append(f"  - {value}")
        lines.append("- stop_conditions:")
        for value in item["stop_conditions"]:
            lines.append(f"  - {value}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Backlog Forge issue generation.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--telemetry", default="artifacts/swarm_events.jsonl")
    parser.add_argument("--ci-failures", default="artifacts/ci_failures.json")
    parser.add_argument("--output", default="artifacts/backlog_forge.md")
    args = parser.parse_args()

    root = Path(args.root)
    telemetry = Path(args.telemetry)
    ci_failures_path = Path(args.ci_failures)
    ci_failures = []
    if ci_failures_path.exists():
        ci_failures = json.loads(ci_failures_path.read_text(encoding="utf-8"))
    todo_paths = _todo_hits(root)
    payloads = build_issue_payloads(telemetry_path=telemetry, ci_failures=ci_failures, todo_paths=todo_paths)

    # Keep GitHub API optional as requested.
    if not os.environ.get("GITHUB_TOKEN"):
        Path(args.output).write_text(_render_markdown(payloads), encoding="utf-8")
        return 0

    # Online creation intentionally omitted in default local mode.
    Path(args.output).write_text(json.dumps(payloads, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
