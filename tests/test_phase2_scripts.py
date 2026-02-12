from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_recommend_routing_policy_stable(tmp_path: Path) -> None:
    inp = tmp_path / "microbench_results.json"
    inp.write_text(
        json.dumps(
            {
                "seed": 17,
                "lane": "L0",
                "results": [
                    {"id": "1", "task_type": "docs", "passed": True, "duration_ms": 100, "cost_usd": 0.0},
                    {"id": "2", "task_type": "tests", "passed": False, "duration_ms": 300, "cost_usd": 0.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "recommended_routing_policy.yaml"
    subprocess.check_call(
        [
            "python3",
            "scripts/recommend_routing_policy.py",
            "--input",
            str(inp),
            "--output",
            str(out),
            "--diff-output",
            str(tmp_path / "diff.md"),
        ],
        cwd="/Users/sdevisch/dev/orxaq-ops",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["default"]["provider"] == "lmstudio"
    assert payload["task_types"]["docs"]["lane"] == "L0"


def test_lane_ate_report_runs(tmp_path: Path) -> None:
    events = tmp_path / "swarm_events.jsonl"
    events.write_text(
        '{"event_id":"1","timestamp":"2026-01-01T00:00:00+00:00","event_type":"task_end","work_id":"a","task_type":"docs","risk_level":"low","lane":"L0","provider":"lmstudio","model":"m","status":"success"}\n',
        encoding="utf-8",
    )
    out = tmp_path / "lane_ate.md"
    subprocess.check_call(
        ["python3", "scripts/lane_ate_report.py", "--events", str(events), "--output", str(out)],
        cwd="/Users/sdevisch/dev/orxaq-ops",
    )
    assert "Lane ATE Report" in out.read_text(encoding="utf-8")
