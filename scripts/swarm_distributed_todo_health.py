#!/usr/bin/env python3
"""Health checker for swarm lanes + distributed to-do artifacts."""

from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from orxaq_autonomy.cli import _safe_lane_status_snapshot
from orxaq_autonomy.dashboard import _safe_distributed_todo_snapshot, _safe_watchdog_snapshot
from orxaq_autonomy.manager import ManagerConfig

DEFAULT_OUTPUT = Path("artifacts/autonomy/swarm_todo_health/latest.json")
DEFAULT_HISTORY = Path("artifacts/autonomy/swarm_todo_health/history.ndjson")
DEFAULT_PID = Path("artifacts/autonomy/swarm_todo_health/health.pid")
DEFAULT_LOG = Path("artifacts/autonomy/swarm_todo_health/health.log")
DEFAULT_TODO_GLOBS = (
    "{root}/ops/backlog/distributed_todo.yaml",
    "{root_parent}/orxaq/ops/backlog/distributed_todo.yaml",
    "{root_parent}/.worktrees/*/ops/backlog/distributed_todo.yaml",
)

STOP = False
HEALTHY_LANE_STATES = {"ok", "idle", "paused"}
ACTIVE_TODO_STATES = {"todo", "doing", "review", "blocked"}


def _sig_handler(_sig: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _to_path(raw: str) -> Path:
    return Path(str(raw)).expanduser().resolve()


@contextmanager
def _temporary_env(name: str, value: str | None):
    had = name in os.environ
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if had:
            os.environ[name] = old or ""
        else:
            os.environ.pop(name, None)


def _resolve_todo_patterns(root: Path, raw_patterns: Iterable[str]) -> list[str]:
    root_parent = root.parent.resolve()
    out: list[str] = []
    for raw in raw_patterns:
        text = str(raw).strip()
        if not text:
            continue
        out.append(text.format(root=str(root), root_parent=str(root_parent)))
    return out


def _discover_todo_files(
    *,
    root: Path,
    explicit_files: list[str],
    patterns: list[str],
    discover: bool,
) -> tuple[list[Path], list[Path]]:
    found: dict[str, Path] = {}
    missing_explicit: list[Path] = []

    for raw in explicit_files:
        path = _to_path(raw)
        if path.exists() and path.is_file():
            found[str(path)] = path
        else:
            missing_explicit.append(path)

    if discover:
        for pattern in _resolve_todo_patterns(root, patterns):
            for match in sorted(glob.glob(pattern)):
                path = _to_path(match)
                if path.exists() and path.is_file():
                    found[str(path)] = path

    return sorted(found.values(), key=lambda p: str(p)), missing_explicit


def _snapshot_todo_file(cfg: ManagerConfig, todo_file: Path) -> dict[str, Any]:
    with _temporary_env("ORXAQ_DISTRIBUTED_TODO_FILE", str(todo_file)):
        payload = _safe_distributed_todo_snapshot(cfg)

    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    active_requests = payload.get("active_requests_all", payload.get("active_requests", []))
    active_items = active_requests if isinstance(active_requests, list) else []
    swarm_counts: dict[str, int] = {}
    unassigned_active = 0
    for item in active_items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).strip().lower()
        if status and status not in ACTIVE_TODO_STATES:
            continue
        swarm = str(item.get("assigned_swarm", "")).strip().lower()
        if swarm:
            swarm_counts[swarm] = swarm_counts.get(swarm, 0) + 1
        else:
            unassigned_active += 1

    return {
        "todo_file": str(todo_file),
        "ok": bool(payload.get("ok", False)),
        "partial": bool(payload.get("partial", False)),
        "errors": payload.get("errors", []) if isinstance(payload.get("errors"), list) else [],
        "summary": {
            "cycle_id": str(summary.get("cycle_id", "")).strip(),
            "generated_utc": str(summary.get("generated_utc", "")).strip(),
            "task_total": _as_int(summary.get("task_total", 0), 0),
            "open_task_count": _as_int(summary.get("open_task_count", 0), 0),
            "done_task_count": _as_int(summary.get("done_task_count", 0), 0),
            "blocked_task_count": _as_int(summary.get("blocked_task_count", 0), 0),
            "p0_open_count": _as_int(summary.get("p0_open_count", 0), 0),
            "file_age_sec": _as_int(summary.get("file_age_sec", -1), -1),
            "active_watch_live_uncovered_count": _as_int(summary.get("active_watch_live_uncovered_count", 0), 0),
            "active_watch_total": _as_int(summary.get("active_watch_total", 0), 0),
            "file_modified_at": str(summary.get("file_modified_at", "")).strip(),
        },
        "active_swarm_counts": dict(sorted(swarm_counts.items())),
        "unassigned_active_task_count": unassigned_active,
    }


