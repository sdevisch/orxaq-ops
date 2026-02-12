#!/usr/bin/env python3
"""Hourly remediation loop that drives swarm health toward bright green."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT = Path("artifacts/autonomy/health_green_loop/latest.json")
DEFAULT_HISTORY = Path("artifacts/autonomy/health_green_loop/history.ndjson")
DEFAULT_PID = Path("artifacts/autonomy/health_green_loop/loop.pid")
DEFAULT_LOG = Path("artifacts/autonomy/health_green_loop/loop.log")
DEFAULT_LOCK = Path("artifacts/autonomy/health_green_loop/loop.lock")
DEFAULT_INTERVAL_SEC = 3600
DEFAULT_LOW_CODEX_MODEL = "gpt-5-mini"

STOP = False


def _sig_handler(_sig: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_path(raw: str) -> Path:
    return Path(str(raw)).expanduser().resolve()


def _run_json(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[bool, dict[str, Any], str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    text = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, {}, (proc.stderr or text or "").strip()
    try:
        payload = json.loads(text)
    except Exception:
        return False, {}, "json_parse_failed"
    if not isinstance(payload, dict):
        return False, {}, "json_not_object"
    return True, payload, ""


def _run_cmd(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[bool, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    return False, (proc.stderr or proc.stdout or "").strip()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _issue_list(health: dict[str, Any], cleanup: dict[str, Any], dashboard: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    status = health.get("status", {}) if isinstance(health.get("status"), dict) else {}
    state_counts = health.get("state_counts", {}) if isinstance(health.get("state_counts"), dict) else {}
    blocked_tasks = health.get("blocked_tasks", []) if isinstance(health.get("blocked_tasks"), list) else []

    if not bool(status.get("supervisor_running", False)):
        issues.append("supervisor_not_running")
    if bool(health.get("heartbeat_stale", True)):
        issues.append("heartbeat_stale")
    if int(state_counts.get("blocked", 0) or 0) > 0 or len(blocked_tasks) > 0:
        issues.append("blocked_tasks_present")
    if not bool(dashboard.get("running", False)):
        issues.append("dashboard_not_running")

    summary = cleanup.get("summary", {}) if isinstance(cleanup.get("summary"), dict) else {}
    repo_issue_count = int(summary.get("repo_issue_count", 0) or 0)
    misplaced_count = int(summary.get("misplaced_candidate_count", 0) or 0)
    if repo_issue_count > 0:
        issues.append("repo_issues_present")
    if misplaced_count > 0:
        issues.append("misplaced_files_present")
    return issues


def _acquire_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+", encoding="utf-8")
    if os.name == "nt":
        import msvcrt  # type: ignore

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl  # type: ignore

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def _release_lock(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt  # type: ignore

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # type: ignore

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()


def _write_report(report: dict[str, Any], output_file: Path, history_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    row = {
        "timestamp": str(report.get("timestamp", "")),
        "bright_green": bool(report.get("bright_green", False)),
        "issue_count": len(report.get("issues_remaining", []) or []),
        "actions_taken": len(report.get("actions", []) or []),
        "iterations": int(report.get("iterations", 0) or 0),
    }
    with history_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _health_pass(args: argparse.Namespace) -> dict[str, Any]:
    root = _to_path(args.root)
    lock_file = _to_path(args.lock_file)
    output_file = _to_path(args.output_file)
    history_file = _to_path(args.history_file)

    try:
        lock_handle = _acquire_lock(lock_file)
    except OSError:
        report = {
            "timestamp": _utc_now_iso(),
            "bright_green": False,
            "skipped": True,
            "skip_reason": "lock_busy",
            "root_dir": str(root),
            "lock_file": str(lock_file),
        }
        _write_report(report, output_file, history_file)
        return report

    actions: list[dict[str, Any]] = []
    low_model = str(args.low_codex_model).strip() or DEFAULT_LOW_CODEX_MODEL
    low_env = os.environ.copy()
    low_env["ORXAQ_AUTONOMY_CODEX_MODEL"] = low_model

    try:
        ok, health, health_err = _run_json(["python3", "-m", "orxaq_autonomy.cli", "--root", str(root), "health"])
        if not ok:
            report = {
                "timestamp": _utc_now_iso(),
                "bright_green": False,
                "skipped": False,
                "root_dir": str(root),
                "error": f"health_probe_failed:{health_err}",
                "actions": actions,
            }
            _write_report(report, output_file, history_file)
            return report

        cleanup = _load_json(root / "artifacts" / "autonomy" / "cleanup_loop" / "latest.json")
        ok_dash, dashboard, _ = _run_json(
            ["python3", "-m", "orxaq_autonomy.cli", "--root", str(root), "dashboard-status"]
        )
        if not ok_dash:
            dashboard = {}

        issues = _issue_list(health, cleanup, dashboard)
        iterations = 0
        max_iters = max(1, int(args.max_iterations))

        while issues and iterations < max_iters:
            iterations += 1
            issue = issues[0]
            if issue == "supervisor_not_running":
                ok_cmd, msg = _run_cmd(["python3", "-m", "orxaq_autonomy.cli", "--root", str(root), "start"], env=low_env)
                actions.append({"iteration": iterations, "issue": issue, "action": "start", "ok": ok_cmd, "message": msg})
            elif issue in {"heartbeat_stale", "blocked_tasks_present"}:
                ok_cmd, msg = _run_cmd(["python3", "-m", "orxaq_autonomy.cli", "--root", str(root), "ensure"], env=low_env)
                actions.append({"iteration": iterations, "issue": issue, "action": "ensure", "ok": ok_cmd, "message": msg})
                ok_lane, lane_msg = _run_cmd(
                    ["python3", "-m", "orxaq_autonomy.cli", "--root", str(root), "lanes-ensure", "--json"],
                    env=low_env,
                )
                actions.append(
                    {"iteration": iterations, "issue": issue, "action": "lanes-ensure", "ok": ok_lane, "message": lane_msg}
                )
            elif issue == "dashboard_not_running":
                ok_cmd, msg = _run_cmd(
                    ["python3", "-m", "orxaq_autonomy.cli", "--root", str(root), "dashboard-ensure"],
                    env=low_env,
                )
                actions.append(
                    {"iteration": iterations, "issue": issue, "action": "dashboard-ensure", "ok": ok_cmd, "message": msg}
                )
            elif issue in {"repo_issues_present", "misplaced_files_present"}:
                ok_cmd, msg = _run_cmd(["python3", str(root / "scripts" / "cleanup_loop.py"), "--root", str(root)], env=low_env)
                actions.append(
                    {"iteration": iterations, "issue": issue, "action": "cleanup-loop-once", "ok": ok_cmd, "message": msg}
                )
            else:
                actions.append({"iteration": iterations, "issue": issue, "action": "noop", "ok": False, "message": "unknown_issue"})
                break

            ok, health, health_err = _run_json(["python3", "-m", "orxaq_autonomy.cli", "--root", str(root), "health"])
            if not ok:
                actions.append(
                    {
                        "iteration": iterations,
                        "issue": issue,
                        "action": "reprobe-health",
                        "ok": False,
                        "message": health_err,
                    }
                )
                break
            cleanup = _load_json(root / "artifacts" / "autonomy" / "cleanup_loop" / "latest.json")
            ok_dash, dashboard, _ = _run_json(
                ["python3", "-m", "orxaq_autonomy.cli", "--root", str(root), "dashboard-status"]
            )
            if not ok_dash:
                dashboard = {}
            issues = _issue_list(health, cleanup, dashboard)

        bright_green = len(issues) == 0
        report = {
            "timestamp": _utc_now_iso(),
            "bright_green": bright_green,
            "skipped": False,
            "root_dir": str(root),
            "low_codex_model": low_model,
            "iterations": iterations,
            "max_iterations": max_iters,
            "issues_remaining": issues,
            "actions": actions,
            "health": health,
            "cleanup": cleanup,
            "dashboard": dashboard,
        }
        _write_report(report, output_file, history_file)
        return report
    finally:
        _release_lock(lock_handle)


def _spawn_daemon(args: argparse.Namespace) -> int:
    root = _to_path(args.root)
    pid_file = _to_path(args.pid_file)
    log_file = _to_path(args.log_file)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    child_args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--root",
        str(root),
        "--watch",
        "--interval-sec",
        str(max(300, int(args.interval_sec))),
        "--output-file",
        str(_to_path(args.output_file)),
        "--history-file",
        str(_to_path(args.history_file)),
        "--lock-file",
        str(_to_path(args.lock_file)),
        "--max-iterations",
        str(max(1, int(args.max_iterations))),
        "--low-codex-model",
        str(args.low_codex_model),
    ]
    with log_file.open("a", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            child_args,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    print(proc.pid)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drive autonomy health to bright green with safe remediation.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--output-file", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID))
    parser.add_argument("--log-file", default=str(DEFAULT_LOG))
    parser.add_argument("--lock-file", default=str(DEFAULT_LOCK))
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC)
    parser.add_argument("--max-iterations", type=int, default=6)
    parser.add_argument("--low-codex-model", default=DEFAULT_LOW_CODEX_MODEL)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if bool(args.daemon):
        return _spawn_daemon(args)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    if not bool(args.watch):
        report = _health_pass(args)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    interval_sec = max(300, int(args.interval_sec))
    while not STOP:
        report = _health_pass(args)
        print(json.dumps(report, sort_keys=True), flush=True)
        for _ in range(interval_sec):
            if STOP:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
