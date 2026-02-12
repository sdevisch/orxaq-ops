#!/usr/bin/env python3
"""Run swarm todo health against active/recent worktrees only."""

from __future__ import annotations

import argparse
import glob
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_TODO_GLOBS = (
    "{root_parent}/orxaq/ops/backlog/distributed_todo.yaml",
    "{root_parent}/.worktrees/*/ops/backlog/distributed_todo.yaml",
)
DEFAULT_OUTPUT = Path("artifacts/autonomy/swarm_todo_health/current_latest.json")
DEFAULT_HISTORY = Path("artifacts/autonomy/swarm_todo_health/current_history.ndjson")
DEFAULT_PID = Path("artifacts/autonomy/swarm_todo_health/current_health.pid")
DEFAULT_LOG = Path("artifacts/autonomy/swarm_todo_health/current_health.log")

STOP = False


def _sig_handler(_sig: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _to_path(raw: str | Path) -> Path:
    return Path(str(raw)).expanduser().resolve()


def _resolve_patterns(root: Path, patterns: Iterable[str]) -> list[str]:
    root_parent = root.parent.resolve()
    out: list[str] = []
    for raw in patterns:
        text = str(raw).strip()
        if not text:
            continue
        out.append(text.format(root=str(root), root_parent=str(root_parent)))
    return out


def _discover_by_glob(root: Path, patterns: Iterable[str]) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in _resolve_patterns(root, patterns):
        for item in sorted(glob.glob(pattern)):
            path = _to_path(item)
            if path.exists() and path.is_file():
                found[str(path)] = path
    return sorted(found.values(), key=lambda p: str(p))


def _discover_from_worktrees(root: Path) -> list[Path]:
    root_parent = root.parent.resolve()
    orxaq_repo = root_parent / "orxaq"
    if not (orxaq_repo / ".git").exists():
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(orxaq_repo), "worktree", "list", "--porcelain"],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.SubprocessError:
        return []
    out: dict[str, Path] = {}
    for line in proc.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        wt = _to_path(line.removeprefix("worktree ").strip())
        todo = wt / "ops" / "backlog" / "distributed_todo.yaml"
        if todo.exists() and todo.is_file():
            out[str(todo)] = todo
    return sorted(out.values(), key=lambda p: str(p))


def _is_recent(path: Path, *, max_age_sec: int, now_ts: float) -> bool:
    try:
        age = int(now_ts - path.stat().st_mtime)
    except OSError:
        return False
    return age <= max_age_sec


def _target_todo_files(root: Path, *, max_age_sec: int) -> list[Path]:
    now_ts = time.time()
    root_parent = root.parent.resolve()
    main_todo = root_parent / "orxaq" / "ops" / "backlog" / "distributed_todo.yaml"

    candidates: dict[str, Path] = {}
    for path in _discover_by_glob(root, DEFAULT_TODO_GLOBS):
        candidates[str(path)] = path
    for path in _discover_from_worktrees(root):
        candidates[str(path)] = path

    selected: dict[str, Path] = {}
    for key, path in candidates.items():
        if _is_recent(path, max_age_sec=max_age_sec, now_ts=now_ts):
            selected[key] = path

    # Always include canonical main backlog when available.
    if main_todo.exists() and main_todo.is_file():
        selected[str(main_todo.resolve())] = main_todo.resolve()

    return sorted(selected.values(), key=lambda p: str(p))


def _build_checker_cmd(args: argparse.Namespace, *, root: Path, todo_files: list[Path]) -> list[str]:
    checker = root / "scripts" / "swarm_distributed_todo_health.py"
    cmd = [
        sys.executable,
        str(checker),
        "--root",
        str(root),
        "--no-discover",
        "--max-todo-age-sec",
        str(int(args.max_todo_age_sec)),
        "--max-watchdog-age-sec",
        str(int(args.max_watchdog_age_sec)),
        "--watchdog-events",
        str(int(args.watchdog_events)),
        "--output-file",
        str(_to_path(args.output_file)),
        "--history-file",
        str(_to_path(args.history_file)),
    ]
    if args.allow_zero_running_lanes:
        cmd.append("--allow-zero-running-lanes")
    else:
        cmd.append("--require-running-lanes")
    if args.require_watchdog_recent:
        cmd.append("--require-watchdog-recent")
    if args.json:
        cmd.append("--json")
    for todo in todo_files:
        cmd.extend(["--todo-file", str(todo)])
    return cmd


def _run_once(args: argparse.Namespace) -> int:
    root = _to_path(args.root)
    todo_files = _target_todo_files(root, max_age_sec=max(60, int(args.max_todo_age_sec)))
    if args.print_files:
        for item in todo_files:
            print(str(item))
        return 0
    cmd = _build_checker_cmd(args, root=root, todo_files=todo_files)
    proc = subprocess.run(cmd, env=os.environ.copy())
    return int(proc.returncode)


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
        "--interval-sec",
        str(max(60, int(args.interval_sec))),
        "--max-todo-age-sec",
        str(int(args.max_todo_age_sec)),
        "--max-watchdog-age-sec",
        str(int(args.max_watchdog_age_sec)),
        "--watchdog-events",
        str(int(args.watchdog_events)),
        "--output-file",
        str(_to_path(args.output_file)),
        "--history-file",
        str(_to_path(args.history_file)),
    ]
    if args.allow_zero_running_lanes:
        child_args.append("--allow-zero-running-lanes")
    else:
        child_args.append("--require-running-lanes")
    if args.require_watchdog_recent:
        child_args.append("--require-watchdog-recent")
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run swarm todo health for active/recent worktrees.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--max-todo-age-sec", type=int, default=14400, help="Todo freshness threshold in seconds.")
    parser.add_argument(
        "--max-watchdog-age-sec",
        type=int,
        default=1800,
        help="Maximum acceptable watchdog age in seconds.",
    )
    parser.add_argument("--watchdog-events", type=int, default=20, help="Watchdog history events to sample.")
    parser.add_argument(
        "--allow-zero-running-lanes",
        dest="allow_zero_running_lanes",
        action="store_true",
        help="Do not fail when zero lanes run.",
    )
    parser.add_argument(
        "--require-running-lanes",
        dest="allow_zero_running_lanes",
        action="store_false",
        help="Fail when lane inventory is present but nothing is running.",
    )
    parser.set_defaults(allow_zero_running_lanes=False)
    parser.add_argument("--require-watchdog-recent", action="store_true", help="Fail when watchdog is stale.")
    parser.add_argument("--json", action="store_true", help="Print JSON from downstream checker.")
    parser.add_argument("--print-files", action="store_true", help="Print selected todo file list and exit.")
    parser.add_argument("--output-file", default=str(DEFAULT_OUTPUT), help="Output JSON report path.")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY), help="History NDJSON path.")
    parser.add_argument("--watch", action="store_true", help="Run continuously in foreground.")
    parser.add_argument("--interval-sec", type=int, default=1800, help="Foreground/daemon poll interval.")
    parser.add_argument("--daemon", action="store_true", help="Start detached background loop.")
    parser.add_argument("--pid-file", default="", help="Daemon PID file path.")
    parser.add_argument("--log-file", default="", help="Daemon log file path.")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.daemon:
        return _spawn_daemon(args)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    interval_sec = max(60, int(args.interval_sec))

    if not args.watch or args.print_files:
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