def _summarize_top_counts(counts: dict[str, int], *, limit: int = 8) -> list[dict[str, Any]]:
    rows = [{"key": key, "count": value} for key, value in counts.items() if value > 0]
    rows.sort(key=lambda row: (-int(row["count"]), str(row["key"])))
    return rows[: max(1, int(limit))]


def _run_health_pass(args: argparse.Namespace) -> dict[str, Any]:
    root = _to_path(args.root)
    cfg = ManagerConfig.from_root(root)

    lane_payload = _safe_lane_status_snapshot(cfg)
    lane_items = lane_payload.get("lanes", []) if isinstance(lane_payload.get("lanes"), list) else []
    lane_errors = lane_payload.get("errors", []) if isinstance(lane_payload.get("errors"), list) else []
    lane_health_counts_raw = lane_payload.get("health_counts", {})
    lane_health_counts = lane_health_counts_raw if isinstance(lane_health_counts_raw, dict) else {}
    lane_total = len([item for item in lane_items if isinstance(item, dict)])
    running_count = sum(1 for item in lane_items if isinstance(item, dict) and bool(item.get("running", False)))

    watchdog = _safe_watchdog_snapshot(cfg, events=max(1, int(args.watchdog_events)))

    todo_files, missing_explicit = _discover_todo_files(
        root=root,
        explicit_files=list(args.todo_file or []),
        patterns=list(args.todo_glob or []),
        discover=bool(args.discover),
    )
    todo_rows = [_snapshot_todo_file(cfg, todo) for todo in todo_files]

    failures: list[str] = []
    warnings: list[str] = []

    if lane_errors:
        failures.extend([f"lane_status_error:{msg}" for msg in lane_errors])
    if bool(args.require_running_lanes) and lane_total > 0 and running_count == 0:
        failures.append("no_running_lanes")
    degraded_lanes = {
        str(state): _as_int(count, 0)
        for state, count in lane_health_counts.items()
        if str(state).strip().lower() not in HEALTHY_LANE_STATES and _as_int(count, 0) > 0
    }
    if degraded_lanes:
        warnings.append(
            "lane_health_degraded:"
            + ",".join(f"{state}={count}" for state, count in sorted(degraded_lanes.items()))
        )

    watchdog_ok = bool(watchdog.get("ok", False))
    watchdog_errors = watchdog.get("errors", []) if isinstance(watchdog.get("errors"), list) else []
    if not watchdog_ok:
        failures.extend([f"watchdog_error:{msg}" for msg in watchdog_errors])
    problematic_count = _as_int(watchdog.get("problematic_count", 0), 0)
    if problematic_count > 0:
        failures.append(f"watchdog_problematic_processes:{problematic_count}")
    last_run_age_sec = _as_int(watchdog.get("last_run_age_sec", -1), -1)
    if bool(args.require_watchdog_recent):
        if last_run_age_sec < 0:
            failures.append("watchdog_last_run_unknown")
        elif last_run_age_sec > int(args.max_watchdog_age_sec):
            failures.append(f"watchdog_stale:{last_run_age_sec}>{int(args.max_watchdog_age_sec)}")
    else:
        if last_run_age_sec < 0:
            warnings.append("watchdog_last_run_unknown")
        elif last_run_age_sec > int(args.max_watchdog_age_sec):
            warnings.append(f"watchdog_stale:{last_run_age_sec}>{int(args.max_watchdog_age_sec)}")

    if missing_explicit:
        failures.extend([f"todo_file_missing:{path}" for path in missing_explicit])
    if not todo_rows:
        failures.append("distributed_todo_missing:no_discoverable_files")

    swarm_totals: dict[str, int] = {}
    task_total = 0
    blocked_total = 0
    unassigned_total = 0
    stale_file_count = 0
    todo_file_errors = 0
    for row in todo_rows:
        summary = row.get("summary", {}) if isinstance(row.get("summary"), dict) else {}
        task_total += _as_int(summary.get("task_total", 0), 0)
        blocked_total += _as_int(summary.get("blocked_task_count", 0), 0)
        unassigned_total += _as_int(row.get("unassigned_active_task_count", 0), 0)
        file_age_sec = _as_int(summary.get("file_age_sec", -1), -1)
        if file_age_sec >= 0 and file_age_sec > int(args.max_todo_age_sec):
            stale_file_count += 1
            failures.append(f"todo_file_stale:{row.get('todo_file')}:{file_age_sec}>{int(args.max_todo_age_sec)}")
        if not bool(row.get("ok", False)):
            todo_file_errors += 1
            errors = row.get("errors", []) if isinstance(row.get("errors"), list) else []
            if errors:
                failures.extend([f"todo_error:{row.get('todo_file')}:{msg}" for msg in errors])
            else:
                failures.append(f"todo_error:{row.get('todo_file')}:unknown")
        if _as_int(summary.get("task_total", 0), 0) == 0:
            warnings.append(f"todo_empty:{row.get('todo_file')}")
        for swarm, count in (row.get("active_swarm_counts", {}) or {}).items():
            swarm_totals[str(swarm)] = swarm_totals.get(str(swarm), 0) + _as_int(count, 0)

    if unassigned_total > 0:
        warnings.append(f"todo_unassigned_active_tasks:{unassigned_total}")

    report = {
        "timestamp": _utc_now_iso(),
        "ok": len(failures) == 0,
        "root_dir": str(root),
        "failures": failures,
        "warnings": warnings,
        "lane": {
            "errors": lane_errors,
            "lane_total": lane_total,
            "running_count": running_count,
            "health_counts": lane_health_counts,
            "degraded_counts": degraded_lanes,
        },
        "watchdog": {
            "ok": watchdog_ok,
            "partial": bool(watchdog.get("partial", False)),
            "errors": watchdog_errors,
            "state_file": str(watchdog.get("state_file", "")),
            "history_file": str(watchdog.get("history_file", "")),
            "runs_total": _as_int(watchdog.get("runs_total", 0), 0),
            "last_run_at": str(watchdog.get("last_run_at", "")).strip(),
            "last_run_age_sec": last_run_age_sec,
            "problematic_count": problematic_count,
            "problematic_ids": watchdog.get("problematic_ids", []),
            "healthy_count": _as_int(watchdog.get("healthy_count", 0), 0),
            "restarted_count": _as_int(watchdog.get("restarted_count", 0), 0),
            "total_processes": _as_int(watchdog.get("total_processes", 0), 0),
        },
        "distributed_todo": {
            "file_count": len(todo_rows),
            "missing_explicit_count": len(missing_explicit),
            "error_file_count": todo_file_errors,
            "stale_file_count": stale_file_count,
            "task_total": task_total,
            "blocked_task_total": blocked_total,
            "unassigned_active_task_total": unassigned_total,
            "active_swarms": _summarize_top_counts(swarm_totals, limit=12),
            "files": todo_rows,
        },
    }
    return report


