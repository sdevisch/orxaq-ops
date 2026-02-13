#!/usr/bin/env python3
"""Recurring watchdog for autonomous process health and restart."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".claude" / "watchdogs" / "process-watchdog.json"
DEFAULT_STATE_PATH = Path.home() / ".claude" / "watchdogs" / "process-watchdog-state.json"
DEFAULT_HISTORY_PATH = Path.home() / ".claude" / "watchdogs" / "process-watchdog-history.ndjson"

PROBLEM_STATUSES = {"restart_failed", "down_cooldown", "down_no_restart"}
MAX_CAPTURE_CHARS = 2000

# Issue #67: Patterns for sensitive data that must never appear in process output or logs
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bAGE-SECRET-KEY-[A-Za-z0-9]+\b"), "[REDACTED_AGE_SECRET_KEY]"),
    (re.compile(r"\bage1[a-z0-9]{56,}\b"), "[REDACTED_AGE_PUBLIC_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"), r"\1=****"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{20,}\b"), "Bearer ****"),
]


def _redact_secrets(text: str) -> str:
    """Redact sensitive key material from text before logging or output."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tail(text: str, limit: int = MAX_CAPTURE_CHARS) -> str:
    value = (text or "").strip()
    # Issue #67: Always redact secrets from captured process output
    value = _redact_secrets(value)
    if len(value) <= limit:
        return value
    return value[-limit:]


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"Invalid JSON at {path}: {err}") from err


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _append_ndjson(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_pid(pid_file: str | None) -> int | None:
    if not pid_file:
        return None
    path = Path(pid_file).expanduser()
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    pid = _to_int(raw, default=-1)
    return pid if pid > 0 else None


def _pid_running(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user.
        return True


def _pid_command(pid: int | None) -> str:
    if pid is None or pid <= 0:
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    return (proc.stdout or "").strip()


def _command_matches(pid: int | None, pattern: str | None) -> bool:
    if not pattern:
        return True
    cmd = _pid_command(pid)
    if not cmd:
        return False
    try:
        return bool(re.search(pattern, cmd))
    except re.error:
        return False


def _normalize_command(command: Any, field_name: str) -> tuple[list[str] | str, bool]:
    if isinstance(command, list):
        if not command:
            raise ValueError(f"{field_name} cannot be an empty list")
        if not all(isinstance(item, str) and item.strip() for item in command):
            raise ValueError(f"{field_name} list entries must be non-empty strings")
        return command, False
    if isinstance(command, str) and command.strip():
        return command.strip(), True
    raise ValueError(f"{field_name} must be a non-empty string or string list")


def _merge_env(spec: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    extra_env = spec.get("env")
    if isinstance(extra_env, dict):
        for key, value in extra_env.items():
            if isinstance(key, str):
                env[key] = str(value)
    return env


def _resolve_pid(spec: dict[str, Any], process_state: dict[str, Any]) -> int | None:
    pid = _read_pid(spec.get("pid_file"))
    if pid:
        return pid
    configured_pid = _to_int(spec.get("pid"), default=-1)
    if configured_pid > 0:
        return configured_pid
    remembered = _to_int(process_state.get("last_pid"), default=-1)
    if remembered > 0:
        return remembered
    return None


def _run_check(spec: dict[str, Any], env: dict[str, str]) -> tuple[bool, str]:
    if "check_command" not in spec:
        return True, ""
    try:
        command, shell = _normalize_command(spec.get("check_command"), "check_command")
    except ValueError as err:
        return False, str(err)

    timeout_sec = max(_to_int(spec.get("check_timeout_sec"), default=15), 1)
    cwd = spec.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return False, "cwd must be a string when provided"

    try:
        proc = subprocess.run(
            command,
            shell=shell,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"check_command timed out after {timeout_sec}s"
    except OSError as err:
        return False, f"check_command failed to start: {err}"

    combined = "\n".join([part for part in [proc.stdout, proc.stderr] if part])
    output = _tail(combined)
    if proc.returncode == 0:
        return True, output
    reason = f"check_command exited with {proc.returncode}"
    if output:
        reason = f"{reason}; {output}"
    return False, reason


def _run_restart(spec: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    command_raw = spec.get("restart_command")
    if command_raw is None:
        return {
            "attempted": False,
            "ok": False,
            "returncode": None,
            "spawned_pid": None,
            "output": "restart_command is not configured",
        }

    try:
        command, shell = _normalize_command(command_raw, "restart_command")
    except ValueError as err:
        return {
            "attempted": True,
            "ok": False,
            "returncode": None,
            "spawned_pid": None,
            "output": str(err),
        }

    timeout_sec = max(_to_int(spec.get("restart_timeout_sec"), default=120), 1)
    detached = bool(spec.get("restart_detached", False))
    cwd = spec.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return {
            "attempted": True,
            "ok": False,
            "returncode": None,
            "spawned_pid": None,
            "output": "cwd must be a string when provided",
        }

    try:
        if detached:
            proc = subprocess.Popen(
                command,
                shell=shell,
                cwd=cwd,
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {
                "attempted": True,
                "ok": True,
                "returncode": 0,
                "spawned_pid": proc.pid,
                "output": f"spawned detached process pid={proc.pid}",
            }

        proc = subprocess.run(
            command,
            shell=shell,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "attempted": True,
            "ok": False,
            "returncode": None,
            "spawned_pid": None,
            "output": f"restart_command timed out after {timeout_sec}s",
        }
    except OSError as err:
        return {
            "attempted": True,
            "ok": False,
            "returncode": None,
            "spawned_pid": None,
            "output": f"restart_command failed to start: {err}",
        }

    combined = "\n".join([part for part in [proc.stdout, proc.stderr] if part])
    return {
        "attempted": True,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "spawned_pid": None,
        "output": _tail(combined),
    }


def _load_config(config_path: Path) -> dict[str, Any]:
    config = _load_json(config_path, default=None)
    if config is None:
        raise ValueError(f"Config file not found: {config_path}")
    if not isinstance(config, dict):
        raise ValueError("Config root must be a JSON object")
    processes = config.get("processes")
    if not isinstance(processes, list):
        raise ValueError("Config must contain a 'processes' array")
    seen: set[str] = set()
    for entry in processes:
        if not isinstance(entry, dict):
            raise ValueError("Each process entry must be an object")
        process_id = entry.get("id")
        if not isinstance(process_id, str) or not process_id.strip():
            raise ValueError("Each process entry requires a non-empty string 'id'")
        if process_id in seen:
            raise ValueError(f"Duplicate process id: {process_id}")
        seen.add(process_id)
    return config


def _default_state() -> dict[str, Any]:
    return {"runs_total": 0, "last_run_at": None, "processes": {}}


def run_watchdog(config: dict[str, Any], state_path: Path, history_path: Path, json_output: bool = False) -> int:
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}

    if not config["processes"]:
        print("No processes configured. Nothing to do.")
        return 0

    loaded_state = _load_json(state_path, default=_default_state())
    if not isinstance(loaded_state, dict):
        loaded_state = _default_state()
    process_state_map = loaded_state.get("processes")
    if not isinstance(process_state_map, dict):
        process_state_map = {}
        loaded_state["processes"] = process_state_map
    # Drop entries for processes that are no longer configured so downstream health
    # checks (which read the state file directly) don't report stale processes as unhealthy.
    configured_ids = {spec.get("id") for spec in config.get("processes", []) if isinstance(spec, dict)}
    configured_ids = {pid for pid in configured_ids if isinstance(pid, str) and pid.strip()}
    for stale_id in list(process_state_map.keys()):
        if stale_id not in configured_ids:
            process_state_map.pop(stale_id, None)

    default_cooldown = max(_to_int(defaults.get("restart_cooldown_sec"), default=60), 0)
    default_grace = max(_to_int(defaults.get("startup_grace_sec"), default=3), 0)

    now_iso = _now_iso()
    now_ts = int(time.time())
    summary: list[dict[str, Any]] = []
    problem_count = 0

    for spec in config["processes"]:
        process_id = spec["id"]
        current_state = process_state_map.get(process_id)
        if not isinstance(current_state, dict):
            current_state = {}
            process_state_map[process_id] = current_state

        env = _merge_env(spec)
        pid = _resolve_pid(spec, current_state)

        status = "healthy"
        reason_parts: list[str] = []
        restart_result: dict[str, Any] | None = None

        pid_alive = _pid_running(pid)
        if not pid_alive:
            reason_parts.append("pid not running")

        if pid_alive and not _command_matches(pid, spec.get("match_regex")):
            pid_alive = False
            reason_parts.append("match_regex did not match process command")

        check_ok = True
        check_detail = ""
        if "check_command" in spec:
            check_ok, check_detail = _run_check(spec, env)
            if not check_ok:
                reason_parts.append(check_detail or "check_command failed")

        # Some services (for example dashboards) are best verified by an external check
        # (HTTP probe) even when we can't reliably map them to a PID in this environment.
        alive = pid_alive or ("check_command" in spec and check_ok)

        if alive and check_ok:
            status = "healthy"
            # If the health probe passed, stale PID reasons are just noise.
            if "check_command" in spec and check_ok:
                reason_parts = []
        else:
            cooldown_sec = max(_to_int(spec.get("restart_cooldown_sec"), default_cooldown), 0)
            last_restart_ts = _to_int(current_state.get("last_restart_ts"), default=0)
            seconds_since_restart = now_ts - last_restart_ts if last_restart_ts > 0 else None
            if spec.get("restart_command") is None:
                status = "down_no_restart"
                reason_parts.append("restart_command missing")
            elif seconds_since_restart is not None and seconds_since_restart < cooldown_sec:
                status = "down_cooldown"
                reason_parts.append(
                    f"restart cooldown active ({cooldown_sec - seconds_since_restart}s remaining)"
                )
            else:
                restart_result = _run_restart(spec, env)
                if restart_result.get("attempted"):
                    current_state["restart_attempts"] = _to_int(current_state.get("restart_attempts"), 0) + 1
                    current_state["last_restart_at"] = now_iso
                    current_state["last_restart_ts"] = now_ts
                    current_state["last_restart_rc"] = restart_result.get("returncode")

                if restart_result.get("output"):
                    reason_parts.append(str(restart_result["output"]))

                grace_sec = max(_to_int(spec.get("startup_grace_sec"), default_grace), 0)
                if grace_sec > 0:
                    time.sleep(grace_sec)

                pid = _resolve_pid(spec, current_state)
                spawned_pid = _to_int(restart_result.get("spawned_pid"), default=-1)
                if pid is None and spawned_pid > 0:
                    pid = spawned_pid

                post_pid_alive = _pid_running(pid)
                if post_pid_alive and not _command_matches(pid, spec.get("match_regex")):
                    post_pid_alive = False
                    reason_parts.append("post-restart match_regex did not match process command")

                post_check_ok = check_ok
                post_check_detail = ""
                if "check_command" in spec:
                    post_check_ok, post_check_detail = _run_check(spec, env)
                    if not post_check_ok:
                        reason_parts.append(post_check_detail or "post-restart check_command failed")

                post_alive = post_pid_alive or ("check_command" in spec and post_check_ok)

                if restart_result.get("ok") and post_alive and post_check_ok:
                    status = "restarted"
                    current_state["restart_successes"] = _to_int(current_state.get("restart_successes"), 0) + 1
                else:
                    status = "restart_failed"
                    current_state["restart_failures"] = _to_int(current_state.get("restart_failures"), 0) + 1

        current_state["checks_total"] = _to_int(current_state.get("checks_total"), 0) + 1
        current_state["last_status"] = status
        current_state["last_checked_at"] = now_iso
        current_state["last_pid"] = pid
        current_state["last_reason"] = _tail("; ".join([part for part in reason_parts if part]))

        if status == "healthy":
            current_state["healthy_checks"] = _to_int(current_state.get("healthy_checks"), 0) + 1
        else:
            current_state["unhealthy_checks"] = _to_int(current_state.get("unhealthy_checks"), 0) + 1
            if status in PROBLEM_STATUSES:
                problem_count += 1

        event = {
            "time": now_iso,
            "id": process_id,
            "status": status,
            "pid": pid,
            "reason": current_state["last_reason"],
            "restart_returncode": current_state.get("last_restart_rc"),
        }
        _append_ndjson(history_path, event)

        summary.append(
            {
                "id": process_id,
                "pid": pid,
                "status": status,
                "reason": current_state["last_reason"],
            }
        )

    loaded_state["runs_total"] = _to_int(loaded_state.get("runs_total"), default=0) + 1
    loaded_state["last_run_at"] = now_iso
    loaded_state["processes"] = process_state_map
    _write_json(state_path, loaded_state)

    if json_output:
        print(
            json.dumps(
                {
                    "time": now_iso,
                    "state_file": str(state_path),
                    "history_file": str(history_path),
                    "problem_count": problem_count,
                    "processes": summary,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"[{now_iso}] Checked {len(summary)} process(es)")
        for item in summary:
            reason = f" | {item['reason']}" if item["reason"] else ""
            print(f"- {item['id']}: status={item['status']} pid={item['pid']}{reason}")
        print(f"State: {state_path}")
        print(f"History: {history_path}")

    return 2 if problem_count > 0 else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Watchdog config JSON path")
    parser.add_argument("--state-file", default="", help="Override state JSON output path")
    parser.add_argument("--history-file", default="", help="Override history NDJSON output path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    try:
        config = _load_config(config_path)
    except ValueError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1

    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else Path(config.get("state_file", DEFAULT_STATE_PATH)).expanduser().resolve()
    )
    history_path = (
        Path(args.history_file).expanduser().resolve()
        if args.history_file
        else Path(config.get("history_file", DEFAULT_HISTORY_PATH)).expanduser().resolve()
    )

    return run_watchdog(config=config, state_path=state_path, history_path=history_path, json_output=args.json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
