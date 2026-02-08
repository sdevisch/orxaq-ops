#!/usr/bin/env python3
"""Run autonomous multi-agent development cycles for Orxaq.

This runner coordinates:
- Codex for implementation tasks in the main repository.
- Gemini for independent testing/review tasks in a sibling test repository.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
)


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
        if task.owner not in {"codex", "gemini"}:
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


def task_dependencies_done(task: Task, state: dict[str, dict[str, Any]]) -> bool:
    for dep in task.depends_on:
        dep_state = state.get(dep, {})
        if dep_state.get("status") != STATUS_DONE:
            return False
    return True


def _task_ready_now(entry: dict[str, Any], now: dt.datetime) -> bool:
    not_before = _parse_iso(str(entry.get("not_before", "")))
    if not_before is None:
        return True
    return now >= not_before


def select_next_task(tasks: list[Task], state: dict[str, dict[str, Any]], now: dt.datetime | None = None) -> Task | None:
    now = now or _now_utc()
    ready: list[Task] = []
    for task in tasks:
        entry = state[task.id]
        status = str(entry.get("status", STATUS_PENDING))
        if status != STATUS_PENDING:
            continue
        if not _task_ready_now(entry, now):
            continue
        if not task_dependencies_done(task, state):
            continue
        ready.append(task)
    if not ready:
        return None
    owner_rank = {"codex": 0, "gemini": 1}
    ready.sort(key=lambda t: (t.priority, owner_rank[t.owner], t.id))
    return ready[0]


def soonest_pending_time(tasks: list[Task], state: dict[str, dict[str, Any]]) -> dt.datetime | None:
    soonest: dt.datetime | None = None
    for task in tasks:
        entry = state[task.id]
        if entry.get("status") != STATUS_PENDING:
            continue
        if not task_dependencies_done(task, state):
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

    return (
        f"{objective_text.strip()}\n\n"
        "Current autonomous task:\n"
        f"- Task ID: {task.id}\n"
        f"- Title: {task.title}\n"
        f"- Owner role: {role}\n"
        f"- Repository path: {repo_path}\n"
        f"- Description: {task.description}\n"
        f"- Acceptance criteria:\n{acceptance}\n"
        f"{continuation_block}\n"
        "Execution requirements:\n"
        "- Work fully autonomously for this task.\n"
        "- Do not ask for user nudges unless blocked by credentials, destructive actions, or true tradeoff decisions.\n"
        "- Run validation commands: `make lint` then `make test`.\n"
        "- Commit and push contiguous changes.\n"
        "- If a command fails transiently (rate limits/network/timeouts), retry with resilient fallbacks before giving up.\n"
        "- Return ONLY JSON with keys: status, summary, commit, validations, next_actions, blocker.\n"
        "- status must be one of: done, partial, blocked.\n"
    )


def run_command(cmd: list[str], cwd: Path, timeout_sec: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        stdout = err.stdout if isinstance(err.stdout, str) else ""
        stderr = err.stderr if isinstance(err.stderr, str) else ""
        timeout_msg = f"\n[TIMEOUT] command exceeded {timeout_sec}s: {' '.join(cmd)}"
        return subprocess.CompletedProcess(cmd, returncode=124, stdout=stdout, stderr=stderr + timeout_msg)


def run_validations(repo: Path, validate_commands: list[str], timeout_sec: int) -> tuple[bool, str]:
    for raw in validate_commands:
        cmd = shlex.split(raw)
        if not cmd:
            continue
        _print(f"Running validation in {repo}: {raw}")
        result = run_command(cmd, cwd=repo, timeout_sec=timeout_sec)
        if result.returncode != 0:
            details = (result.stdout + "\n" + result.stderr).strip()
            return False, f"Validation failed for `{raw}`:\n{details}"
    return True, "ok"


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
) -> tuple[bool, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{task.id}_codex_result.json"
    prompt = build_agent_prompt(
        task,
        objective_text,
        role="implementation-owner",
        repo_path=repo,
        retry_context=retry_context,
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
    result = run_command(cmd, cwd=repo, timeout_sec=timeout_sec)
    if result.returncode != 0:
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
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Codex produced non-JSON final output",
                "blocker": "Expected JSON object in output-last-message file.",
                "next_actions": [],
            }
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
) -> tuple[bool, dict[str, Any]]:
    prompt = build_agent_prompt(
        task,
        objective_text,
        role="test-owner",
        repo_path=repo,
        retry_context=retry_context,
    )
    prompt += (
        "\nTesting-owner constraints:\n"
        "- Focus on tests/specs/benchmarks and validation depth.\n"
        "- Avoid production code edits unless strictly required to keep tests executable.\n"
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
    result = run_command(cmd, cwd=repo, timeout_sec=timeout_sec)
    if result.returncode != 0:
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


def main() -> int:
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
    parser.add_argument("--codex-model", default=None)
    parser.add_argument("--gemini-model", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    impl_repo = Path(args.impl_repo).resolve()
    test_repo = Path(args.test_repo).resolve()
    tasks_file = Path(args.tasks_file).resolve()
    state_file = Path(args.state_file).resolve()
    objective_file = Path(args.objective_file).resolve()
    schema_file = Path(args.codex_schema).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    heartbeat_file = Path(args.heartbeat_file).resolve()
    lock_file = Path(args.lock_file).resolve()

    if not impl_repo.exists():
        raise FileNotFoundError(f"Implementation repo not found: {impl_repo}")
    if not tasks_file.exists():
        raise FileNotFoundError(f"Task file not found: {tasks_file}")
    if not objective_file.exists():
        raise FileNotFoundError(f"Objective file not found: {objective_file}")
    if not schema_file.exists():
        raise FileNotFoundError(f"Codex schema file not found: {schema_file}")

    ensure_cli_exists(args.codex_cmd, "Codex")
    ensure_cli_exists(args.gemini_cmd, "Gemini")

    lock = RunnerLock(lock_file)
    lock.acquire()
    atexit.register(lock.release)

    tasks = load_tasks(tasks_file)
    state = load_state(state_file, tasks)
    objective_text = _read_text(objective_file)
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
        task = select_next_task(tasks, state, now=now)
        if task is None:
            soonest = soonest_pending_time(tasks, state)
            pending = [t.id for t in tasks if state[t.id]["status"] == STATUS_PENDING]
            blocked = [t.id for t in tasks if state[t.id]["status"] == STATUS_BLOCKED]

            if soonest is not None and soonest > now:
                sleep_for = min(args.idle_sleep_sec, max(1, int((soonest - now).total_seconds())))
                write_heartbeat(
                    heartbeat_file,
                    phase="idle",
                    cycle=cycle,
                    task_id=None,
                    message=f"waiting {sleep_for}s for retry cooldown",
                    extra={"pending": pending, "blocked": blocked},
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
                extra={"pending": pending, "blocked": blocked},
            )
            return 2

        _print(f"Cycle {cycle}: selected task {task.id} ({task.owner})")
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

        owner_repo = impl_repo if task.owner == "codex" else test_repo
        retry_context = {
            "attempts": task_state.get("attempts", 0),
            "last_summary": task_state.get("last_summary", ""),
            "last_error": task_state.get("last_error", ""),
        }

        if task.owner == "gemini" and not owner_repo.exists():
            outcome = normalize_outcome(
                {
                    "status": STATUS_BLOCKED,
                    "summary": "Gemini task repository missing",
                    "blocker": f"Test repo does not exist: {owner_repo}",
                    "next_actions": [],
                }
            )
            ok = False
        elif task.owner == "codex":
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
            )
        else:
            ok, outcome = run_gemini_task(
                task=task,
                repo=owner_repo,
                objective_text=objective_text,
                gemini_cmd=args.gemini_cmd,
                gemini_model=args.gemini_model,
                timeout_sec=args.agent_timeout_sec,
                retry_context=retry_context,
            )

        summarize_run(task=task, repo=owner_repo, outcome=outcome, report_dir=artifacts_dir)
        status = str(outcome.get("status", STATUS_BLOCKED)).lower()
        blocker_text = str(outcome.get("blocker", ""))
        summary_text = str(outcome.get("summary", ""))

        if not ok or status == STATUS_BLOCKED:
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