def _write_report(report: dict[str, Any], *, output_file: Path, history_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    history_row = {
        "timestamp": str(report.get("timestamp", "")),
        "ok": bool(report.get("ok", False)),
        "failure_count": len(report.get("failures", []) or []),
        "warning_count": len(report.get("warnings", []) or []),
        "lane_running_count": _as_int((report.get("lane", {}) or {}).get("running_count", 0), 0),
        "todo_file_count": _as_int((report.get("distributed_todo", {}) or {}).get("file_count", 0), 0),
    }
    with history_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_row, sort_keys=True) + "\n")


def _print_human(report: dict[str, Any]) -> None:
    status = "OK" if bool(report.get("ok", False)) else "FAILED"
    lane = report.get("lane", {}) if isinstance(report.get("lane"), dict) else {}
    watchdog = report.get("watchdog", {}) if isinstance(report.get("watchdog"), dict) else {}
    todo = report.get("distributed_todo", {}) if isinstance(report.get("distributed_todo"), dict) else {}
    print(
        "swarm+todo health "
        f"{status}: lanes_running={_as_int(lane.get('running_count', 0), 0)}/{_as_int(lane.get('lane_total', 0), 0)} "
        f"watchdog_problematic={_as_int(watchdog.get('problematic_count', 0), 0)} "
        f"todo_files={_as_int(todo.get('file_count', 0), 0)} "
        f"todo_tasks={_as_int(todo.get('task_total', 0), 0)}",
    )
    failures = report.get("failures", []) if isinstance(report.get("failures"), list) else []
    warnings = report.get("warnings", []) if isinstance(report.get("warnings"), list) else []
    if failures:
        print("failures:")
        for failure in failures:
            print(f"- {failure}")
    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"- {warning}")


