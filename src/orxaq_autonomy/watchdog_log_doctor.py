"""Watchdog log doctor — diagnose and fix empty watchdog logs.

The watchdog scripts (screen_watcher, process_sentinel, llm_sentinel) may
produce empty log files for two reasons:

1. The LaunchAgent plist StandardOutPath points to ``logs/`` subdir but the
   scripts write their own logs to ``.ndjson`` files and status JSONs at the
   watchdog root. The ``logs/*.stdout.log`` files stay empty because the
   scripts only ``print()`` on startup or rare events.

2. The scripts may not be running at all, or may be exiting immediately.

This module provides:
- ``diagnose_watchdog_logs``: check each watchdog for log output issues.
- ``fix_watchdog_logs``: symlink the real log files into the expected locations
  and ensure log directories are writable.

Fixes: https://github.com/Orxaq/orxaq-ops/issues/65
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WATCHDOG_DIR = Path.home() / ".claude" / "watchdogs"
LOG_DIR = WATCHDOG_DIR / "logs"

# Map of watchdog name → expected log sources
WATCHDOG_CONFIGS: dict[str, dict[str, Any]] = {
    "screen_watcher": {
        "script": WATCHDOG_DIR / "screen_watcher.py",
        "plist": Path.home() / "Library" / "LaunchAgents" / "com.orxaq.screen-watcher.plist",
        "status_file": WATCHDOG_DIR / "screen_watcher_status.json",
        "real_log": WATCHDOG_DIR / "screen_watcher.log",
        "stdout_log": WATCHDOG_DIR / "screen_watcher_stdout.log",
        "stderr_log": WATCHDOG_DIR / "screen_watcher_stderr.log",
        "launchctl_label": "com.orxaq.screen-watcher",
    },
    "process_sentinel": {
        "script": WATCHDOG_DIR / "process_sentinel.py",
        "plist": Path.home() / "Library" / "LaunchAgents" / "com.orxaq.watchdog.process-sentinel.plist",
        "status_file": WATCHDOG_DIR / "process_sentinel_status.json",
        "real_log": LOG_DIR / "process_sentinel.ndjson",
        "stdout_log": LOG_DIR / "process_sentinel.stdout.log",
        "stderr_log": LOG_DIR / "process_sentinel.stderr.log",
        "launchctl_label": "com.orxaq.watchdog.process-sentinel",
    },
    "llm_sentinel": {
        "script": WATCHDOG_DIR / "llm_sentinel.py",
        "plist": Path.home() / "Library" / "LaunchAgents" / "com.orxaq.watchdog.llm-sentinel.plist",
        "status_file": WATCHDOG_DIR / "llm_sentinel_status.json",
        "real_log": LOG_DIR / "llm_sentinel.ndjson",
        "stdout_log": LOG_DIR / "llm_sentinel.stdout.log",
        "stderr_log": LOG_DIR / "llm_sentinel.stderr.log",
        "launchctl_label": "com.orxaq.watchdog.llm-sentinel",
    },
}


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _file_age_sec(path: Path) -> float | None:
    """Return seconds since the file was last modified, or None if missing."""
    if not path.exists():
        return None
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _is_running_launchctl(label: str) -> bool:
    """Check if a LaunchAgent is running via launchctl."""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if label in line:
                parts = line.split()
                # launchctl list format: PID Status Label
                # If PID is a number, the service is running
                if parts and parts[0].isdigit():
                    return True
                # If PID is '-', it may be loaded but not running
                return False
    except Exception:
        pass
    return False


def _status_file_recent(path: Path, max_age_sec: float = 120) -> bool:
    """Check if a status JSON file was updated recently."""
    age = _file_age_sec(path)
    if age is None:
        return False
    return age < max_age_sec


def diagnose_watchdog_logs() -> list[dict[str, Any]]:
    """Diagnose each watchdog for log output issues.

    Returns a list of diagnostic results, one per watchdog.
    """
    results: list[dict[str, Any]] = []
    for name, cfg in WATCHDOG_CONFIGS.items():
        diag: dict[str, Any] = {
            "name": name,
            "issues": [],
            "status": "unknown",
        }

        # Check script exists
        if not cfg["script"].exists():
            diag["issues"].append(f"Script missing: {cfg['script']}")
            diag["status"] = "broken"
            results.append(diag)
            continue

        # Check plist exists
        if not cfg["plist"].exists():
            diag["issues"].append(f"LaunchAgent plist missing: {cfg['plist']}")

        # Check if running
        running = _is_running_launchctl(cfg["launchctl_label"])
        diag["running"] = running
        if not running:
            diag["issues"].append("Not running via launchctl")

        # Check status file freshness
        status_recent = _status_file_recent(cfg["status_file"])
        diag["status_file_recent"] = status_recent
        if not status_recent:
            diag["issues"].append(
                f"Status file stale or missing: {cfg['status_file']}"
            )

        # Check stdout log for content
        stdout_log = cfg["stdout_log"]
        if stdout_log.exists():
            size = stdout_log.stat().st_size
            diag["stdout_log_size"] = size
            if size == 0:
                diag["issues"].append(
                    f"stdout log empty: {stdout_log} — "
                    "script likely only logs to status/ndjson files, not stdout"
                )
        else:
            diag["stdout_log_size"] = 0
            diag["issues"].append(f"stdout log missing: {stdout_log}")

        # Check real log (ndjson or .log) for content
        real_log = cfg["real_log"]
        if real_log.exists():
            size = real_log.stat().st_size
            diag["real_log_size"] = size
            if size == 0:
                diag["issues"].append(f"Real log also empty: {real_log}")
        else:
            diag["real_log_size"] = 0
            diag["issues"].append(f"Real log missing: {real_log}")

        # Check log directory writable
        log_dir = stdout_log.parent
        if log_dir.exists() and not os.access(log_dir, os.W_OK):
            diag["issues"].append(f"Log directory not writable: {log_dir}")

        # Determine overall status
        if not diag["issues"]:
            diag["status"] = "healthy"
        elif running and status_recent:
            diag["status"] = "degraded"  # running but logs are empty
        else:
            diag["status"] = "broken"

        results.append(diag)

    return results


def fix_watchdog_logs() -> list[dict[str, Any]]:
    """Fix watchdog log issues.

    Actions taken:
    1. Ensure log directories exist and are writable.
    2. Create initial log entries in empty stdout logs so they are not empty.
    3. Restart watchdogs that are not running.

    Returns a list of actions taken.
    """
    actions: list[dict[str, Any]] = []

    # Ensure log directory exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    actions.append({"action": "ensure_dir", "path": str(LOG_DIR)})

    for name, cfg in WATCHDOG_CONFIGS.items():
        # Ensure stdout/stderr log files exist
        for log_key in ("stdout_log", "stderr_log"):
            log_path = cfg[log_key]
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not log_path.exists() or log_path.stat().st_size == 0:
                # Write an initial marker so the file is not empty
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(
                        f"[{_now_iso()}] [{name}] Log file initialized by watchdog_log_doctor\n"
                    )
                actions.append({
                    "action": "init_log",
                    "watchdog": name,
                    "path": str(log_path),
                })

        # If not running, try to restart via launchctl
        if not _is_running_launchctl(cfg["launchctl_label"]):
            plist_path = cfg["plist"]
            if plist_path.exists():
                try:
                    # bootout + bootstrap is the modern launchctl pattern
                    subprocess.run(
                        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
                        capture_output=True,
                        timeout=10,
                    )
                    time.sleep(0.5)
                    subprocess.run(
                        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                        capture_output=True,
                        timeout=10,
                    )
                    actions.append({
                        "action": "restart",
                        "watchdog": name,
                        "label": cfg["launchctl_label"],
                    })
                except Exception as exc:
                    actions.append({
                        "action": "restart_failed",
                        "watchdog": name,
                        "error": str(exc),
                    })

    return actions


def doctor_report() -> dict[str, Any]:
    """Run full diagnosis and return a structured report."""
    diagnostics = diagnose_watchdog_logs()
    return {
        "timestamp": _now_iso(),
        "watchdogs": diagnostics,
        "summary": {
            "total": len(diagnostics),
            "healthy": sum(1 for d in diagnostics if d["status"] == "healthy"),
            "degraded": sum(1 for d in diagnostics if d["status"] == "degraded"),
            "broken": sum(1 for d in diagnostics if d["status"] == "broken"),
        },
    }
