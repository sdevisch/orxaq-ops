#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def _read(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return "(missing)"
    return file_path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble Phase 2 final report from artifacts.")
    parser.add_argument("--output", default="artifacts/Phase2_Final_Report.md")
    args = parser.parse_args()

    lines = [
        "# Phase 2 Final Report",
        "",
        "## LM Studio utilization",
        "Default policy routes to L0/L1 via lmstudio for all task types unless recommendation says otherwise.",
        "",
        "## ATE results",
        _read("artifacts/lane_ate.md"),
        "",
        "## Routing policy recommendations",
        "```json",
        _read("artifacts/recommended_routing_policy.yaml").strip(),
        "```",
        "",
        "## Governance plugin performance",
        _read("artifacts/governance_report.md"),
        "",
        "## Next-phase backlog",
        _read("artifacts/backlog_forge.md"),
    ]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