def _run_once(args: argparse.Namespace) -> int:
    report = _run_health_pass(args)
    output_file = _to_path(args.output_file)
    history_file = _to_path(args.history_file)
    _write_report(report, output_file=output_file, history_file=history_file)
    if bool(args.json):
        print(json.dumps(report, indent=None if bool(args.watch) else 2, sort_keys=True), flush=True)
    else:
        _print_human(report)
    return 0 if bool(report.get("ok", False)) else 1


def _spawn_daemon(args: argparse.Namespace) -> int:
    root = _to_path(args.root)
    pid_file = _to_path(args.pid_file) if str(args.pid_file).strip() else DEFAULT_PID.resolve()
    log_file = _to_path(args.log_file) if str(args.log_file).strip() else DEFAULT_LOG.resolve()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    child_args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--root",
        str(root),
        "--watch",
        "--json",
        "--interval-sec",
        str(max(60, int(args.interval_sec))),
        "--output-file",
        str(_to_path(args.output_file)),
        "--history-file",
        str(_to_path(args.history_file)),
        "--max-todo-age-sec",
        str(int(args.max_todo_age_sec)),
        "--max-watchdog-age-sec",
        str(int(args.max_watchdog_age_sec)),
        "--watchdog-events",
        str(int(args.watchdog_events)),
    ]
    if bool(args.require_running_lanes):
        child_args.append("--require-running-lanes")
    else:
        child_args.append("--allow-zero-running-lanes")
    if bool(args.require_watchdog_recent):
        child_args.append("--require-watchdog-recent")
    if bool(args.discover):
        child_args.append("--discover")
    else:
        child_args.append("--no-discover")
    for item in args.todo_file or []:
        child_args.extend(["--todo-file", str(item)])
    for item in args.todo_glob or []:
        child_args.extend(["--todo-glob", str(item)])

    with log_file.open("a", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            child_args,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    print(proc.pid)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Health check swarm lanes + distributed to-do backlogs.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--output-file", default=str(DEFAULT_OUTPUT), help="Latest JSON report file path.")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY), help="NDJSON run-history file path.")
    parser.add_argument(
        "--todo-file",
        action="append",
        default=[],
        help="Explicit distributed_todo.yaml file path. Repeatable.",
    )
    parser.add_argument(
        "--todo-glob",
        action="append",
        default=list(DEFAULT_TODO_GLOBS),
        help="Discovery glob pattern (supports {root} and {root_parent}). Repeatable.",
    )
    parser.add_argument("--discover", dest="discover", action="store_true", default=True, help="Enable file discovery.")
    parser.add_argument("--no-discover", dest="discover", action="store_false", help="Disable file discovery.")
    parser.add_argument("--max-todo-age-sec", type=int, default=14400, help="Maximum age before a todo file is stale.")
    parser.add_argument(
        "--max-watchdog-age-sec",
        type=int,
        default=1800,
        help="Maximum acceptable watchdog last-run age (seconds).",
    )
    parser.add_argument("--watchdog-events", type=int, default=20, help="Watchdog history events to sample.")
    parser.add_argument(
        "--require-running-lanes",
        dest="require_running_lanes",
        action="store_true",
        default=True,
        help="Fail if zero lanes are running.",
    )
    parser.add_argument(
        "--allow-zero-running-lanes",
        dest="require_running_lanes",
        action="store_false",
        help="Do not fail when zero lanes are running.",
    )
    parser.add_argument(
        "--require-watchdog-recent",
        action="store_true",
        help="Fail if watchdog last run is unknown/stale per --max-watchdog-age-sec.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--watch", action="store_true", help="Run continuously in foreground.")
    parser.add_argument("--interval-sec", type=int, default=3600, help="Foreground/daemon poll interval.")
    parser.add_argument("--daemon", action="store_true", help="Start detached background loop.")
    parser.add_argument("--pid-file", default="", help="Daemon PID file path.")
    parser.add_argument("--log-file", default="", help="Daemon log file path.")
    return parser


def main() -> int:
    args = _parser().parse_args()

    if bool(args.daemon):
        return _spawn_daemon(args)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    interval_sec = max(60, int(args.interval_sec))

    if not bool(args.watch):
        return _run_once(args)

    exit_code = 0
    while not STOP:
        code = _run_once(args)
        if code != 0:
            exit_code = 1
        for _ in range(interval_sec):
            if STOP:
                break
            time.sleep(1)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
