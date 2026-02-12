#!/usr/bin/env python3
"""Run swarm health against a temporary clean worktree snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _run(cmd: list[str], *, cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    return (int(proc.returncode), proc.stdout, proc.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run strict+operational swarm health on a clean temporary worktree.")
    parser.add_argument("--ops-root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--source-root", default="../orxaq", help="Path to source orxaq repository.")
    parser.add_argument(
        "--connectivity-report",
        default="artifacts/model_connectivity.json",
        help="Path to connectivity report (relative to ops root or absolute).",
    )
    parser.add_argument(
        "--strict-output",
        default="artifacts/autonomy/health_snapshot/strict.json",
        help="Strict health JSON output path (relative to ops root or absolute).",
    )
    parser.add_argument(
        "--operational-output",
        default="artifacts/autonomy/health_snapshot/operational.json",
        help="Operational health JSON output path (relative to ops root or absolute).",
    )
    parser.add_argument("--threshold", type=int, default=85, help="Strict threshold.")
    parser.add_argument("--fail-on-strict", action="store_true", help="Exit non-zero when strict pass gate fails.")
    args = parser.parse_args()

    ops_root = Path(args.ops_root).expanduser().resolve()
    source_root = Path(args.source_root).expanduser().resolve()
    connectivity_report = Path(args.connectivity_report)
    if not connectivity_report.is_absolute():
        connectivity_report = (ops_root / connectivity_report).resolve()

    strict_output = Path(args.strict_output)
    if not strict_output.is_absolute():
        strict_output = (ops_root / strict_output).resolve()

    operational_output = Path(args.operational_output)
    if not operational_output.is_absolute():
        operational_output = (ops_root / operational_output).resolve()

    strict_output.parent.mkdir(parents=True, exist_ok=True)
    operational_output.parent.mkdir(parents=True, exist_ok=True)

    tmp_parent = Path(tempfile.mkdtemp(prefix="orxaq-health-snapshot-"))
    tmp_dir = tmp_parent / "worktree"
    strict_json = tmp_dir / "artifacts" / "health.json"
    operational_json = tmp_dir / "artifacts" / "health_operational.json"

    strict_rc = 0
    try:
        add_cmd = ["git", "-C", str(source_root), "worktree", "add", "--detach", str(tmp_dir), "HEAD"]
        add_rc, add_out, add_err = _run(add_cmd, cwd=ops_root)
        if add_rc != 0:
            print(json.dumps({"ok": False, "stage": "worktree_add", "stdout": add_out, "stderr": add_err}, sort_keys=True))
            return 1

        strict_cmd = [
            "python3",
            str(source_root / "orxaq_cli.py"),
            "swarm-health",
            "--root",
            str(tmp_dir),
            "--output",
            str(strict_json),
            "--strict",
            "--threshold",
            str(max(1, int(args.threshold))),
            "--connectivity-report",
            str(connectivity_report),
        ]
        strict_rc, strict_out, strict_err = _run(strict_cmd, cwd=ops_root)

        operational_cmd = [
            "python3",
            str(source_root / "orxaq_cli.py"),
            "swarm-health",
            "--root",
            str(tmp_dir),
            "--output",
            str(operational_json),
            "--threshold",
            str(max(1, int(args.threshold))),
            "--connectivity-report",
            str(connectivity_report),
            "--skip-quality-gates",
            "--skip-security-gates",
        ]
        op_rc, op_out, op_err = _run(operational_cmd, cwd=ops_root)

        if strict_json.exists():
            shutil.copy2(strict_json, strict_output)
        if operational_json.exists():
            shutil.copy2(operational_json, operational_output)

        strict_payload = _load_json(strict_output)
        operational_payload = _load_json(operational_output)

        print(
            json.dumps(
                {
                    "ok": True,
                    "snapshot_root": str(tmp_dir),
                    "strict_output": str(strict_output),
                    "operational_output": str(operational_output),
                    "strict_returncode": strict_rc,
                    "operational_returncode": op_rc,
                    "strict_pass_gate": strict_payload.get("pass_gate"),
                    "strict_score": strict_payload.get("score"),
                    "operational_pass_gate": operational_payload.get("pass_gate"),
                    "operational_score": operational_payload.get("score"),
                    "strict_stdout": strict_out.strip()[-300:],
                    "strict_stderr": strict_err.strip()[-300:],
                    "operational_stdout": op_out.strip()[-300:],
                    "operational_stderr": op_err.strip()[-300:],
                },
                sort_keys=True,
            )
        )

        if args.fail_on_strict and not bool(strict_payload.get("pass_gate", False)):
            return 2
        return 0
    finally:
        remove_cmd = ["git", "-C", str(source_root), "worktree", "remove", "--force", str(tmp_dir)]
        _run(remove_cmd, cwd=ops_root)
        if tmp_parent.exists():
            shutil.rmtree(tmp_parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
