#!/usr/bin/env python3
"""Run autonomous multi-agent development cycles for Orxaq.

This runner coordinates:
- Codex for implementation tasks in the main repository.
- Gemini for independent testing/review tasks in a sibling test repository.
- Claude for additional independent implementation/review lanes.

It advances a task queue until completion criteria are met or a hard blocker is hit.
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .protocols import MCPContextBundle, SkillProtocolSpec, load_mcp_context, load_skill_protocol

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_BLOCKED = "blocked"
VALID_STATUSES = {STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED}

RETRYABLE_ERROR_PATTERNS = (
    "timeout",
    "timed out",
    "rate limit",
    "429",
    "too many requests",
    "connection reset",
    "connection aborted",
    "network",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "try again",
    "retry",
    "context deadline exceeded",
    "internal server error",
    "unavailable",
    "index.lock",
    "another git process",
    "unable to create",
    "terminal prompts disabled",
    "could not read username",
    "eof when reading a line",
    "resource temporarily unavailable",
    "no rule to make target",
    "command not found",
)

NON_INTERACTIVE_ENV_OVERRIDES = {
    "CI": "1",
    "TERM": "dumb",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_PROGRESS_BAR": "off",
    "PYTHONUNBUFFERED": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "FORCE_COLOR": "0",
    "CLICOLOR": "0",
    "NO_COLOR": "1",
}

VALIDATION_FALLBACKS = {
    "make lint": ["python3 -m ruff check .", ".venv/bin/ruff check ."],
    "make test": [
        "pytest -q",
        "python3 -m pytest -q",
        ".venv/bin/pytest -q",
    ],
}

TEST_COMMAND_HINTS = ("pytest", "make test")
GIT_LOCK_BASENAMES = ("index.lock", "HEAD.lock", "packed-refs.lock")
SUPPORTED_OWNERS = {"codex", "gemini", "claude"}
OWNER_PRIORITY = {"codex": 0, "gemini": 1, "claude": 2}
MAX_CONVERSATION_SNIPPET_CHARS = 8000
MAX_HANDOFF_SNIPPET_CHARS = 5000
HANDOFF_RECENT_LIMIT = 5


@dataclass(frozen=True)
class Task:
    id: str
    owner: str
    priority: int
    title: str
    description: str
    depends_on: list[str]
    acceptance: list[str]


class RunnerLock:
    """Simple file lock to prevent concurrent autonomy runners."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            existing_pid = int(existing.get("pid", 0)) if str(existing.get("pid", "")).isdigit() else 0
            if existing_pid and _pid_is_running(existing_pid):
                raise RuntimeError(
                    f"Another autonomy runner is already active (pid={existing_pid}, lock={self.path})."
                )
            self.path.unlink(missing_ok=True)

        payload = {
            "pid": os.getpid(),
            "created_at": _now_iso(),
            "lock_file": str(self.path),
        }

        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(str(self.path), flags)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
            handle.write("\n")
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        self.path.unlink(missing_ok=True)
        self.acquired = False


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _print(msg: str) -> None:
    print(f"[{_now_iso()}] {msg}", flush=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_optional_text(path: Path | None) -> str:
    if path is None:
        return ""
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_json(path: Path, payload: Any) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _truncate_text(value: str, limit: int = MAX_CONVERSATION_SNIPPET_CHARS) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def append_conversation_event(
    path: Path | None,
    *,
    cycle: int,
    task: Task | None,
    owner: str,
    event_type: str,
    content: str,
    meta: dict[str, Any] | None = None,
) -> None:
    if path is None:
        return
    payload: dict[str, Any] = {
        "timestamp": _now_iso(),
        "cycle": int(cycle),
        "task_id": task.id if task else "",
        "task_title": task.title if task else "",
        "owner": owner,
        "event_type": event_type,
        "content": _truncate_text(content),
    }
    if meta:
        payload["meta"] = meta
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _append_ndjson(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _tail_ndjson(path: Path, limit: int = HANDOFF_RECENT_LIMIT) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def record_handoff_event(
    *,
    handoff_dir: Path,
    task: Task,
    outcome: dict[str, Any],
) -> None:
    status = str(outcome.get("status", "")).strip().lower()
    summary = str(outcome.get("summary", "")).strip()
    blocker = str(outcome.get("blocker", "")).strip()
    next_actions = [str(item) for item in (outcome.get("next_actions", []) or [])]
    payload = {
        "timestamp": _now_iso(),
        "task_id": task.id,
        "owner": task.owner,
        "status": status,
        "summary": summary,
        "blocker": blocker,
        "next_actions": next_actions,
        "commit": str(outcome.get("commit", "")).strip(),
    }
    if task.owner in {"codex", "claude"}:
        _append_ndjson(handoff_dir / "to_gemini.ndjson", payload)
    if task.owner == "gemini":
        _append_ndjson(handoff_dir / "to_codex.ndjson", payload)


def render_handoff_context(handoff_dir: Path, owner: str) -> str:
    if owner == "gemini":
        source = handoff_dir / "to_gemini.ndjson"
        heading = "Recent implementation handoffs for testing"
    elif owner in {"codex", "claude"}:
        source = handoff_dir / "to_codex.ndjson"
        heading = "Recent testing feedback for implementation"
    else:
        return ""

    events = _tail_ndjson(source, HANDOFF_RECENT_LIMIT)
    if not events:
        return ""
    lines = [f"{heading}:"]
    for item in events:
        lines.append(
            "- "
            f"[{item.get('timestamp', '')}] task={item.get('task_id', '')} "
            f"status={item.get('status', '')} "
            f"summary={str(item.get('summary', '')).strip()[:220]} "
            f"blocker={str(item.get('blocker', '')).strip()[:220]} "
            f"next_actions={str(item.get('next_actions', []))[:260]}"
        )
    return _truncate_text("\n".join(lines), limit=MAX_HANDOFF_SNIPPET_CHARS)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(ts: str) -> dt.datetime | None:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def build_subprocess_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(NON_INTERACTIVE_ENV_OVERRIDES)
    if extra_env:
        env.update(extra_env)
    return env


def _list_process_commands() -> list[str]:
    if os.name == "nt":
        return []
    result = subprocess.run(
        ["ps", "ax", "-o", "command="],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def has_running_git_processes() -> bool:
    commands = _list_process_commands()
    if not commands:
        return False
    for cmd in commands:
        lowered = cmd.lower()
        if "git " in lowered or lowered.endswith("/git"):
            return True
    return False


def find_git_lock_files(repo: Path) -> list[Path]:
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return []
    lock_files: list[Path] = []
    for name in GIT_LOCK_BASENAMES:
        lock_path = git_dir / name
        if lock_path.exists():
            lock_files.append(lock_path)
    return lock_files


def heal_stale_git_locks(repo: Path, stale_after_sec: int) -> list[Path]:
    removed: list[Path] = []
    lock_files = find_git_lock_files(repo)
    if not lock_files:
        return removed
    if has_running_git_processes():
        return removed
    now = time.time()
    for lock_path in lock_files:
        try:
            age = now - lock_path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age < stale_after_sec:
            continue
        lock_path.unlink(missing_ok=True)
        removed.append(lock_path)
    return removed


def get_repo_filetype_context(repo: Path, limit: int = 8) -> str:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return "File-type profile unavailable."
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not files:
        return "File-type profile unavailable."
    counts: Counter[str] = Counter()
    for rel in files:
        path = Path(rel)
        suffix = path.suffix.lower().lstrip(".")
        if suffix:
            counts[suffix] += 1
        else:
            counts["(no_ext)"] += 1
    most_common = counts.most_common(limit)
    top = ", ".join(f"{ext}:{count}" for ext, count in most_common)
    return f"Top file types: {top}."


def repo_state_hints(repo: Path) -> list[str]:
    hints: list[str] = []
    git_dir = repo / ".git"
    if not git_dir.exists():
        return hints
    if (git_dir / "MERGE_HEAD").exists():
        hints.append("Merge in progress detected (.git/MERGE_HEAD).")
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        hints.append("Rebase in progress detected (.git/rebase-*).")
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        hints.append("Cherry-pick in progress detected (.git/CHERRY_PICK_HEAD).")
    return hints


def validation_fallback_commands(raw: str) -> list[str]:
    try:
        normalized = " ".join(shlex.split(raw.strip()))
    except ValueError:
        return []
    return list(VALIDATION_FALLBACKS.get(normalized, []))


def is_test_command(raw: str) -> bool:
    normalized = raw.lower()
    return any(hint in normalized for hint in TEST_COMMAND_HINTS)


def write_heartbeat(
    path: Path,
    *,
    phase: str,
    cycle: int,
    task_id: str | None,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "timestamp": _now_iso(),
        "pid": os.getpid(),
        "phase": phase,
        "cycle": cycle,
        "task_id": task_id or "",
        "message": message,
    }
    if extra:
        payload.update(extra)
    _write_json(path, payload)


def load_tasks(path: Path) -> list[Task]:
    raw = json.loads(_read_text(path))
    if not isinstance(raw, list):
        raise ValueError(f"Task file must be a JSON array: {path}")
    tasks: list[Task] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"Task entries must be objects: {item!r}")
        task = Task(
            id=str(item["id"]),
            owner=str(item["owner"]).lower(),
            priority=int(item["priority"]),
            title=str(item["title"]),
            description=str(item["description"]),
            depends_on=[str(x) for x in item.get("depends_on", [])],
            acceptance=[str(x) for x in item.get("acceptance", [])],
        )
        if task.id in seen:
            raise ValueError(f"Duplicate task id: {task.id}")
        if task.owner not in SUPPORTED_OWNERS:
            raise ValueError(f"Unsupported task owner {task.owner!r} for task {task.id}")
        seen.add(task.id)
        tasks.append(task)
    return tasks


def load_state(path: Path, tasks: list[Task]) -> dict[str, dict[str, Any]]:
    if path.exists():
        raw = json.loads(_read_text(path))
        if not isinstance(raw, dict):
            raise ValueError(f"State file must be a JSON object: {path}")
    else:
        raw = {}

    out: dict[str, dict[str, Any]] = {}
    for task in tasks:
        entry = raw.get(task.id, {})
        status = str(entry.get("status", STATUS_PENDING))
        if status not in VALID_STATUSES:
            status = STATUS_PENDING
        if status == STATUS_IN_PROGRESS:
            # Recover from interrupted runs without deadlocking task selection.
            status = STATUS_PENDING
        out[task.id] = {
            "status": status,
            "attempts": _safe_int(entry.get("attempts", 0), 0),
            "retryable_failures": _safe_int(entry.get("retryable_failures", 0), 0),
            "not_before": str(entry.get("not_before", "")),
            "last_update": str(entry.get("last_update", "")),
            "last_summary": str(entry.get("last_summary", "")),
            "last_error": str(entry.get("last_error", "")),
            "owner": task.owner,
        }
    return out


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    _write_json(path, state)


def load_dependency_state(path: Path | None) -> dict[str, dict[str, Any]] | None:
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _dependency_done(
    dep: str,
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> bool:
    dep_state = state.get(dep, {})
    if dep_state:
        return dep_state.get("status") == STATUS_DONE
    if dependency_state:
        ext = dependency_state.get(dep, {})
        if ext:
            return ext.get("status") == STATUS_DONE
    return False


def unresolved_dependencies(
    task: Task,
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    unresolved: list[str] = []
    for dep in task.depends_on:
        if not _dependency_done(dep, state, dependency_state):
            unresolved.append(dep)
    return unresolved


def task_dependencies_done(
    task: Task,
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> bool:
    return len(unresolved_dependencies(task, state, dependency_state)) == 0


def _task_ready_now(entry: dict[str, Any], now: dt.datetime) -> bool:
    not_before = _parse_iso(str(entry.get("not_before", "")))
    if not_before is None:
        return True
    return now >= not_before


def select_next_task(
    tasks: list[Task],
    state: dict[str, dict[str, Any]],
    now: dt.datetime | None = None,
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> Task | None:
    now = now or _now_utc()
    ready: list[Task] = []
    for task in tasks:
        entry = state[task.id]
        status = str(entry.get("status", STATUS_PENDING))
        if status != STATUS_PENDING:
            continue
        if not _task_ready_now(entry, now):
            continue
        if not task_dependencies_done(task, state, dependency_state):
            continue
        ready.append(task)
    if not ready:
        return None
    ready.sort(key=lambda t: (t.priority, OWNER_PRIORITY[t.owner], t.id))
    return ready[0]


def soonest_pending_time(
    tasks: list[Task],
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> dt.datetime | None:
    soonest: dt.datetime | None = None
    for task in tasks:
        entry = state[task.id]
        if entry.get("status") != STATUS_PENDING:
            continue
        if not task_dependencies_done(task, state, dependency_state):
            continue
        not_before = _parse_iso(str(entry.get("not_before", "")))
        if not_before is None:
            continue
        if soonest is None or not_before < soonest:
            soonest = not_before
    return soonest


def build_agent_prompt(
    task: Task,
    objective_text: str,
    role: str,
    repo_path: Path,
    retry_context: dict[str, Any] | None,
    repo_context: str,
    repo_hints: list[str],
    skill_protocol: SkillProtocolSpec,
    mcp_context: MCPContextBundle | None,
    startup_instructions: str = "",
    handoff_context: str = "",
) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance) or "- No explicit acceptance items"

    continuation_block = ""
    if retry_context:
        attempts = _safe_int(retry_context.get("attempts", 0), 0)
        if attempts > 1 or retry_context.get("last_error"):
            continuation_block = (
                "\nPrevious attempt context:\n"
                f"- Attempts so far: {attempts}\n"
                f"- Prior summary: {str(retry_context.get('last_summary', '')).strip()[:800]}\n"
                f"- Prior blocker/error: {str(retry_context.get('last_error', '')).strip()[:1200]}\n"
                "- Recovery directive: Continue from the current repository state and finish all acceptance criteria.\n"
            )

    repo_hints_text = ""
    if repo_hints:
        hints = "\n".join(f"- {hint}" for hint in repo_hints)
        repo_hints_text = f"Repository state hints:\n{hints}\n"
    protocol_behaviors = "\n".join(f"- {item}" for item in skill_protocol.required_behaviors)
    mcp_context_text = mcp_context.render_context() + "\n" if mcp_context else ""
    startup_text = startup_instructions.strip()
    startup_block = ""
    if startup_text:
        startup_block = f"Role startup instructions:\n{startup_text}\n\n"
    handoff_text = handoff_context.strip()
    handoff_block = ""
    if handoff_text:
        handoff_block = f"{handoff_text}\n\n"

    return (
        f"{objective_text.strip()}\n\n"
        f"Autonomy skill protocol:\n"
        f"- Name: {skill_protocol.name}\n"
        f"- Version: {skill_protocol.version}\n"
        f"- Description: {skill_protocol.description}\n"
        f"- Required behaviors:\n{protocol_behaviors}\n"
        f"- File-type policy: {skill_protocol.filetype_policy}\n\n"
        f"{startup_block}"
        f"{handoff_block}"
        f"{mcp_context_text}"
        "Current autonomous task:\n"
        f"- Task ID: {task.id}\n"
        f"- Title: {task.title}\n"
        f"- Owner role: {role}\n"
        f"- Repository path: {repo_path}\n"
        f"- Description: {task.description}\n"
        f"- Repository file profile: {repo_context}\n"
        f"- Acceptance criteria:\n{acceptance}\n"
        f"{repo_hints_text}"
        f"{continuation_block}\n"
        "Execution requirements:\n"
        "- Work fully autonomously for this task.\n"
        "- Do not ask for user nudges unless blocked by credentials, destructive actions, or true tradeoff decisions.\n"
        "- Run validation commands: `make lint` then `make test`.\n"
        "- Commit and push contiguous changes.\n"
        "- If a command fails transiently (rate limits/network/timeouts), retry with resilient fallbacks before giving up.\n"
        "- Use non-interactive commands only (never wait for terminal prompts).\n"
        "- Handle new/unknown file types safely: preserve binary formats, avoid destructive rewrites, and add `.gitattributes` entries when needed.\n"
        "- If git locks or in-progress git states are detected, recover safely and continue.\n"
        "- If you are implementation-owner: provide explicit test requests for Gemini in next_actions.\n"
        "- If you are test-owner: when you find implementation issues, provide concrete fix feedback and hints for Codex in blocker/next_actions.\n"
        "- Return ONLY JSON with keys: status, summary, commit, validations, next_actions, blocker.\n"
        "- status must be one of: done, partial, blocked.\n"
    )


def run_command(
    cmd: list[str],
    cwd: Path,
    timeout_sec: int,
    progress_callback: Callable[[int], None] | None = None,
    progress_interval_sec: int = 15,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = build_subprocess_env(extra_env)
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as err:
        missing = err.filename or (cmd[0] if cmd else "")
        return subprocess.CompletedProcess(
            cmd,
            returncode=127,
            stdout="",
            stderr=f"[ENOENT] command not found: {missing}",
        )
    start = time.monotonic()
    last_progress = start

    while True:
        elapsed = int(time.monotonic() - start)
        if progress_callback and (time.monotonic() - last_progress) >= progress_interval_sec:
            progress_callback(elapsed)
            last_progress = time.monotonic()
        try:
            stdout, stderr = process.communicate(timeout=1)
            return subprocess.CompletedProcess(cmd, returncode=process.returncode or 0, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            if elapsed >= timeout_sec:
                process.kill()
                stdout, stderr = process.communicate()
                timeout_msg = f"\n[TIMEOUT] command exceeded {timeout_sec}s: {' '.join(cmd)}"
                return subprocess.CompletedProcess(
                    cmd,
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr + timeout_msg,
                )


def run_validations(
    repo: Path,
    validate_commands: list[str],
    timeout_sec: int,
    progress_callback: Callable[[str, int], None] | None = None,
    retries_per_command: int = 1,
) -> tuple[bool, str]:
    for raw in validate_commands:
        try:
            cmd = shlex.split(raw)
        except ValueError as err:
            return False, f"Validation command parse failed for `{raw}`: {err}"
        if not cmd:
            continue
        attempts = max(1, retries_per_command + 1) if is_test_command(raw) else 1
        failure_details = ""
        for idx in range(attempts):
            _print(f"Running validation in {repo}: {raw} (attempt {idx + 1}/{attempts})")
            result = run_command(
                cmd,
                cwd=repo,
                timeout_sec=timeout_sec,
                progress_callback=(lambda elapsed: progress_callback(raw, elapsed)) if progress_callback else None,
            )
            if result.returncode == 0:
                failure_details = ""
                break
            failure_details = (result.stdout + "\n" + result.stderr).strip()
            if idx + 1 < attempts:
                _print(f"Validation retry queued for `{raw}` after failure.")
        if not failure_details:
            continue

        fallbacks = validation_fallback_commands(raw)
        if fallbacks:
            fallback_errors: list[str] = []
            for fallback in fallbacks:
                fallback_cmd = shlex.split(fallback)
                if not fallback_cmd:
                    continue
                _print(f"Running fallback validation in {repo}: {fallback}")
                fallback_result = run_command(
                    fallback_cmd,
                    cwd=repo,
                    timeout_sec=timeout_sec,
                    progress_callback=(lambda elapsed: progress_callback(fallback, elapsed)) if progress_callback else None,
                )
                if fallback_result.returncode == 0:
                    failure_details = ""
                    break
                fallback_output = (fallback_result.stdout + "\n" + fallback_result.stderr).strip()
                fallback_errors.append(
                    f"`{fallback}` failed:\n{fallback_output}"
                )
            if not failure_details:
                continue
            if fallback_errors:
                failure_details = f"{failure_details}\n\nFallback failures:\n" + "\n\n".join(fallback_errors)

        return False, f"Validation failed for `{raw}`:\n{failure_details}"
    return True, "ok"


def _git_output(repo: Path, args: list[str], timeout_sec: int = 120) -> tuple[bool, str]:
    result = run_command(["git", *args], cwd=repo, timeout_sec=timeout_sec)
    merged = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        return False, merged
    return True, result.stdout.strip()


def ensure_repo_pushed(repo: Path, timeout_sec: int = 180) -> tuple[bool, str]:
    ok, inside = _git_output(repo, ["rev-parse", "--is-inside-work-tree"])
    if not ok:
        return True, f"push check skipped (not a git repo): {inside}"

    ok, upstream = _git_output(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not ok:
        return False, f"no upstream configured for branch: {upstream}"

    ok, counts = _git_output(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
    if not ok:
        return False, f"unable to compare branch with upstream: {counts}"
    parts = counts.split()
    if len(parts) != 2:
        return False, f"unexpected rev-list output: {counts}"
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
    except ValueError:
        return False, f"unable to parse ahead/behind counts: {counts}"

    if ahead <= 0:
        return True, f"branch synced to {upstream} (behind={behind}, ahead={ahead})"

    push = run_command(["git", "push"], cwd=repo, timeout_sec=timeout_sec)
    if push.returncode != 0:
        return False, f"git push failed:\n{(push.stdout + '\n' + push.stderr).strip()}"

    ok, recounted = _git_output(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
    if not ok:
        return False, f"push verification failed: {recounted}"
    parts = recounted.split()
    if len(parts) != 2:
        return False, f"unexpected post-push rev-list output: {recounted}"
    try:
        behind_after = int(parts[0])
        ahead_after = int(parts[1])
    except ValueError:
        return False, f"unable to parse post-push counts: {recounted}"
    if ahead_after > 0:
        return False, f"branch still ahead after push (behind={behind_after}, ahead={ahead_after})"
    return True, f"push verified to {upstream} (behind={behind_after}, ahead={ahead_after})"


def _extract_json_object_from_text(raw: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def parse_json_text(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fence_patterns = (
        r"```json\s*(\{[\s\S]*?\})\s*```",
        r"```\s*(\{[\s\S]*?\})\s*```",
    )
    for pattern in fence_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        block = match.group(1)
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return _extract_json_object_from_text(text)


def normalize_outcome(raw: dict[str, Any]) -> dict[str, Any]:
    status = str(raw.get("status", STATUS_BLOCKED)).strip().lower()
    if status not in {STATUS_DONE, STATUS_PARTIAL, STATUS_BLOCKED}:
        status = STATUS_PARTIAL
    next_actions = raw.get("next_actions", [])
    if not isinstance(next_actions, list):
        next_actions = [str(next_actions)]

    return {
        "status": status,
        "summary": str(raw.get("summary", "")).strip(),
        "commit": str(raw.get("commit", "")).strip(),
        "validations": raw.get("validations", []),
        "next_actions": [str(x) for x in next_actions],
        "blocker": str(raw.get("blocker", "")).strip(),
        "raw_output": str(raw.get("raw_output", "")).strip(),
    }


def run_codex_task(
    *,
    task: Task,
    repo: Path,
    objective_text: str,
    schema_path: Path,
    output_dir: Path,
    codex_cmd: str,
    codex_model: str | None,
    timeout_sec: int,
    retry_context: dict[str, Any],
    progress_callback: Callable[[int], None] | None,
    repo_context: str,
    repo_hints: list[str],
    skill_protocol: SkillProtocolSpec,
    mcp_context: MCPContextBundle | None,
    startup_instructions: str,
    handoff_context: str,
    conversation_log_file: Path | None,
    cycle: int,
) -> tuple[bool, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{task.id}_codex_result.json"
    prompt = build_agent_prompt(
        task,
        objective_text,
        role="implementation-owner",
        repo_path=repo,
        retry_context=retry_context,
        repo_context=repo_context,
        repo_hints=repo_hints,
        skill_protocol=skill_protocol,
        mcp_context=mcp_context,
        startup_instructions=startup_instructions,
        handoff_context=handoff_context,
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="prompt",
        content=prompt,
        meta={"agent": "codex"},
    )

    cmd = [
        codex_cmd,
        "exec",
        "--cd",
        str(repo),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_file),
        prompt,
    ]
    if codex_model:
        cmd[2:2] = ["--model", codex_model]

    _print(f"Running Codex task {task.id}")
    result = run_command(cmd, cwd=repo, timeout_sec=timeout_sec, progress_callback=progress_callback)
    if result.returncode != 0:
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_error",
            content=(result.stdout + "\n" + result.stderr).strip(),
            meta={"agent": "codex", "returncode": result.returncode},
        )
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Codex command failed",
                "blocker": (result.stdout + "\n" + result.stderr).strip(),
                "next_actions": [],
            }
        )

    parsed = parse_json_text(output_file.read_text(encoding="utf-8")) if output_file.exists() else None
    if parsed is None:
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_error",
            content="Expected JSON object in output-last-message file.",
            meta={"agent": "codex"},
        )
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Codex produced non-JSON final output",
                "blocker": "Expected JSON object in output-last-message file.",
                "next_actions": [],
            }
        )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="agent_output",
        content=json.dumps(parsed, sort_keys=True),
        meta={"agent": "codex"},
    )
    return True, normalize_outcome(parsed)


def run_gemini_task(
    *,
    task: Task,
    repo: Path,
    objective_text: str,
    gemini_cmd: str,
    gemini_model: str | None,
    timeout_sec: int,
    retry_context: dict[str, Any],
    progress_callback: Callable[[int], None] | None,
    repo_context: str,
    repo_hints: list[str],
    skill_protocol: SkillProtocolSpec,
    mcp_context: MCPContextBundle | None,
    startup_instructions: str,
    handoff_context: str,
    conversation_log_file: Path | None,
    cycle: int,
) -> tuple[bool, dict[str, Any]]:
    prompt = build_agent_prompt(
        task,
        objective_text,
        role="test-owner",
        repo_path=repo,
        retry_context=retry_context,
        repo_context=repo_context,
        repo_hints=repo_hints,
        skill_protocol=skill_protocol,
        mcp_context=mcp_context,
        startup_instructions=startup_instructions,
        handoff_context=handoff_context,
    )
    prompt += (
        "\nTesting-owner constraints:\n"
        "- Focus on tests/specs/benchmarks and validation depth.\n"
        "- Avoid production code edits unless strictly required to keep tests executable.\n"
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="prompt",
        content=prompt,
        meta={"agent": "gemini"},
    )

    cmd = [
        gemini_cmd,
        "--approval-mode",
        "yolo",
        "--output-format",
        "text",
        "-p",
        prompt,
    ]
    if gemini_model:
        cmd[1:1] = ["--model", gemini_model]

    _print(f"Running Gemini task {task.id}")
    result = run_command(cmd, cwd=repo, timeout_sec=timeout_sec, progress_callback=progress_callback)
    if result.returncode != 0:
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_error",
            content=(result.stdout + "\n" + result.stderr).strip(),
            meta={"agent": "gemini", "returncode": result.returncode},
        )
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Gemini command failed",
                "blocker": (result.stdout + "\n" + result.stderr).strip(),
                "next_actions": [],
            }
        )

    parsed = parse_json_text(result.stdout)
    if parsed is None:
        parsed = {
            "status": STATUS_PARTIAL,
            "summary": "Gemini output was not strict JSON; treating as partial",
            "next_actions": [],
            "raw_output": result.stdout.strip(),
        }
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="agent_output",
        content=result.stdout.strip(),
        meta={"agent": "gemini"},
    )
    return True, normalize_outcome(parsed)


def run_claude_task(
    *,
    task: Task,
    repo: Path,
    objective_text: str,
    claude_cmd: str,
    claude_model: str | None,
    timeout_sec: int,
    retry_context: dict[str, Any],
    progress_callback: Callable[[int], None] | None,
    repo_context: str,
    repo_hints: list[str],
    skill_protocol: SkillProtocolSpec,
    mcp_context: MCPContextBundle | None,
    startup_instructions: str,
    handoff_context: str,
    conversation_log_file: Path | None,
    cycle: int,
) -> tuple[bool, dict[str, Any]]:
    prompt = build_agent_prompt(
        task,
        objective_text,
        role="review-owner",
        repo_path=repo,
        retry_context=retry_context,
        repo_context=repo_context,
        repo_hints=repo_hints,
        skill_protocol=skill_protocol,
        mcp_context=mcp_context,
        startup_instructions=startup_instructions,
        handoff_context=handoff_context,
    )
    prompt += (
        "\nReview-owner constraints:\n"
        "- Focus on governance, architecture, and collaboration safety constraints.\n"
        "- Keep changes production-grade and verify with tests where applicable.\n"
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="prompt",
        content=prompt,
        meta={"agent": "claude"},
    )

    cmd = [claude_cmd]
    if claude_model:
        cmd.extend(["--model", claude_model])
    cmd.extend(["-p", prompt])

    _print(f"Running Claude task {task.id}")
    result = run_command(cmd, cwd=repo, timeout_sec=timeout_sec, progress_callback=progress_callback)
    if result.returncode != 0:
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_error",
            content=(result.stdout + "\n" + result.stderr).strip(),
            meta={"agent": "claude", "returncode": result.returncode},
        )
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Claude command failed",
                "blocker": (result.stdout + "\n" + result.stderr).strip(),
                "next_actions": [],
            }
        )

    parsed = parse_json_text(result.stdout)
    if parsed is None:
        parsed = {
            "status": STATUS_PARTIAL,
            "summary": "Claude output was not strict JSON; treating as partial",
            "next_actions": [],
            "raw_output": result.stdout.strip(),
        }
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="agent_output",
        content=result.stdout.strip(),
        meta={"agent": "claude"},
    )
    return True, normalize_outcome(parsed)


