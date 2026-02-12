#!/usr/bin/env python3
"""Continuous cleanup instrumentation loop for Orxaq repos.

Phase order per cycle:
1) repo issues
2) files that do not belong
3) safe improvements
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orxaq_autonomy.manager import ManagerConfig

DEFAULT_OUTPUT = Path("artifacts/autonomy/cleanup_loop/latest.json")
DEFAULT_HISTORY = Path("artifacts/autonomy/cleanup_loop/history.ndjson")
DEFAULT_PID = Path("artifacts/autonomy/cleanup_loop/cleanup.pid")
DEFAULT_LOG = Path("artifacts/autonomy/cleanup_loop/cleanup.log")
DEFAULT_LOCK = Path("artifacts/autonomy/cleanup_loop/cleanup.lock")

STOP = False
JUNK_FILE_NAMES = {".DS_Store", "Thumbs.db"}
JUNK_SUFFIXES = ("~", ".tmp", ".temp", ".swp", ".swo", ".orig", ".bak")
SKIP_DIR_NAMES = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
MAX_SCAN_FILES_PER_REPO = 20000
MIN_INTERVAL_SEC = 30


def _sig_handler(_sig: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_path(raw: str) -> Path:
    return Path(str(raw)).expanduser().resolve()


def _run_git(repo: Path, args: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, (proc.stdout or "").strip()


def _repo_state(repo: Path) -> dict[str, Any]:
    if not repo.exists():
        return {"path": str(repo), "ok": False, "error": "missing_repo"}
    if not repo.is_dir():
        return {"path": str(repo), "ok": False, "error": "not_directory"}
    ok, inside = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
    if not ok or inside.strip().lower() != "true":
        return {"path": str(repo), "ok": False, "error": "not_git_repo"}

    ok_branch, branch = _run_git(repo, ["branch", "--show-current"])
    ok_status, status_out = _run_git(repo, ["status", "--porcelain"])
    ok_unmerged, unmerged_out = _run_git(repo, ["diff", "--name-only", "--diff-filter=U"])
    ok_untracked, untracked_out = _run_git(repo, ["ls-files", "--others", "--exclude-standard"])

    tracked_changes = 0
    untracked_files = 0
    if ok_status:
        for row in status_out.splitlines():
            row = row.rstrip()
            if not row:
                continue
            if row.startswith("?? "):
                untracked_files += 1
            else:
                tracked_changes += 1

    issue_codes: list[str] = []
    if not ok_branch:
        issue_codes.append("branch_unknown")
    if tracked_changes > 0:
        issue_codes.append("tracked_changes_present")
    if ok_unmerged and unmerged_out.strip():
        issue_codes.append("merge_conflicts_present")

    git_dir = repo / ".git"
    lock_path = git_dir / "index.lock"
    lock_exists = lock_path.exists()
    lock_age_sec = -1
    if lock_exists:
        try:
            lock_age_sec = int(time.time() - lock_path.stat().st_mtime)
            issue_codes.append("git_index_lock_present")
        except OSError:
            lock_age_sec = -1

    return {
        "path": str(repo),
        "ok": len(issue_codes) == 0,
        "branch": branch if ok_branch else "",
        "tracked_changes": tracked_changes,
        "untracked_files": untracked_files if ok_untracked else -1,
        "unmerged_files": len([line for line in unmerged_out.splitlines() if line.strip()]) if ok_unmerged else -1,
        "git_index_lock_exists": lock_exists,
        "git_index_lock_age_sec": lock_age_sec,
        "issue_codes": issue_codes,
        "untracked_sample": [line for line in untracked_out.splitlines()[:20] if line.strip()] if ok_untracked else [],
    }


def _is_junk_candidate(path: Path) -> bool:
    name = path.name
    if name in JUNK_FILE_NAMES:
        return True
    return name.endswith(JUNK_SUFFIXES)


def _should_skip_dir(path: Path) -> bool:
    return path.name in SKIP_DIR_NAMES


def _git_tracked(repo: Path, file_path: Path) -> bool:
    rel = str(file_path.resolve().relative_to(repo.resolve()))
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", rel],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _scan_misplaced_files(repo: Path, *, max_files: int) -> dict[str, Any]:
    scanned = 0
    candidates: list[dict[str, Any]] = []
    skipped_due_to_cap = False
    repo_resolved = repo.resolve()

    for root, dirs, files in os.walk(repo_resolved):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if not _should_skip_dir(root_path / d)]
        for file_name in files:
            scanned += 1
            if scanned > max_files:
                skipped_due_to_cap = True
                break
            candidate = root_path / file_name
            if not _is_junk_candidate(candidate):
                continue
            try:
                rel = candidate.relative_to(repo_resolved)
            except ValueError:
                continue
            tracked = _git_tracked(repo_resolved, candidate)
            age_sec = -1
            try:
                age_sec = int(time.time() - candidate.stat().st_mtime)
            except OSError:
                pass
            candidates.append(
                {
                    "path": str(candidate),
                    "repo": str(repo_resolved),
                    "relative_path": str(rel),
                    "tracked": tracked,
                    "age_sec": age_sec,
                }
            )
        if skipped_due_to_cap:
            break

    candidates.sort(key=lambda row: (bool(row.get("tracked", False)), str(row.get("relative_path", ""))))
    return {
        "repo": str(repo_resolved),
        "scanned_files": scanned,
        "scan_capped": skipped_due_to_cap,
        "candidates": candidates,
    }


def _apply_safe_improvements(
    candidates: list[dict[str, Any]],
    *,
    apply_changes: bool,
    min_age_sec: int,
) -> dict[str, Any]:
    removed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in candidates:
        path = Path(str(row.get("path", ""))).resolve()
        tracked = bool(row.get("tracked", False))
        age_sec = int(row.get("age_sec", -1) or -1)
        if tracked:
            skipped.append({"path": str(path), "reason": "tracked_file"})
            continue
        if age_sec >= 0 and age_sec < min_age_sec:
            skipped.append({"path": str(path), "reason": f"too_new<{min_age_sec}s"})
            continue
        if not path.exists():
            skipped.append({"path": str(path), "reason": "already_missing"})
            continue
        if not apply_changes:
            skipped.append({"path": str(path), "reason": "dry_run"})
            continue
        try:
            path.unlink()
            removed.append({"path": str(path), "action": "deleted_untracked_junk"})
        except OSError as exc:
            skipped.append({"path": str(path), "reason": f"delete_failed:{exc}"})
    return {"removed": removed, "skipped": skipped}


@contextmanager
def _cycle_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt  # type: ignore

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl  # type: ignore

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
        yield acquired
    finally:
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


def _cleanup_pass(args: argparse.Namespace) -> dict[str, Any]:
    root = _to_path(args.root)
    cfg = ManagerConfig.from_root(root)
    repos = [root.resolve(), cfg.impl_repo.resolve(), cfg.test_repo.resolve()]
    unique_repos: list[Path] = []
    seen = set()
    for repo in repos:
        key = str(repo)
        if key in seen:
            continue
        seen.add(key)
        unique_repos.append(repo)

    lock_file = _to_path(args.lock_file)
    with _cycle_lock(lock_file) as acquired:
        if not acquired:
            return {
                "timestamp": _utc_now_iso(),
                "ok": True,
                "skipped": True,
                "skip_reason": "lock_busy",
                "lock_file": str(lock_file),
            }

        repo_reports = [_repo_state(repo) for repo in unique_repos]
        repo_issue_count = sum(len(item.get("issue_codes", [])) for item in repo_reports if isinstance(item, dict))

        misplaced_scan = [_scan_misplaced_files(repo, max_files=max(1000, int(args.max_scan_files_per_repo))) for repo in unique_repos]
        all_candidates: list[dict[str, Any]] = []
        for row in misplaced_scan:
            all_candidates.extend(row.get("candidates", []) if isinstance(row.get("candidates"), list) else [])
        improvements = _apply_safe_improvements(
            all_candidates,
            apply_changes=bool(args.apply_changes),
            min_age_sec=max(0, int(args.min_delete_age_sec)),
        )
        removed_count = len(improvements.get("removed", []))

        warnings: list[str] = []
        if repo_issue_count > 0:
            warnings.append(f"repo_issue_count:{repo_issue_count}")
        if any(bool(row.get("scan_capped", False)) for row in misplaced_scan):
            warnings.append("misplaced_scan_capped")

        report = {
            "timestamp": _utc_now_iso(),
            "ok": repo_issue_count == 0,
            "skipped": False,
            "phase_order": ["repo_issues", "misplaced_files", "safe_improvements"],
            "root_dir": str(root),
            "repos": repo_reports,
            "misplaced_files": {
                "candidate_count": len(all_candidates),
                "repos": misplaced_scan,
            },
            "improvements": {
                "apply_changes": bool(args.apply_changes),
                "min_delete_age_sec": int(args.min_delete_age_sec),
                "removed_count": removed_count,
                "removed": improvements.get("removed", []),
                "skipped_count": len(improvements.get("skipped", [])),
                "skipped": improvements.get("skipped", []),
            },
            "warnings": warnings,
            "lock_file": str(lock_file),
            "summary": {
                "repo_issue_count": repo_issue_count,
                "misplaced_candidate_count": len(all_candidates),
                "removed_count": removed_count,
                "repo_paths_with_issues": [
                    str(item.get("path", ""))
                    for item in repo_reports
                    if isinstance(item, dict) and (item.get("issue_codes") or [])
                ],
                "next_focus": (
                    "repo_issues"
                    if repo_issue_count > 0
                    else ("misplaced_files" if len(all_candidates) > 0 else "stability")
                ),
            },
        }
        return report


def _write_report(report: dict[str, Any], output_file: Path, history_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    history_row = {
        "timestamp": str(report.get("timestamp", "")),
        "ok": bool(report.get("ok", False)),
        "skipped": bool(report.get("skipped", False)),
        "repo_count": len(report.get("repos", []) or []),
        "repo_issue_count": sum(
            len(item.get("issue_codes", []))
            for item in (report.get("repos", []) if isinstance(report.get("repos"), list) else [])
            if isinstance(item, dict)
        ),
        "misplaced_candidate_count": int((report.get("misplaced_files", {}) or {}).get("candidate_count", 0) or 0),
        "removed_count": int((report.get("improvements", {}) or {}).get("removed_count", 0) or 0),
    }
    with history_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_row, sort_keys=True) + "\n")


def _run_once(args: argparse.Namespace) -> int:
    report = _cleanup_pass(args)
    output_file = _to_path(args.output_file)
    history_file = _to_path(args.history_file)
    _write_report(report, output_file=output_file, history_file=history_file)
    print(json.dumps(report, indent=None if bool(args.watch) else 2, sort_keys=True), flush=True)
    return 0


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
        str(max(MIN_INTERVAL_SEC, int(args.interval_sec))),
        "--output-file",
        str(_to_path(args.output_file)),
        "--history-file",
        str(_to_path(args.history_file)),
        "--lock-file",
        str(_to_path(args.lock_file)),
        "--max-scan-files-per-repo",
        str(max(1000, int(args.max_scan_files_per_repo))),
        "--min-delete-age-sec",
        str(max(0, int(args.min_delete_age_sec))),
    ]
    if bool(args.apply_changes):
        child_args.append("--apply-changes")
    else:
        child_args.append("--no-apply-changes")

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
    parser = argparse.ArgumentParser(description="Continuous cleanup loop with instrumentation and safe improvements.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--output-file", default=str(DEFAULT_OUTPUT), help="Latest JSON report path.")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY), help="NDJSON history path.")
    parser.add_argument("--pid-file", default=str(DEFAULT_PID), help="Daemon PID file path.")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG), help="Daemon log file path.")
    parser.add_argument("--lock-file", default=str(DEFAULT_LOCK), help="Cycle lock file path.")
    parser.add_argument("--watch", action="store_true", help="Run continuously in foreground.")
    parser.add_argument("--daemon", action="store_true", help="Start detached background loop.")
    parser.add_argument("--interval-sec", type=int, default=900, help="Watch/daemon interval in seconds.")
    parser.add_argument(
        "--apply-changes",
        dest="apply_changes",
        action="store_true",
        default=True,
        help="Apply safe improvements (delete untracked junk files only).",
    )
    parser.add_argument(
        "--no-apply-changes",
        dest="apply_changes",
        action="store_false",
        help="Dry-run improvements without deleting files.",
    )
    parser.add_argument("--min-delete-age-sec", type=int, default=900, help="Minimum file age before deletion.")
    parser.add_argument("--max-scan-files-per-repo", type=int, default=MAX_SCAN_FILES_PER_REPO, help="Scan cap per repo.")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if bool(args.daemon):
        return _spawn_daemon(args)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    if not bool(args.watch):
        return _run_once(args)

    interval_sec = max(MIN_INTERVAL_SEC, int(args.interval_sec))
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
