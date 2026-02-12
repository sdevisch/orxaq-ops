"""Autonomy stop-report generation and optional GitHub issue filing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def _safe_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_stop_report(*, root: Path, health_path: Path, heartbeat_path: Path, state_path: Path, output_path: Path) -> Path:
    health = _safe_json(health_path)
    heartbeat = _safe_json(heartbeat_path)
    state = _safe_json(state_path)

    last_task = str(heartbeat.get("task_id", ""))
    health_score = health.get("score", "unknown")
    blocked = [k for k, v in state.items() if isinstance(v, dict) and v.get("status") == "blocked"]

    lines = [
        "# AUTONOMY STOP REPORT",
        "",
        f"- Last executed task: `{last_task or 'unknown'}`",
        f"- Health score: `{health_score}`",
        f"- Blocked tasks: `{', '.join(blocked) if blocked else 'none'}`",
        "- Suggested smallest fix path: repair first blocked task, rerun validations, resume run.",
        "",
        "## Inputs",
        f"- Health report: `{health_path}`",
        f"- Heartbeat: `{heartbeat_path}`",
        f"- State file: `{state_path}`",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def file_issue(*, root: Path, title: str, body_path: Path) -> dict[str, Any]:
    cmd = ["gh", "issue", "create", "--title", title, "--body-file", str(body_path)]
    completed = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, check=False)
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }
