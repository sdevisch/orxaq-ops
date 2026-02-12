#!/usr/bin/env python3
"""Autonomous guard that keeps local-model lanes active and self-healed."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.manager import (  # noqa: E402
    ManagerConfig,
    ensure_lanes_background,
    lane_status_snapshot,
    load_lane_specs,
    start_lane_background,
)
from orxaq_autonomy import runner as runner_module  # noqa: E402

DEFAULT_CONFIG = Path("config/local_model_idle_guard.json")
DEFAULT_REPORT = Path("artifacts/autonomy/local_models/idle_guard_report.json")
DEFAULT_STATE = Path("artifacts/autonomy/local_models/idle_guard_state.json")
DEFAULT_HISTORY = Path("artifacts/autonomy/local_models/idle_guard_history.ndjson")
DEFAULT_FLEET_STATUS = Path("artifacts/autonomy/local_models/fleet_status.json")
DEFAULT_PID = Path("artifacts/autonomy/local_models/idle_guard.pid")
DEFAULT_LOG = Path("artifacts/autonomy/local_models/idle_guard.log")

STOP = False


def _ensure_pythonpath_env() -> None:
    current = os.environ.get("PYTHONPATH", "")
    parts = [item for item in current.split(":") if item]
    src_path = str(SRC)
    if src_path not in parts:
        parts.insert(0, src_path)
    os.environ["PYTHONPATH"] = ":".join(parts)


def _sig_handler(_sig: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_ndjson(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def _to_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _to_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _to_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_endpoint_key(raw_url: str) -> str:
    text = str(raw_url).strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return ""
    port = parsed.port
    if port is None:
        port = 443 if str(parsed.scheme or "").strip().lower() == "https" else 80
    return f"{host}:{port}"


def _lane_endpoint_key(lane: dict[str, Any]) -> str:
    env = lane.get("env", {}) if isinstance(lane.get("env", {}), dict) else {}
    raw_urls: list[str] = []
    base_urls = str(env.get("ORXAQ_LOCAL_OPENAI_BASE_URLS", "")).strip()
    if base_urls:
        raw_urls.extend(item.strip() for item in base_urls.split(",") if item.strip())
    base_url = str(env.get("ORXAQ_LOCAL_OPENAI_BASE_URL", "")).strip()
    if base_url:
        raw_urls.append(base_url)
    for item in raw_urls:
        key = _normalize_endpoint_key(item)
        if key:
            return key
    return ""


def _is_local_lane(lane: dict[str, Any], *, require_local_only: bool) -> bool:
    env = lane.get("env", {}) if isinstance(lane.get("env", {}), dict) else {}
    has_local_endpoint = bool(str(env.get("ORXAQ_LOCAL_OPENAI_BASE_URLS", "")).strip() or str(env.get("ORXAQ_LOCAL_OPENAI_BASE_URL", "")).strip())
    if not has_local_endpoint:
        return False
    if not require_local_only:
        return True
    return _to_bool(env.get("ORXAQ_AUTONOMY_LOCAL_ONLY"), False)


def _load_config(path: Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "version": 1,
        "enabled": True,
        "interval_sec": 30,
        "fleet_probe_interval_sec": 120,
        "fleet_capability_scan_interval_sec": 600,
        "minimum_healthy_endpoints": 1,
        "auto_unpause_local_lanes": True,
        "stale_manual_pause_sec": 600,
        "max_auto_starts_per_cycle": 4,
        "allow_blocked_recovery": True,
        "require_local_only": True,
        "require_pending_work": True,
        "queue_depth_enables_start": True,
        "backlog_recycle_on_idle": True,
        "backlog_recycle_delay_sec": 45,
        "backlog_recycle_max_per_lane": 6,
        "idle_alert_threshold_cycles": 6,
        "revive_stalled_state_on_idle": True,
        "revive_after_idle_cycles": 2,
        "lane_id_allowlist": [],
        "lane_id_denylist": [],
        "report_file": str(DEFAULT_REPORT),
        "state_file": str(DEFAULT_STATE),
        "history_file": str(DEFAULT_HISTORY),
        "fleet_status_file": str(DEFAULT_FLEET_STATUS),
    }
    loaded = _read_json(path)
    merged = dict(defaults)
    for key, value in loaded.items():
        merged[key] = value
    return merged


def _revive_lane_state(lane: dict[str, Any]) -> dict[str, Any]:
    tasks_path = Path(str(lane.get("tasks_file", ""))).resolve()
    state_path = Path(str(lane.get("state_file", ""))).resolve()
    if not tasks_path.exists() or not state_path.exists():
        return {"changed": False, "reason": "missing_files", "tasks_file": str(tasks_path), "state_file": str(state_path)}
    try:
        tasks = runner_module.load_tasks(tasks_path)
        state = runner_module.load_state(state_path, tasks)
    except Exception as err:  # noqa: BLE001
        return {"changed": False, "reason": f"load_failed:{err}"}

    dep_path = Path(str(lane.get("dependency_state_file", ""))).resolve() if str(lane.get("dependency_state_file", "")).strip() else None
    dependency_state = runner_module.load_dependency_state(dep_path)
    ready = runner_module.select_next_task(tasks, state, dependency_state=dependency_state)
    if ready is not None:
        return {"changed": False, "reason": "already_ready", "task_id": ready.id}

    reopened: list[str] = []
    cleared_not_before: list[str] = []
    for task in tasks:
        entry = state.get(task.id, {})
        status = str(entry.get("status", runner_module.STATUS_PENDING))
        if status == runner_module.STATUS_BLOCKED:
            entry["status"] = runner_module.STATUS_PENDING
            entry["attempts"] = 0
            entry["retryable_failures"] = 0
            entry["deadlock_recoveries"] = 0
            entry["deadlock_reopens"] = 0
            entry["not_before"] = ""
            entry["last_update"] = _now_iso()
            entry["last_error"] = "idle_guard_reopened_blocked_task"
            reopened.append(task.id)
            continue
        if status == runner_module.STATUS_PENDING and str(entry.get("not_before", "")).strip():
            entry["not_before"] = ""
            entry["last_update"] = _now_iso()
            cleared_not_before.append(task.id)

    if reopened or cleared_not_before:
        try:
            runner_module.save_state(state_path, state)
        except Exception as err:  # noqa: BLE001
            return {"changed": False, "reason": f"save_failed:{err}", "reopened": reopened, "cleared_not_before": cleared_not_before}
        return {
            "changed": True,
            "reason": "stalled_state_revived",
            "lane_id": str(lane.get("id", "")).strip(),
            "reopened": reopened,
            "cleared_not_before": cleared_not_before,
        }
    return {"changed": False, "reason": "nothing_to_reopen"}


def _pause_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "paused.flag").resolve()


def _pause_metadata(config: ManagerConfig, lane_id: str) -> dict[str, Any]:
    path = _pause_file(config, lane_id)
    if not path.exists():
        return {"exists": False, "path": str(path), "manual": False, "age_sec": 0, "message": ""}
    message = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        mtime = path.stat().st_mtime
        age = max(0, int(time.time() - mtime))
    except Exception:
        age = 0
    return {
        "exists": True,
        "path": str(path),
        "manual": "manual" in message.lower() if message else True,
        "age_sec": age,
        "message": message,
    }


def _lane_work_score(status: dict[str, Any], *, allow_blocked_recovery: bool) -> tuple[bool, int, dict[str, int]]:
    counts = status.get("state_counts", {}) if isinstance(status.get("state_counts", {}), dict) else {}
    pending = max(0, _to_int(counts.get("pending", 0), 0))
    in_progress = max(0, _to_int(counts.get("in_progress", 0), 0))
    blocked = max(0, _to_int(counts.get("blocked", 0), 0))
    done = max(0, _to_int(counts.get("done", 0), 0))
    score = (pending * 100) + (in_progress * 50) + (blocked * 5)
    has_work = pending > 0 or in_progress > 0 or (allow_blocked_recovery and blocked > 0)
    detail = {"pending": pending, "in_progress": in_progress, "blocked": blocked, "done": done}
    return has_work, score, detail


def _decode_queue_items(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw_text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw_text:
        return []
    try:
        payload = json.loads(raw_text)
    except Exception:
        payload = None
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        tasks_payload = payload.get("tasks", [])
        if isinstance(tasks_payload, list):
            return [item for item in tasks_payload if isinstance(item, dict)]
        return [payload]
    out: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _queue_item_owner(item: dict[str, Any]) -> str:
    task_id = str(item.get("id") or item.get("task") or "").strip()
    owner = str(item.get("owner", "")).strip().lower()
    if owner:
        return owner
    inferred = task_id.split("-", 1)[0].strip().lower()
    if inferred in {"codex", "gemini", "claude"}:
        return inferred
    return "codex"


def _lane_queue_depth(lane: dict[str, Any]) -> dict[str, Any]:
    queue_file_raw = str(lane.get("task_queue_file", "")).strip()
    if not queue_file_raw:
        return {"pending": 0, "queue_file": "", "queue_state_file": ""}
    queue_file = Path(queue_file_raw).resolve()
    queue_state_raw = str(lane.get("task_queue_state_file", "")).strip()
    queue_state_file = Path(queue_state_raw).resolve() if queue_state_raw else None
    claimed = runner_module.load_task_queue_state(queue_state_file)
    claimed_ids = set(claimed)
    lane_owner = str(lane.get("owner", "")).strip().lower()
    pending = 0
    for item in _decode_queue_items(queue_file):
        task_id = str(item.get("id") or item.get("task") or "").strip()
        if not task_id or task_id in claimed_ids:
            continue
        item_owner = _queue_item_owner(item)
        if lane_owner and item_owner and item_owner != lane_owner:
            continue
        pending += 1
    return {
        "pending": pending,
        "queue_file": str(queue_file),
        "queue_state_file": str(queue_state_file) if queue_state_file is not None else "",
    }


def _recycle_backlog_tasks(lane: dict[str, Any], *, delay_sec: int, max_recycles: int) -> dict[str, Any]:
    tasks_path = Path(str(lane.get("tasks_file", ""))).resolve()
    state_path = Path(str(lane.get("state_file", ""))).resolve()
    if not tasks_path.exists() or not state_path.exists():
        return {"changed": False, "reason": "missing_files", "tasks_file": str(tasks_path), "state_file": str(state_path)}
    try:
        tasks = runner_module.load_tasks(tasks_path)
        state = runner_module.load_state(state_path, tasks)
    except Exception as err:  # noqa: BLE001
        return {"changed": False, "reason": f"load_failed:{err}"}

    dep_path = Path(str(lane.get("dependency_state_file", ""))).resolve() if str(lane.get("dependency_state_file", "")).strip() else None
    dependency_state = runner_module.load_dependency_state(dep_path)
    ready = runner_module.select_next_task(tasks, state, dependency_state=dependency_state)
    if ready is not None and not bool(getattr(ready, "backlog", False)):
        return {"changed": False, "reason": "live_work_already_ready", "task_id": ready.id}

    recycled: list[str] = []
    delay = max(0, int(delay_sec))
    not_before = ""
    if delay > 0:
        not_before = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)).isoformat()

    ordered_backlog = sorted(
        [task for task in tasks if bool(getattr(task, "backlog", False))],
        key=lambda task: (task.priority, task.id),
    )
    for task in ordered_backlog:
        if len(recycled) >= max(1, int(max_recycles)):
            break
        entry = state.get(task.id, {})
        status = str(entry.get("status", runner_module.STATUS_PENDING))
        if status not in {runner_module.STATUS_DONE, runner_module.STATUS_BLOCKED}:
            continue
        entry["status"] = runner_module.STATUS_PENDING
        entry["attempts"] = 0
        entry["retryable_failures"] = 0
        entry["deadlock_recoveries"] = 0
        entry["deadlock_reopens"] = 0
        entry["not_before"] = not_before
        entry["last_update"] = _now_iso()
        entry["last_error"] = "idle_guard_recycled_backlog_task"
        recycled.append(task.id)

    if not recycled:
        return {"changed": False, "reason": "no_recyclable_backlog"}
    try:
        runner_module.save_state(state_path, state)
    except Exception as err:  # noqa: BLE001
        return {"changed": False, "reason": f"save_failed:{err}", "recycled": recycled}
    return {
        "changed": True,
        "reason": "backlog_recycled",
        "lane_id": str(lane.get("id", "")).strip(),
        "recycled": recycled,
        "delay_sec": delay,
    }


def _healthy_endpoint_keys(fleet_payload: dict[str, Any]) -> set[str]:
    probe = fleet_payload.get("probe", {}) if isinstance(fleet_payload.get("probe", {}), dict) else {}
    endpoints = probe.get("endpoints", []) if isinstance(probe.get("endpoints", []), list) else []
    out: set[str] = set()
    for row in endpoints:
        if not isinstance(row, dict):
            continue
        if not bool(row.get("ok", False)):
            continue
        key = _normalize_endpoint_key(str(row.get("base_url", "")).strip())
        if key:
            out.add(key)
    return out


def _run_command(cmd: list[str], *, cwd: Path, timeout_sec: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=max(5, timeout_sec),
        )
        latency = time.monotonic() - started
        combined = "\n".join(part for part in [proc.stdout, proc.stderr] if part).strip()
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "latency_sec": round(latency, 3),
            "output": combined[-1600:],
            "command": cmd,
        }
    except subprocess.TimeoutExpired:
        latency = time.monotonic() - started
        return {
            "ok": False,
            "returncode": None,
            "latency_sec": round(latency, 3),
            "output": f"timeout after {timeout_sec}s",
            "command": cmd,
        }


def _maybe_refresh_fleet(*, repo_root: Path, cfg: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    last_probe = _to_float(state.get("last_probe_epoch", 0), 0.0)
    last_capability = _to_float(state.get("last_capability_epoch", 0), 0.0)
    probe_interval = max(30, _to_int(cfg.get("fleet_probe_interval_sec", 120), 120))
    capability_interval = max(60, _to_int(cfg.get("fleet_capability_scan_interval_sec", 600), 600))
    events: list[dict[str, Any]] = []

    if now - last_probe >= probe_interval:
        probe_result = _run_command([sys.executable, "scripts/local_model_fleet.py", "probe"], cwd=repo_root, timeout_sec=120)
        events.append({"event": "fleet_probe", **probe_result})
        if probe_result["ok"]:
            state["last_probe_epoch"] = now

    if now - last_capability >= capability_interval:
        cap_result = _run_command([sys.executable, "scripts/local_model_fleet.py", "capability-scan"], cwd=repo_root, timeout_sec=180)
        events.append({"event": "fleet_capability_scan", **cap_result})
        if cap_result["ok"]:
            state["last_capability_epoch"] = now

    fleet_status_file = Path(str(cfg.get("fleet_status_file", DEFAULT_FLEET_STATUS))).resolve()
    fleet_payload = _read_json(fleet_status_file)
    return {
        "fleet_status_file": str(fleet_status_file),
        "fleet": fleet_payload,
        "events": events,
    }


def run_cycle(*, repo_root: Path, config_path: Path, force_unpause: bool = False) -> dict[str, Any]:
    cfg = _load_config(config_path)
    state_file = Path(str(cfg.get("state_file", DEFAULT_STATE))).resolve()
    history_file = Path(str(cfg.get("history_file", DEFAULT_HISTORY))).resolve()
    report_file = Path(str(cfg.get("report_file", DEFAULT_REPORT))).resolve()

    state = _read_json(state_file)
    if not _to_bool(cfg.get("enabled", True), True):
        payload = {
            "timestamp": _now_iso(),
            "enabled": False,
            "report_file": str(report_file),
            "state_file": str(state_file),
            "history_file": str(history_file),
            "reason": "guard_disabled",
        }
        _write_json(report_file, payload)
        _append_ndjson(history_file, payload)
        return payload

    manager_cfg = ManagerConfig.from_root(repo_root)
    fleet_payload = _maybe_refresh_fleet(repo_root=repo_root, cfg=cfg, state=state)
    healthy_endpoint_keys = _healthy_endpoint_keys(fleet_payload["fleet"])

    lane_specs = load_lane_specs(manager_cfg)
    status_snapshot = lane_status_snapshot(manager_cfg)
    status_by_id = {
        str(item.get("id", "")).strip(): item
        for item in status_snapshot.get("lanes", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }

    allowlist = {str(item).strip() for item in cfg.get("lane_id_allowlist", []) if str(item).strip()}
    denylist = {str(item).strip() for item in cfg.get("lane_id_denylist", []) if str(item).strip()}
    require_local_only = _to_bool(cfg.get("require_local_only", True), True)
    local_specs = [
        lane
        for lane in lane_specs
        if lane.get("enabled", False)
        and _is_local_lane(lane, require_local_only=require_local_only)
        and (not allowlist or str(lane.get("id", "")).strip() in allowlist)
        and str(lane.get("id", "")).strip() not in denylist
    ]

    local_status_rows = [status_by_id.get(str(lane.get("id", "")).strip(), {}) for lane in local_specs]
    local_running_count = sum(1 for row in local_status_rows if bool(row.get("running", False)))
    previous_idle_cycles = max(0, _to_int(state.get("consecutive_idle_cycles", 0), 0))

    auto_unpause = _to_bool(cfg.get("auto_unpause_local_lanes", True), True)
    stale_manual_pause_sec = max(0, _to_int(cfg.get("stale_manual_pause_sec", 600), 600))
    require_pending_work = _to_bool(cfg.get("require_pending_work", True), True)
    allow_blocked_recovery = _to_bool(cfg.get("allow_blocked_recovery", True), True)
    max_starts = max(1, _to_int(cfg.get("max_auto_starts_per_cycle", 4), 4))
    minimum_healthy_endpoints = max(0, _to_int(cfg.get("minimum_healthy_endpoints", 1), 1))
    revive_stalled = _to_bool(cfg.get("revive_stalled_state_on_idle", True), True)
    revive_after_idle_cycles = max(0, _to_int(cfg.get("revive_after_idle_cycles", 2), 2))
    queue_depth_enables_start = _to_bool(cfg.get("queue_depth_enables_start", True), True)
    backlog_recycle_on_idle = _to_bool(cfg.get("backlog_recycle_on_idle", True), True)
    backlog_recycle_delay_sec = max(0, _to_int(cfg.get("backlog_recycle_delay_sec", 45), 45))
    backlog_recycle_max_per_lane = max(1, _to_int(cfg.get("backlog_recycle_max_per_lane", 6), 6))

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unpaused: list[dict[str, Any]] = []
    started: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    revived_states: list[dict[str, Any]] = []
    backlog_recycled: list[dict[str, Any]] = []

    if revive_stalled and local_running_count == 0 and previous_idle_cycles >= revive_after_idle_cycles:
        for lane in local_specs:
            lane_id = str(lane.get("id", "")).strip()
            row = status_by_id.get(lane_id, {}) if isinstance(status_by_id.get(lane_id, {}), dict) else {}
            if bool(row.get("running", False)):
                continue
            revive_payload = _revive_lane_state(lane)
            if revive_payload.get("changed", False):
                revived_states.append(revive_payload)
            elif str(revive_payload.get("reason", "")).startswith("save_failed"):
                failed.append({"id": lane_id, "source": "revive_state", "error": str(revive_payload.get("reason", ""))})

    if revived_states:
        status_snapshot = lane_status_snapshot(manager_cfg)
        status_by_id = {
            str(item.get("id", "")).strip(): item
            for item in status_snapshot.get("lanes", [])
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }

    for lane in local_specs:
        lane_id = str(lane.get("id", "")).strip()
        row = status_by_id.get(lane_id, {}) if isinstance(status_by_id.get(lane_id, {}), dict) else {}
        if bool(row.get("running", False)):
            continue

        endpoint_key = _lane_endpoint_key(lane)
        if healthy_endpoint_keys and endpoint_key and endpoint_key not in healthy_endpoint_keys:
            skipped.append({"id": lane_id, "reason": "endpoint_unhealthy", "endpoint_key": endpoint_key})
            continue

        pause = _pause_metadata(manager_cfg, lane_id)
        if pause["exists"]:
            stale_pause = pause["manual"] and pause["age_sec"] >= stale_manual_pause_sec
            if force_unpause or (auto_unpause and stale_pause):
                try:
                    Path(pause["path"]).unlink(missing_ok=True)
                    unpaused.append(
                        {
                            "id": lane_id,
                            "reason": "stale_manual_pause" if stale_pause else "force_unpause",
                            "pause_age_sec": pause["age_sec"],
                            "pause_message": pause["message"],
                        }
                    )
                except Exception as err:  # noqa: BLE001
                    failed.append({"id": lane_id, "source": "unpause", "error": str(err)})
                    continue
            else:
                skipped.append(
                    {
                        "id": lane_id,
                        "reason": "manual_pause_active",
                        "pause_age_sec": pause["age_sec"],
                        "pause_message": pause["message"],
                    }
                )
                continue

        has_work, score, work = _lane_work_score(row, allow_blocked_recovery=allow_blocked_recovery)
        queue_depth = _lane_queue_depth(lane)
        queue_pending = max(0, _to_int(queue_depth.get("pending", 0), 0))
        if queue_depth_enables_start and queue_pending > 0:
            has_work = True
            score += queue_pending * 120
            work["queued"] = queue_pending
            work["queue_file"] = str(queue_depth.get("queue_file", ""))

        if require_pending_work and not has_work:
            if backlog_recycle_on_idle:
                recycle_payload = _recycle_backlog_tasks(
                    lane,
                    delay_sec=backlog_recycle_delay_sec,
                    max_recycles=backlog_recycle_max_per_lane,
                )
                if recycle_payload.get("changed", False):
                    backlog_recycled.append(recycle_payload)
                    recycled_count = len(recycle_payload.get("recycled", []))
                    has_work = True
                    score += max(80, recycled_count * 80)
                    work["backlog_recycled"] = recycled_count
                    work["backlog_recycle_delay_sec"] = backlog_recycle_delay_sec
                elif str(recycle_payload.get("reason", "")).startswith("save_failed"):
                    failed.append({"id": lane_id, "source": "backlog_recycle", "error": str(recycle_payload.get("reason", ""))})
            if require_pending_work and not has_work:
                skipped.append({"id": lane_id, "reason": "no_runnable_work", "work": work})
                continue

        candidates.append(
            {
                "id": lane_id,
                "score": score,
                "endpoint_key": endpoint_key,
                "work": work,
                "queue_pending": queue_pending,
                "continuous": bool(lane.get("continuous", False)),
            }
        )

    if len(healthy_endpoint_keys) < minimum_healthy_endpoints:
        skipped.append(
            {
                "id": "*",
                "reason": "healthy_endpoint_floor_not_met",
                "healthy_endpoint_count": len(healthy_endpoint_keys),
                "minimum": minimum_healthy_endpoints,
            }
        )
    else:
        ordered = sorted(
            candidates,
            key=lambda item: (
                -_to_int(item.get("score", 0), 0),
                str(item.get("id", "")),
            ),
        )
        for candidate in ordered[:max_starts]:
            lane_id = str(candidate["id"])
            try:
                payload = start_lane_background(manager_cfg, lane_id)
                started.append(
                    {
                        "id": lane_id,
                        "pid": payload.get("pid"),
                        "endpoint_key": candidate.get("endpoint_key", ""),
                        "queue_pending": candidate.get("queue_pending", 0),
                        "work": candidate.get("work", {}),
                    }
                )
            except Exception as err:  # noqa: BLE001
                failed.append({"id": lane_id, "source": "start_lane", "error": str(err)})

    ensure_payload = ensure_lanes_background(manager_cfg)
    post_status = lane_status_snapshot(manager_cfg)
    post_by_id = {
        str(item.get("id", "")).strip(): item
        for item in post_status.get("lanes", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    local_running_after = sum(
        1
        for lane in local_specs
        if bool((post_by_id.get(str(lane.get("id", "")).strip(), {}) or {}).get("running", False))
    )

    consecutive_idle_cycles = previous_idle_cycles
    if local_running_after == 0 and len(healthy_endpoint_keys) >= minimum_healthy_endpoints:
        consecutive_idle_cycles += 1
    else:
        consecutive_idle_cycles = 0

    idle_alert_threshold = max(1, _to_int(cfg.get("idle_alert_threshold_cycles", 6), 6))
    anomalies: list[str] = []
    if local_running_after == 0 and len(healthy_endpoint_keys) >= minimum_healthy_endpoints:
        anomalies.append("idle_while_endpoints_healthy")
    if consecutive_idle_cycles >= idle_alert_threshold:
        anomalies.append("persistent_idle_condition")
    if failed:
        anomalies.append("self_healing_errors")
    if revived_states:
        anomalies.append("stalled_state_revived")
    if backlog_recycled:
        anomalies.append("backlog_recycled")

    state.update(
        {
            "last_cycle_at": _now_iso(),
            "last_probe_epoch": _to_float(state.get("last_probe_epoch", 0), 0.0),
            "last_capability_epoch": _to_float(state.get("last_capability_epoch", 0), 0.0),
            "consecutive_idle_cycles": consecutive_idle_cycles,
            "last_anomalies": anomalies,
            "last_started_lane_ids": [item["id"] for item in started],
            "last_failed_lane_ids": [item["id"] for item in failed],
            "last_backlog_recycled_lane_ids": [str(item.get("lane_id", "")).strip() for item in backlog_recycled],
        }
    )

    report = {
        "timestamp": _now_iso(),
        "ok": len(failed) == 0,
        "repo_root": str(repo_root),
        "config_file": str(config_path),
        "state_file": str(state_file),
        "history_file": str(history_file),
        "fleet_status_file": fleet_payload["fleet_status_file"],
        "local_lane_total": len(local_specs),
        "local_running_before": local_running_count,
        "local_running_after": local_running_after,
        "healthy_endpoint_count": len(healthy_endpoint_keys),
        "healthy_endpoint_keys": sorted(healthy_endpoint_keys),
        "candidate_count": len(candidates),
        "started_count": len(started),
        "unpaused_count": len(unpaused),
        "revived_state_count": len(revived_states),
        "backlog_recycled_count": len(backlog_recycled),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "consecutive_idle_cycles": consecutive_idle_cycles,
        "anomalies": anomalies,
        "started": started,
        "unpaused": unpaused,
        "skipped": skipped,
        "failed": failed,
        "revived_states": revived_states,
        "backlog_recycled": backlog_recycled,
        "fleet_refresh_events": fleet_payload["events"],
        "ensure": {
            "ok": bool(ensure_payload.get("ok", False)),
            "started_count": _to_int(ensure_payload.get("started_count", 0), 0),
            "restarted_count": _to_int(ensure_payload.get("restarted_count", 0), 0),
            "failed_count": _to_int(ensure_payload.get("failed_count", 0), 0),
            "skipped_count": _to_int(ensure_payload.get("skipped_count", 0), 0),
        },
        "recommendations": [
            "Unpause local-only lanes only via stale/manual policy and keep everything else untouched.",
            "Keep `local_model_fleet.py probe` frequent and capability-scan less frequent for cost/speed balance.",
            "If persistent idle remains > threshold, add or expand local backlog tasks in local-only lanes.",
            "Keep per-lane queue files populated so idle lanes can start on queued work even before direct prompts.",
        ],
    }

    _write_json(state_file, state)
    _write_json(report_file, report)
    _append_ndjson(history_file, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guard local-model lanes against idle drift")
    parser.add_argument("--root", default=str(ROOT), help="Repository root (default: script parent repo)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Idle-guard config JSON")
    parser.add_argument("--interval-sec", type=int, default=0, help="Override cycle interval")
    parser.add_argument("--once", action="store_true", help="Run exactly one cycle")
    parser.add_argument("--json", action="store_true", help="Emit cycle report JSON to stdout")
    parser.add_argument("--force-unpause", action="store_true", help="Force-clear manual pause for eligible local lanes")
    parser.add_argument("--daemon", action="store_true", help="Run guard in detached mode")
    parser.add_argument("--pid-file", default="", help="PID file path for --daemon")
    parser.add_argument("--log-file", default="", help="Log file path for --daemon")
    return parser


def main() -> int:
    args = _parser().parse_args()
    repo_root = Path(str(args.root)).resolve()
    config_path = Path(str(args.config)).resolve()
    _ensure_pythonpath_env()

    if args.daemon:
        pid_file = Path(str(args.pid_file).strip()).resolve() if str(args.pid_file).strip() else DEFAULT_PID.resolve()
        log_file = Path(str(args.log_file).strip()).resolve() if str(args.log_file).strip() else DEFAULT_LOG.resolve()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        child_args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--root",
            str(repo_root),
            "--config",
            str(config_path),
        ]
        if args.interval_sec > 0:
            child_args.extend(["--interval-sec", str(max(5, int(args.interval_sec)))])
        if args.force_unpause:
            child_args.append("--force-unpause")
        if args.json:
            child_args.append("--json")
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

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    cfg = _load_config(config_path)
    interval_sec = max(5, _to_int(cfg.get("interval_sec", 30), 30))
    if args.interval_sec > 0:
        interval_sec = max(5, int(args.interval_sec))

    cycle = 0
    exit_code = 0
    while not STOP:
        cycle += 1
        report = run_cycle(
            repo_root=repo_root,
            config_path=config_path,
            force_unpause=bool(args.force_unpause),
        )
        report["cycle"] = cycle
        if args.json:
            print(json.dumps(report, sort_keys=True), flush=True)
        if report.get("failed_count", 0):
            exit_code = 1
        if args.once:
            break
        for _ in range(interval_sec):
            if STOP:
                break
            time.sleep(1)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