def ensure_cli_exists(binary: str, role: str) -> None:
    if shutil.which(binary):
        return
    raise FileNotFoundError(f"{role} CLI binary not found in PATH: {binary}")


def summarize_run(
    *,
    task: Task,
    repo: Path,
    outcome: dict[str, Any],
    report_dir: Path,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{task.id}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        f"# Autonomy Task Report: {task.id}",
        "",
        f"- Timestamp: {_now_iso()}",
        f"- Owner: {task.owner}",
        f"- Repo: {repo}",
        f"- Status: {outcome.get('status', '')}",
        f"- Summary: {outcome.get('summary', '')}",
        f"- Commit: {outcome.get('commit', '')}",
        f"- Blocker: {outcome.get('blocker', '')}",
        "",
        "## Next Actions",
    ]
    for item in outcome.get("next_actions", []) or []:
        lines.append(f"- {item}")
    _write_text_atomic(report_path, "\n".join(lines).strip() + "\n")


def is_retryable_error(text: str) -> bool:
    lowered = text.lower()
    if not lowered.strip():
        return False
    if re.search(r"\b(5\d\d|429)\b", lowered):
        return True
    return any(pattern in lowered for pattern in RETRYABLE_ERROR_PATTERNS)


def schedule_retry(
    *,
    entry: dict[str, Any],
    summary: str,
    error: str,
    retryable: bool,
    backoff_base_sec: int,
    backoff_max_sec: int,
) -> int:
    delay = max(1, backoff_base_sec)
    if retryable:
        entry["retryable_failures"] = _safe_int(entry.get("retryable_failures", 0), 0) + 1
        exp = max(0, entry["retryable_failures"] - 1)
        delay = min(backoff_max_sec, backoff_base_sec * (2**exp))

    not_before = _now_utc() + dt.timedelta(seconds=delay)
    entry["status"] = STATUS_PENDING
    entry["not_before"] = not_before.isoformat()
    entry["last_error"] = error.strip()
    entry["last_summary"] = summary.strip()
    entry["last_update"] = _now_iso()
    return delay


