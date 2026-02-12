#!/usr/bin/env python3
"""Daemon wrapper for recurring autonomous process watchdog passes."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_WATCHDOG_SCRIPT = Path("/Users/sdevisch/.codex/skills/autonomous-process-watchdog/scripts/process_watchdog.py")
DEFAULT_CONFIG = Path("config/local_model_process_watchdog.json")
DEFAULT_STATE = Path("artifacts/autonomy/local_models/process_watchdog_state.json")
DEFAULT_HISTORY = Path("artifacts/autonomy/local_models/process_watchdog_history.ndjson")
DEFAULT_PID = Path("artifacts/autonomy/local_models/process_watchdog.pid")
DEFAULT_LOG = Path("artifacts/autonomy/local_models/process_watchdog.log")

STOP = False


def _sig_handler(_sig: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run_watchdog(
    *,
    watchdog_script: Path,
    config: Path,
    state_file: Path,
    history_file: Path,
    root: Path,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(watchdog_script.resolve()),
        "--config",
        str(config.resolve()),
        "--state-file",
        str(state_file.resolve()),
        "--history-file",
        str(history_file.resolve()),
        "--json",
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root.resolve()),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        latency_sec = round(max(0.0, time.monotonic() - started), 3)
        raw_out = (proc.stdout or "").strip()
        raw_err = (proc.stderr or "").strip()
        parsed: dict[str, Any] = {}
        if raw_out:
            try:
                parsed = json.loads(raw_out)
            except Exception:
                parsed = {}
        return {
            "timestamp": _now_iso(),
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "latency_sec": latency_sec,
            "stdout": raw_out[-3000:],
            "stderr": raw_err[-1000:],
            "summary": parsed if isinstance(parsed, dict) else {},
        }
    except subprocess.TimeoutExpired:
        latency_sec = round(max(0.0, time.monotonic() - started), 3)
        return {
            "timestamp": _now_iso(),
            "ok": False,
            "returncode": None,
            "latency_sec": latency_sec,
            "stdout": "",
            "stderr": "watchdog pass timed out after 120s",
            "summary": {},
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local-model process watchdog repeatedly")
    parser.add_argument("--root", default=".")
    parser.add_argument("--watchdog-script", default=str(DEFAULT_WATCHDOG_SCRIPT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--state-file", default=str(DEFAULT_STATE))
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY))
    parser.add_argument("--interval-sec", type=int, default=20)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--pid-file", default="")
    parser.add_argument("--log-file", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = Path(str(args.root)).resolve()
    watchdog_script = Path(str(args.watchdog_script)).resolve()
    config = Path(str(args.config)).resolve()
    state_file = Path(str(args.state_file)).resolve()
    history_file = Path(str(args.history_file)).resolve()

    if args.daemon:
        pid_file = Path(str(args.pid_file).strip()).resolve() if str(args.pid_file).strip() else DEFAULT_PID.resolve()
        log_file = Path(str(args.log_file).strip()).resolve() if str(args.log_file).strip() else DEFAULT_LOG.resolve()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        child_args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--root",
            str(root),
            "--watchdog-script",
            str(watchdog_script),
            "--config",
            str(config),
            "--state-file",
            str(state_file),
            "--history-file",
            str(history_file),
            "--interval-sec",
            str(max(5, int(args.interval_sec))),
            "--json",
        ]
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
    interval_sec = max(5, int(args.interval_sec))

    exit_code = 0
    while not STOP:
        report = _run_watchdog(
            watchdog_script=watchdog_script,
            config=config,
            state_file=state_file,
            history_file=history_file,
            root=root,
        )
        if args.json:
            print(json.dumps(report, sort_keys=True), flush=True)
        if not report.get("ok", False):
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
