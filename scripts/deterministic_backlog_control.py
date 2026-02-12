#!/usr/bin/env python3
"""Deterministic backlog control loop with bounded ready-window enforcement."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None


DEFAULT_POLICY = Path("config/deterministic_backlog_policy.json")
DEFAULT_OUTPUT = Path("artifacts/autonomy/deterministic_backlog_health.json")
DEFAULT_HISTORY = Path("artifacts/autonomy/deterministic_backlog_history.ndjson")
DEFAULT_PID = Path("artifacts/autonomy/deterministic_backlog.pid")
DEFAULT_LOG = Path("artifacts/autonomy/deterministic_backlog.log")

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STOP = False


def _sig_handler(_sig: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_text(value: Any) -> str:
    return str(value).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_path(raw: str | Path) -> Path:
    return Path(str(raw)).expanduser().resolve()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_backlog_fallback(text: str) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        id_match = re.match(r"^\s*-\s+id:\s*(.+?)\s*$", line)
        if id_match:
            if current is not None:
                tasks.append(current)
            current = {"id": id_match.group(1).strip(), "status": "todo"}
            continue
        if current is None:
            continue
        status_match = re.match(r"^\s*status:\s*(.+?)\s*$", line)
        if status_match:
            current["status"] = status_match.group(1).strip()
            continue
        band_match = re.match(r"^\s*priority_band:\s*(.+?)\s*$", line)
        if band_match:
            current["priority_band"] = band_match.group(1).strip()
            continue
        score_match = re.match(r"^\s*priority_score:\s*(.+?)\s*$", line)
        if score_match:
            current["priority_score"] = score_match.group(1).strip()
            continue
        top_match = re.match(r"^\s*top_priority:\s*(.+?)\s*$", line)
        if top_match:
            current["top_priority"] = top_match.group(1).strip().lower() in {"1", "true", "yes", "on"}
            continue
    if current is not None:
        tasks.append(current)
    return {"tasks": tasks}


def _load_backlog(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists() or not path.is_file():
        return ({}, "missing")
    text = path.read_text(encoding="utf-8", errors="replace")
    if yaml is not None:
        try:
            payload = yaml.safe_load(text)
        except Exception:  # noqa: BLE001
            payload = {}
        if isinstance(payload, dict):
            return (payload, "yaml")
    return (_parse_backlog_fallback(text), "fallback")


def _write_backlog(path: Path, payload: dict[str, Any], *, parse_mode: str) -> None:
    if parse_mode != "yaml" or yaml is None:
        raise RuntimeError("yaml_runtime_required_for_apply")
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False, width=120)
    path.write_text(dumped, encoding="utf-8")


def _priority_high_key(task: dict[str, Any]) -> tuple[int, float, str]:
    band = _as_text(task.get("priority_band", "P3")).upper()
    band_rank = PRIORITY_ORDER.get(band, 4)
    score = _as_float(task.get("priority_score", 0.0), 0.0)
    task_id = _as_text(task.get("id", ""))
    return (band_rank, -score, task_id)


def _priority_low_key(task: dict[str, Any]) -> tuple[int, float, str]:
    band = _as_text(task.get("priority_band", "P3")).upper()
    band_rank = PRIORITY_ORDER.get(band, 4)
    score = _as_float(task.get("priority_score", 0.0), 0.0)
    task_id = _as_text(task.get("id", ""))
    return (-band_rank, score, task_id)


def _status_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = _as_text(task.get("status", "unknown")).lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _ready_count(tasks: list[dict[str, Any]], ready_statuses: set[str]) -> int:
    total = 0
    for task in tasks:
        status = _as_text(task.get("status", "")).lower()
        if status in ready_statuses:
            total += 1
    return total


def _marker_complete(payload: dict[str, Any]) -> bool:
    for key in ("complete", "done", "ok", "pass"):
        if key in payload:
            return _as_bool(payload.get(key), False)
    return False


def _task_backlog_control(task: dict[str, Any]) -> dict[str, Any]:
    current = task.get("backlog_control")
    if isinstance(current, dict):
        return current
    out: dict[str, Any] = {}
    task["backlog_control"] = out
    return out


def _apply_completion(
    tasks: list[dict[str, Any]],
    *,
    marker_dir: Path,
    eligible_statuses: set[str],
    max_complete_per_cycle: int,
    require_task_id_match: bool,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if max_complete_per_cycle <= 0:
        return actions
    for task in sorted(tasks, key=_priority_high_key):
        if len(actions) >= max_complete_per_cycle:
            break
        task_id = _as_text(task.get("id", ""))
        if not task_id:
            continue
        status = _as_text(task.get("status", "")).lower()
        if status not in eligible_statuses:
            continue
        marker_file = marker_dir / f"{task_id}.done.json"
        if not marker_file.exists() or not marker_file.is_file():
            continue
        marker = _load_json(marker_file)
        if not marker:
            continue
        marker_task_id = _as_text(marker.get("task_id", ""))
        if require_task_id_match and marker_task_id and marker_task_id != task_id:
            continue
        if not _marker_complete(marker):
            continue
        before = status
        task["status"] = "done"
        control = _task_backlog_control(task)
        control["completed_at_utc"] = _utc_now_iso()
        control["completed_via"] = "deterministic_marker"
        control["completion_marker"] = str(marker_file)
        actions.append(
            {
                "type": "complete",
                "task_id": task_id,
                "before_status": before,
                "after_status": "done",
                "marker_file": str(marker_file),
            }
        )
    return actions


def _throttle_ready_tasks(
    tasks: list[dict[str, Any]],
    *,
    ready_statuses: set[str],
    target_ready: int,
    max_deactivate_per_cycle: int,
    blocked_reason: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if max_deactivate_per_cycle <= 0:
        return actions
    current_ready = _ready_count(tasks, ready_statuses)
    if current_ready <= target_ready:
        return actions
    candidates = [
        task
        for task in tasks
        if _as_text(task.get("status", "")).lower() == "todo"
        and not _as_bool(task.get("top_priority"), False)
    ]
    candidates.sort(key=_priority_low_key)
    for task in candidates:
        if current_ready <= target_ready or len(actions) >= max_deactivate_per_cycle:
            break
        task_id = _as_text(task.get("id", ""))
        if not task_id:
            continue
        task["status"] = "blocked"
        control = _task_backlog_control(task)
        control["throttled"] = True
        control["throttle_reason"] = blocked_reason
        control["throttled_at_utc"] = _utc_now_iso()
        actions.append(
            {
                "type": "throttle",
                "task_id": task_id,
                "before_status": "todo",
                "after_status": "blocked",
                "reason": blocked_reason,
            }
        )
        current_ready -= 1
    return actions


def _release_throttled_tasks(
    tasks: list[dict[str, Any]],
    *,
    ready_statuses: set[str],
    min_ready: int,
    target_ready: int,
    max_activate_per_cycle: int,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if max_activate_per_cycle <= 0:
        return actions
    current_ready = _ready_count(tasks, ready_statuses)
    if current_ready >= min_ready:
        return actions
    candidates: list[dict[str, Any]] = []
    for task in tasks:
        if _as_text(task.get("status", "")).lower() != "blocked":
            continue
        control = task.get("backlog_control")
        if not isinstance(control, dict):
            continue
        if not _as_bool(control.get("throttled", False), False):
            continue
        candidates.append(task)
    candidates.sort(key=_priority_high_key)
    for task in candidates:
        if current_ready >= target_ready or len(actions) >= max_activate_per_cycle:
            break
        task_id = _as_text(task.get("id", ""))
        if not task_id:
            continue
        task["status"] = "todo"
        control = _task_backlog_control(task)
        control["throttled"] = False
        control["released_at_utc"] = _utc_now_iso()
        actions.append(
            {
                "type": "release",
                "task_id": task_id,
                "before_status": "blocked",
                "after_status": "todo",
            }
        )
        current_ready += 1
    return actions


def _action_counts(actions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in actions:
        action_type = _as_text(action.get("type", "unknown")).lower() or "unknown"
        counts[action_type] = counts.get(action_type, 0) + 1
    return dict(sorted(counts.items()))


def _run_cycle(args: argparse.Namespace) -> dict[str, Any]:
    root = _to_path(args.root)
    policy_file = _to_path(args.policy_file) if str(args.policy_file).strip() else (root / DEFAULT_POLICY).resolve()
    policy = _load_json(policy_file)
    if not policy:
        policy = {
            "schema_version": "deterministic-backlog-control.v1",
            "backlog_file": "../orxaq/ops/backlog/distributed_todo.yaml",
            "ready_statuses": ["todo", "doing", "review"],
            "completion": {
                "enabled": True,
                "eligible_statuses": ["todo", "doing", "review", "blocked"],
                "markers_dir": "artifacts/autonomy/task_markers",
                "max_complete_per_cycle": 8,
                "require_task_id_match": True,
            },
            "bounds": {
                "min_ready": 10,
                "target_ready": 16,
                "max_ready": 24,
                "max_activate_per_cycle": 6,
                "max_deactivate_per_cycle": 6,
            },
            "throttle": {"blocked_reason": "deterministic_backlog_throttle"},
        }

    backlog_override = _as_text(args.backlog_file)
    backlog_raw = backlog_override or _as_text(policy.get("backlog_file", "../orxaq/ops/backlog/distributed_todo.yaml"))
    backlog_path = _to_path(backlog_raw if Path(backlog_raw).is_absolute() else (root / backlog_raw))
    payload, parse_mode = _load_backlog(backlog_path)
    tasks_raw = payload.get("tasks", []) if isinstance(payload.get("tasks"), list) else []
    tasks = [item for item in tasks_raw if isinstance(item, dict)]

    ready_statuses = {
        _as_text(item).lower()
        for item in policy.get("ready_statuses", ["todo", "doing", "review"])
        if _as_text(item)
    }
    if not ready_statuses:
        ready_statuses = {"todo", "doing", "review"}

    completion_cfg = policy.get("completion", {}) if isinstance(policy.get("completion"), dict) else {}
    completion_enabled = _as_bool(completion_cfg.get("enabled", True), True)
    completion_eligible = {
        _as_text(item).lower()
        for item in completion_cfg.get("eligible_statuses", ["todo", "doing", "review", "blocked"])
        if _as_text(item)
    }
    if not completion_eligible:
        completion_eligible = {"todo", "doing", "review", "blocked"}
    markers_dir_raw = _as_text(completion_cfg.get("markers_dir", "artifacts/autonomy/task_markers"))
    markers_dir = _to_path(markers_dir_raw if Path(markers_dir_raw).is_absolute() else (root / markers_dir_raw))
    max_complete_per_cycle = max(0, _as_int(completion_cfg.get("max_complete_per_cycle", 8), 8))
    require_task_id_match = _as_bool(completion_cfg.get("require_task_id_match", True), True)

    bounds = policy.get("bounds", {}) if isinstance(policy.get("bounds"), dict) else {}
    min_ready = max(0, _as_int(bounds.get("min_ready", 10), 10))
    target_ready = max(min_ready, _as_int(bounds.get("target_ready", max(min_ready, 16)), max(min_ready, 16)))
    max_ready = max(target_ready, _as_int(bounds.get("max_ready", max(target_ready, 24)), max(target_ready, 24)))
    max_activate_per_cycle = max(0, _as_int(bounds.get("max_activate_per_cycle", 6), 6))
    max_deactivate_per_cycle = max(0, _as_int(bounds.get("max_deactivate_per_cycle", 6), 6))

    throttle = policy.get("throttle", {}) if isinstance(policy.get("throttle"), dict) else {}
    blocked_reason = _as_text(throttle.get("blocked_reason", "deterministic_backlog_throttle")) or "deterministic_backlog_throttle"

    before_status_counts = _status_counts(tasks)
    before_ready = _ready_count(tasks, ready_statuses)
    before_done = before_status_counts.get("done", 0)

    actions: list[dict[str, Any]] = []
    if completion_enabled:
        actions.extend(
            _apply_completion(
                tasks,
                marker_dir=markers_dir,
                eligible_statuses=completion_eligible,
                max_complete_per_cycle=max_complete_per_cycle,
                require_task_id_match=require_task_id_match,
            )
        )
    actions.extend(
        _throttle_ready_tasks(
            tasks,
            ready_statuses=ready_statuses,
            target_ready=target_ready,
            max_deactivate_per_cycle=max_deactivate_per_cycle,
            blocked_reason=blocked_reason,
        )
    )
    actions.extend(
        _release_throttled_tasks(
            tasks,
            ready_statuses=ready_statuses,
            min_ready=min_ready,
            target_ready=target_ready,
            max_activate_per_cycle=max_activate_per_cycle,
        )
    )

    after_status_counts = _status_counts(tasks)
    after_ready = _ready_count(tasks, ready_statuses)
    after_done = after_status_counts.get("done", 0)

    warnings: list[str] = []
    failures: list[str] = []
    if parse_mode == "missing":
        failures.append(f"backlog_missing:{backlog_path}")
    if after_ready < min_ready:
        failures.append(f"ready_below_min:{after_ready}<{min_ready}")
    if after_ready > max_ready:
        failures.append(f"ready_above_max:{after_ready}>{max_ready}")
    if parse_mode != "yaml" and bool(args.apply):
        failures.append("apply_requires_yaml_runtime")
    if not actions:
        warnings.append("no_backlog_adjustments")

    changed = len(actions) > 0
    blocking_prefixes = ("backlog_missing:", "apply_requires_yaml_runtime")
    blocking_failures = [item for item in failures if any(item.startswith(prefix) for prefix in blocking_prefixes)]
    wrote_backlog = False
    if bool(args.apply) and changed and len(blocking_failures) == 0:
        payload["tasks"] = tasks
        _write_backlog(backlog_path, payload, parse_mode=parse_mode)
        wrote_backlog = True

    report = {
        "schema_version": "deterministic-backlog-health.v1",
        "generated_at_utc": _utc_now_iso(),
        "ok": len(failures) == 0,
        "root_dir": str(root),
        "policy_file": str(policy_file),
        "backlog_file": str(backlog_path),
        "parse_mode": parse_mode,
        "apply_requested": bool(args.apply),
        "backlog_updated": wrote_backlog,
        "failures": failures,
        "warnings": warnings,
        "summary": {
            "task_total": len(tasks),
            "ready_before": before_ready,
            "ready_after": after_ready,
            "done_before": before_done,
            "done_after": after_done,
            "ready_min": min_ready,
            "ready_target": target_ready,
            "ready_max": max_ready,
            "status_counts_before": before_status_counts,
            "status_counts_after": after_status_counts,
            "action_count": len(actions),
            "action_counts": _action_counts(actions),
            "blocking_failure_count": len(blocking_failures),
        },
        "actions": actions[:200],
    }
    return report


def _write_outputs(report: dict[str, Any], *, output_file: Path, history_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    history_row = {
        "timestamp": report.get("generated_at_utc", _utc_now_iso()),
        "ok": _as_bool(report.get("ok", False), False),
        "task_total": _as_int(summary.get("task_total", 0), 0),
        "ready_after": _as_int(summary.get("ready_after", 0), 0),
        "done_after": _as_int(summary.get("done_after", 0), 0),
        "action_count": _as_int(summary.get("action_count", 0), 0),
        "backlog_updated": _as_bool(report.get("backlog_updated", False), False),
    }
    with history_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_row, sort_keys=True) + "\n")


def _run_once(args: argparse.Namespace) -> int:
    report = _run_cycle(args)
    output_file = _to_path(args.output_file)
    history_file = _to_path(args.history_file)
    _write_outputs(report, output_file=output_file, history_file=history_file)
    if bool(args.json):
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        print(
            "deterministic backlog control "
            f"{'OK' if _as_bool(report.get('ok', False), False) else 'FAILED'}: "
            f"ready={_as_int(summary.get('ready_after', 0), 0)} "
            f"done={_as_int(summary.get('done_after', 0), 0)} "
            f"actions={_as_int(summary.get('action_count', 0), 0)} "
            f"updated={_as_bool(report.get('backlog_updated', False), False)}"
        )
    return 0 if _as_bool(report.get("ok", False), False) else 1


def _spawn_daemon(args: argparse.Namespace) -> int:
    root = _to_path(args.root)
    pid_file = _to_path(args.pid_file) if str(args.pid_file).strip() else (root / DEFAULT_PID).resolve()
    log_file = _to_path(args.log_file) if str(args.log_file).strip() else (root / DEFAULT_LOG).resolve()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--root",
        str(root),
        "--policy-file",
        str(args.policy_file),
        "--output-file",
        str(args.output_file),
        "--history-file",
        str(args.history_file),
        "--watch",
        "--interval-sec",
        str(max(30, int(args.interval_sec))),
    ]
    if _as_bool(args.apply, False):
        cmd.append("--apply")
    if _as_text(args.backlog_file):
        cmd.extend(["--backlog-file", str(args.backlog_file)])
    if _as_bool(args.json, False):
        cmd.append("--json")
    with log_file.open("a", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            cmd,
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
    parser = argparse.ArgumentParser(description="Deterministic backlog controller.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY), help="Backlog control policy JSON.")
    parser.add_argument("--backlog-file", default="", help="Optional backlog YAML override.")
    parser.add_argument("--output-file", default=str(DEFAULT_OUTPUT), help="Output JSON path.")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY), help="History NDJSON path.")
    parser.add_argument("--apply", action="store_true", help="Apply deterministic status updates to backlog.")
    parser.add_argument("--json", action="store_true", help="Print JSON payload.")
    parser.add_argument("--watch", action="store_true", help="Run continuously in foreground.")
    parser.add_argument("--interval-sec", type=int, default=300, help="Watch interval in seconds.")
    parser.add_argument("--daemon", action="store_true", help="Run as detached daemon.")
    parser.add_argument("--pid-file", default="", help="Daemon PID file path.")
    parser.add_argument("--log-file", default="", help="Daemon log file path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.daemon:
        return _spawn_daemon(args)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    if not bool(args.watch):
        return _run_once(args)

    interval_sec = max(30, int(args.interval_sec))
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