def mark_blocked(entry: dict[str, Any], summary: str, error: str) -> None:
    entry["status"] = STATUS_BLOCKED
    entry["last_error"] = error.strip()
    entry["last_summary"] = summary.strip()
    entry["last_update"] = _now_iso()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autonomous multi-agent runner for Orxaq.")
    parser.add_argument("--impl-repo", default="../orxaq", help="Implementation repository path.")
    parser.add_argument(
        "--test-repo",
        default="../orxaq_gemini",
        help="Independent test repository path for Gemini-owned tasks.",
    )
    parser.add_argument("--tasks-file", default="config/tasks.json")
    parser.add_argument("--state-file", default="state/state.json")
    parser.add_argument("--objective-file", default="config/objective.md")
    parser.add_argument("--codex-schema", default="config/codex_result.schema.json")
    parser.add_argument("--artifacts-dir", default="artifacts/autonomy")
    parser.add_argument("--heartbeat-file", default="artifacts/autonomy/heartbeat.json")
    parser.add_argument("--lock-file", default="artifacts/autonomy/runner.lock")
    parser.add_argument("--max-cycles", type=int, default=10000)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--max-retryable-blocked-retries", type=int, default=20)
    parser.add_argument("--retry-backoff-base-sec", type=int, default=30)
    parser.add_argument("--retry-backoff-max-sec", type=int, default=1800)
    parser.add_argument("--git-lock-stale-sec", type=int, default=300)
    parser.add_argument("--idle-sleep-sec", type=int, default=10)
    parser.add_argument("--agent-timeout-sec", type=int, default=3600)
    parser.add_argument("--validate-timeout-sec", type=int, default=1800)
    parser.add_argument(
        "--validate-command",
        action="append",
        default=["make lint", "make test"],
        help="Validation command to run after each completed task (repeatable).",
    )
    parser.add_argument("--codex-cmd", default="codex")
    parser.add_argument("--gemini-cmd", default="gemini")
    parser.add_argument("--claude-cmd", default="claude")
    parser.add_argument("--codex-model", default=None)
    parser.add_argument("--gemini-model", default=None)
    parser.add_argument("--claude-model", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validation-retries", type=int, default=1)
    parser.add_argument("--skill-protocol-file", default="config/skill_protocol.json")
    parser.add_argument("--mcp-context-file", default="")
    parser.add_argument("--codex-startup-prompt-file", default="")
    parser.add_argument("--gemini-startup-prompt-file", default="")
    parser.add_argument("--claude-startup-prompt-file", default="")
    parser.add_argument("--conversation-log-file", default="artifacts/autonomy/conversations.ndjson")
    parser.add_argument("--handoff-dir", default="artifacts/autonomy/handoffs")
    parser.add_argument(
        "--dependency-state-file",
        default="",
        help="Optional state file to resolve dependencies outside the current owner-filtered task set.",
    )
    parser.add_argument(
        "--owner-filter",
        action="append",
        default=[],
        help="Restrict execution to specific task owners (repeatable).",
    )
    args = parser.parse_args(argv)

    impl_repo = Path(args.impl_repo).resolve()
    test_repo = Path(args.test_repo).resolve()
    tasks_file = Path(args.tasks_file).resolve()
    state_file = Path(args.state_file).resolve()
    objective_file = Path(args.objective_file).resolve()
    schema_file = Path(args.codex_schema).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    heartbeat_file = Path(args.heartbeat_file).resolve()
    lock_file = Path(args.lock_file).resolve()
    skill_protocol_file = Path(args.skill_protocol_file).resolve() if args.skill_protocol_file else None
    mcp_context_file = Path(args.mcp_context_file).resolve() if args.mcp_context_file else None
    codex_startup_prompt_file = Path(args.codex_startup_prompt_file).resolve() if args.codex_startup_prompt_file else None
    gemini_startup_prompt_file = (
        Path(args.gemini_startup_prompt_file).resolve() if args.gemini_startup_prompt_file else None
    )
    claude_startup_prompt_file = (
        Path(args.claude_startup_prompt_file).resolve() if args.claude_startup_prompt_file else None
    )
    conversation_log_file = Path(args.conversation_log_file).resolve() if args.conversation_log_file else None
    handoff_dir = Path(args.handoff_dir).resolve()
    dependency_state_file = Path(args.dependency_state_file).resolve() if args.dependency_state_file else None

    if not impl_repo.exists():
        raise FileNotFoundError(f"Implementation repo not found: {impl_repo}")
    if not tasks_file.exists():
        raise FileNotFoundError(f"Task file not found: {tasks_file}")
    if not objective_file.exists():
        raise FileNotFoundError(f"Objective file not found: {objective_file}")
    if not schema_file.exists():
        raise FileNotFoundError(f"Codex schema file not found: {schema_file}")

    lock = RunnerLock(lock_file)
    lock.acquire()
    atexit.register(lock.release)

    tasks = load_tasks(tasks_file)
    owner_filter = {str(item).strip().lower() for item in args.owner_filter if str(item).strip()}
    if owner_filter:
        unknown = owner_filter - SUPPORTED_OWNERS
        if unknown:
            raise RuntimeError(f"Unknown owner filter(s): {sorted(unknown)}")
        tasks = [task for task in tasks if task.owner in owner_filter]
        if not tasks:
            raise RuntimeError(f"No tasks left after applying owner filter: {sorted(owner_filter)}")
    owners = {task.owner for task in tasks}
    if "codex" in owners:
        ensure_cli_exists(args.codex_cmd, "Codex")
    if "gemini" in owners:
        ensure_cli_exists(args.gemini_cmd, "Gemini")
    if "claude" in owners:
        ensure_cli_exists(args.claude_cmd, "Claude")

    state = load_state(state_file, tasks)
    objective_text = _read_text(objective_file)
    skill_protocol = load_skill_protocol(skill_protocol_file)
    mcp_context = load_mcp_context(mcp_context_file)
    codex_startup_instructions = _read_optional_text(codex_startup_prompt_file)
    gemini_startup_instructions = _read_optional_text(gemini_startup_prompt_file)
    claude_startup_instructions = _read_optional_text(claude_startup_prompt_file)
    save_state(state_file, state)

    _print(f"Starting autonomy runner with {len(tasks)} tasks")
    write_heartbeat(
        heartbeat_file,
        phase="started",
        cycle=0,
        task_id=None,
        message="autonomy runner started",
        extra={"tasks": len(tasks)},
    )

    for cycle in range(1, args.max_cycles + 1):
        dependency_state = load_dependency_state(dependency_state_file)

        if all(state[t.id]["status"] == STATUS_DONE for t in tasks):
            _print("All tasks are marked done.")
            write_heartbeat(
                heartbeat_file,
                phase="completed",
                cycle=cycle,
                task_id=None,
                message="all tasks completed",
            )
            return 0

        now = _now_utc()
        task = select_next_task(tasks, state, now=now, dependency_state=dependency_state)
        if task is None:
            soonest = soonest_pending_time(tasks, state, dependency_state=dependency_state)
            pending = [t.id for t in tasks if state[t.id]["status"] == STATUS_PENDING]
            blocked = [t.id for t in tasks if state[t.id]["status"] == STATUS_BLOCKED]
            waiting_on_deps: dict[str, list[str]] = {}
            for pending_task in tasks:
                if state[pending_task.id]["status"] != STATUS_PENDING:
                    continue
                deps = unresolved_dependencies(pending_task, state, dependency_state)
                if deps:
                    waiting_on_deps[pending_task.id] = deps

            if soonest is not None and soonest > now:
                sleep_for = min(args.idle_sleep_sec, max(1, int((soonest - now).total_seconds())))
                write_heartbeat(
                    heartbeat_file,
                    phase="idle",
                    cycle=cycle,
                    task_id=None,
                    message=f"waiting {sleep_for}s for retry cooldown",
                    extra={"pending": pending, "blocked": blocked, "waiting_on_deps": waiting_on_deps},
                )
                time.sleep(sleep_for)
                continue

            _print(f"No ready tasks remain. Pending={pending}, Blocked={blocked}")
            write_heartbeat(
                heartbeat_file,
                phase="stalled",
                cycle=cycle,
                task_id=None,
                message="no ready tasks remain",
                extra={"pending": pending, "blocked": blocked, "waiting_on_deps": waiting_on_deps},
            )
            return 2

        _print(f"Cycle {cycle}: selected task {task.id} ({task.owner})")
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="task_selected",
            content=f"Selected task `{task.id}` for owner `{task.owner}`.",
            meta={"priority": task.priority},
        )
        task_state = state[task.id]
        task_state["status"] = STATUS_IN_PROGRESS
        task_state["last_update"] = _now_iso()
        task_state["attempts"] = _safe_int(task_state.get("attempts", 0), 0) + 1
        task_state["not_before"] = ""
        save_state(state_file, state)
        write_heartbeat(
            heartbeat_file,
            phase="task_started",
            cycle=cycle,
            task_id=task.id,
            message=f"running task {task.id}",
            extra={"owner": task.owner, "attempts": task_state["attempts"]},
        )

        if args.dry_run:
            _print(f"Dry run enabled; skipping execution for task {task.id}")
            task_state["status"] = STATUS_PENDING
            save_state(state_file, state)
            continue

        owner_repo = impl_repo if task.owner in {"codex", "claude"} else test_repo
        healed = heal_stale_git_locks(owner_repo, stale_after_sec=args.git_lock_stale_sec)
        if healed:
            _print(f"Removed stale git locks in {owner_repo}: {', '.join(str(x) for x in healed)}")
        repo_context = get_repo_filetype_context(owner_repo)
        repo_hints = repo_state_hints(owner_repo)
        retry_context = {
            "attempts": task_state.get("attempts", 0),
            "last_summary": task_state.get("last_summary", ""),
            "last_error": task_state.get("last_error", ""),
        }
        handoff_context = render_handoff_context(handoff_dir, task.owner)

        if task.owner == "gemini" and not owner_repo.exists():
            outcome = normalize_outcome(
                {
                    "status": STATUS_BLOCKED,
                    "summary": "Gemini task repository missing",
                    "blocker": f"Test repo does not exist: {owner_repo}",
                    "next_actions": [],
                }
            )
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="agent_error",
                content=outcome["blocker"],
                meta={"agent": "gemini"},
            )
            ok = False
        elif task.owner == "codex":
            task_progress = lambda elapsed: write_heartbeat(
                heartbeat_file,
                phase="task_running",
                cycle=cycle,
                task_id=task.id,
                message=f"task running for {elapsed}s",
                extra={"owner": task.owner},
            )
            ok, outcome = run_codex_task(
                task=task,
                repo=owner_repo,
                objective_text=objective_text,
                schema_path=schema_file,
                output_dir=artifacts_dir,
                codex_cmd=args.codex_cmd,
                codex_model=args.codex_model,
                timeout_sec=args.agent_timeout_sec,
                retry_context=retry_context,
                progress_callback=task_progress,
                repo_context=repo_context,
                repo_hints=repo_hints,
                skill_protocol=skill_protocol,
                mcp_context=mcp_context,
                startup_instructions=codex_startup_instructions,
                handoff_context=handoff_context,
                conversation_log_file=conversation_log_file,
                cycle=cycle,
            )
        elif task.owner == "gemini":
            task_progress = lambda elapsed: write_heartbeat(
                heartbeat_file,
                phase="task_running",
                cycle=cycle,
                task_id=task.id,
                message=f"task running for {elapsed}s",
                extra={"owner": task.owner},
            )
            ok, outcome = run_gemini_task(
                task=task,
                repo=owner_repo,
                objective_text=objective_text,
                gemini_cmd=args.gemini_cmd,
                gemini_model=args.gemini_model,
                timeout_sec=args.agent_timeout_sec,
                retry_context=retry_context,
                progress_callback=task_progress,
                repo_context=repo_context,
                repo_hints=repo_hints,
                skill_protocol=skill_protocol,
                mcp_context=mcp_context,
                startup_instructions=gemini_startup_instructions,
                handoff_context=handoff_context,
                conversation_log_file=conversation_log_file,
                cycle=cycle,
            )
        else:
            task_progress = lambda elapsed: write_heartbeat(
                heartbeat_file,
                phase="task_running",
                cycle=cycle,
                task_id=task.id,
                message=f"task running for {elapsed}s",
                extra={"owner": task.owner},
            )
            ok, outcome = run_claude_task(
                task=task,
                repo=owner_repo,
                objective_text=objective_text,
                claude_cmd=args.claude_cmd,
                claude_model=args.claude_model,
                timeout_sec=args.agent_timeout_sec,
                retry_context=retry_context,
                progress_callback=task_progress,
                repo_context=repo_context,
                repo_hints=repo_hints,
                skill_protocol=skill_protocol,
                mcp_context=mcp_context,
                startup_instructions=claude_startup_instructions,
                handoff_context=handoff_context,
                conversation_log_file=conversation_log_file,
                cycle=cycle,
            )

        summarize_run(task=task, repo=owner_repo, outcome=outcome, report_dir=artifacts_dir)
        record_handoff_event(handoff_dir=handoff_dir, task=task, outcome=outcome)
        status = str(outcome.get("status", STATUS_BLOCKED)).lower()
        blocker_text = str(outcome.get("blocker", ""))
        summary_text = str(outcome.get("summary", ""))

        if not ok or status == STATUS_BLOCKED:
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="task_blocked",
                content=blocker_text or summary_text or "Task blocked",
                meta={"status": status},
            )
            if "lock" in blocker_text.lower() or "another git process" in blocker_text.lower():
                healed_on_failure = heal_stale_git_locks(owner_repo, stale_after_sec=args.git_lock_stale_sec)
                if healed_on_failure:
                    healed_text = ", ".join(str(x) for x in healed_on_failure)
                    blocker_text = f"{blocker_text}\nRecovered stale lock files: {healed_text}"
                    _print(f"Recovered stale git lock(s) after failure: {healed_text}")
            retryable = is_retryable_error(blocker_text)
            attempts = _safe_int(task_state.get("attempts", 0), 0)
            retryable_failures = _safe_int(task_state.get("retryable_failures", 0), 0)

            if retryable and retryable_failures < args.max_retryable_blocked_retries:
                delay = schedule_retry(
                    entry=task_state,
                    summary=summary_text or "Transient blocker encountered.",
                    error=blocker_text,
                    retryable=True,
                    backoff_base_sec=args.retry_backoff_base_sec,
                    backoff_max_sec=args.retry_backoff_max_sec,
                )
                _print(f"Task {task.id} retryable blocker; retry in {delay}s.")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_retry_scheduled",
                    cycle=cycle,
                    task_id=task.id,
                    message=f"retryable blocker; retry in {delay}s",
                    extra={"attempts": attempts, "retryable_failures": task_state["retryable_failures"]},
                )
            elif attempts < args.max_attempts:
                delay = schedule_retry(
                    entry=task_state,
                    summary=summary_text or "Blocked; retrying for autonomous recovery.",
                    error=blocker_text,
                    retryable=False,
                    backoff_base_sec=max(5, min(60, args.retry_backoff_base_sec)),
                    backoff_max_sec=max(60, min(600, args.retry_backoff_max_sec)),
                )
                _print(f"Task {task.id} blocked; retry in {delay}s (attempt {attempts}/{args.max_attempts}).")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_retry_scheduled",
                    cycle=cycle,
                    task_id=task.id,
                    message=f"blocked; retry in {delay}s",
                    extra={"attempts": attempts},
                )
            else:
                mark_blocked(task_state, summary_text or "Task blocked", blocker_text or "agent command failed")
                _print(f"Task {task.id} blocked: {task_state['last_error']}")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_blocked",
                    cycle=cycle,
                    task_id=task.id,
                    message="task marked blocked",
                    extra={"attempts": attempts, "error": task_state["last_error"][:300]},
                )
            save_state(state_file, state)
            continue

        if status == STATUS_DONE:
            validation_repo = owner_repo if task.owner == "gemini" else impl_repo
            valid, details = run_validations(
                repo=validation_repo,
                validate_commands=args.validate_command,
                timeout_sec=args.validate_timeout_sec,
                retries_per_command=args.validation_retries,
                progress_callback=lambda cmd, elapsed: write_heartbeat(
                    heartbeat_file,
                    phase="task_validating",
                    cycle=cycle,
                    task_id=task.id,
                    message=f"validation `{cmd}` running for {elapsed}s",
                ),
            )
            if valid:
                write_heartbeat(
                    heartbeat_file,
                    phase="task_push_verify",
                    cycle=cycle,
                    task_id=task.id,
                    message="verifying commit push state",
                )
                push_ok, push_details = ensure_repo_pushed(owner_repo, timeout_sec=args.validate_timeout_sec)
                if not push_ok:
                    valid = False
                    details = f"Push verification failed:\n{push_details}"
                else:
                    append_conversation_event(
                        conversation_log_file,
                        cycle=cycle,
                        task=task,
                        owner=task.owner,
                        event_type="task_push_verified",
                        content=push_details,
                    )
            if valid:
                task_state["status"] = STATUS_DONE
                task_state["last_error"] = ""
                task_state["last_summary"] = summary_text
                task_state["retryable_failures"] = 0
                task_state["not_before"] = ""
                task_state["last_update"] = _now_iso()
                _print(f"Task {task.id} done.")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_done",
                    cycle=cycle,
                    task_id=task.id,
                    message="task completed and validated",
                )
                append_conversation_event(
                    conversation_log_file,
                    cycle=cycle,
                    task=task,
                    owner=task.owner,
                    event_type="task_done",
                    content=summary_text or "Task completed and validated.",
                )
            else:
                retryable = is_retryable_error(details)
                attempts = _safe_int(task_state.get("attempts", 0), 0)
                if retryable:
                    delay = schedule_retry(
                        entry=task_state,
                        summary="Validation infrastructure failure; retry scheduled.",
                        error=details,
                        retryable=True,
                        backoff_base_sec=args.retry_backoff_base_sec,
                        backoff_max_sec=args.retry_backoff_max_sec,
                    )
                    _print(f"Task {task.id} validation failed transiently; retry in {delay}s.")
                elif attempts < args.max_attempts:
                    delay = schedule_retry(
                        entry=task_state,
                        summary="Validation failed after agent reported done.",
                        error=details,
                        retryable=False,
                        backoff_base_sec=max(5, min(60, args.retry_backoff_base_sec)),
                        backoff_max_sec=max(60, min(600, args.retry_backoff_max_sec)),
                    )
                    _print(
                        f"Task {task.id} validation failed; retry in {delay}s "
                        f"(attempt {attempts}/{args.max_attempts})."
                    )
                else:
                    mark_blocked(task_state, "Validation failed after repeated retries.", details)
                    _print(f"Task {task.id} validation failed and is now blocked.")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_validation",
                    cycle=cycle,
                    task_id=task.id,
                    message="validation processed",
                    extra={"validation_ok": valid},
                )
                append_conversation_event(
                    conversation_log_file,
                    cycle=cycle,
                    task=task,
                    owner=task.owner,
                    event_type="task_validation_failed",
                    content=details,
                )
            save_state(state_file, state)
            continue

        # Partial progress: keep momentum by rescheduling automatically with backoff.
        attempts = _safe_int(task_state.get("attempts", 0), 0)
        if attempts < args.max_attempts:
            delay = schedule_retry(
                entry=task_state,
                summary=summary_text or "Partial progress; retry queued.",
                error=blocker_text,
                retryable=False,
                backoff_base_sec=max(5, min(60, args.retry_backoff_base_sec)),
                backoff_max_sec=max(60, min(600, args.retry_backoff_max_sec)),
            )
            _print(f"Task {task.id} partial; queued for retry in {delay}s.")
            write_heartbeat(
                heartbeat_file,
                phase="task_partial",
                cycle=cycle,
                task_id=task.id,
                message=f"partial; retry in {delay}s",
            )
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="task_partial",
                content=summary_text or "Partial progress; retry queued.",
                meta={"retry_delay_sec": delay},
            )
        else:
            mark_blocked(
                task_state,
                summary_text or "Partial task exceeded max attempts.",
                blocker_text,
            )
            _print(f"Task {task.id} partial result exhausted retries and is now blocked.")
            write_heartbeat(
                heartbeat_file,
                phase="task_blocked",
                cycle=cycle,
                task_id=task.id,
                message="partial retries exhausted",
            )
        save_state(state_file, state)

    _print(f"Reached max cycles: {args.max_cycles}")
    write_heartbeat(
        heartbeat_file,
        phase="max_cycles_reached",
        cycle=args.max_cycles,
        task_id=None,
        message="max cycle limit reached",
    )
    return 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _print("Interrupted by user.")
        raise SystemExit(130) from None
    except Exception as err:  # Defensive guard so supervisors can restart cleanly.
        _print(f"Fatal runner error: {err}")
        raise
