#!/usr/bin/env python3
"""Run autonomous multi-agent development cycles for Orxaq.

This runner coordinates:
- Codex for implementation tasks in the main repository.
- Gemini for independent testing/review tasks in a sibling test repository.

It advances a task queue until completion criteria are met or a hard blocker is hit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_BLOCKED = "blocked"
VALID_STATUSES = {STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED}


@dataclass(frozen=True)
class Task:
    id: str
    owner: str
    priority: int
    title: str
    description: str
    depends_on: list[str]
    acceptance: list[str]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _print(msg: str) -> None:
    print(f"[{_now_iso()}] {msg}", flush=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


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
            # Recover from prior interrupted runs without deadlocking the queue.
            status = STATUS_PENDING
        out[task.id] = {
            "status": status,
            "attempts": int(entry.get("attempts", 0)),
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


def select_next_task(tasks: list[Task], state: dict[str, dict[str, Any]]) -> Task | None:
    ready: list[Task] = []
    for task in tasks:
        status = state[task.id]["status"]
        if status != STATUS_PENDING:
            continue
        if not task_dependencies_done(task, state):
            continue
        ready.append(task)
    if not ready:
        return None
    owner_rank = {"codex": 0, "gemini": 1}
    ready.sort(key=lambda t: (t.priority, owner_rank[t.owner], t.id))
    return ready[0]


def build_agent_prompt(task: Task, objective_text: str, role: str, repo_path: Path) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance) or "- No explicit acceptance items"
    return (
        f"{objective_text.strip()}\n\n"
        f"Current autonomous task:\n"
        f"- Task ID: {task.id}\n"
        f"- Title: {task.title}\n"
        f"- Owner role: {role}\n"
        f"- Repository path: {repo_path}\n"
        f"- Description: {task.description}\n"
        f"- Acceptance criteria:\n{acceptance}\n\n"
        "Execution requirements:\n"
        "- Work fully autonomously for this task.\n"
        "- Do not ask for user nudges unless blocked by credentials, destructive actions, or true tradeoff decisions.\n"
        "- Run validation commands: `make lint` then `make test`.\n"
        "- Commit and push contiguous changes.\n"
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
    return None


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
) -> tuple[bool, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{task.id}_codex_result.json"
    prompt = build_agent_prompt(task, objective_text, role="implementation-owner", repo_path=repo)

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
        return False, {
            "status": STATUS_BLOCKED,
            "summary": "Codex command failed",
            "blocker": (result.stdout + "\n" + result.stderr).strip(),
            "next_actions": [],
        }
    parsed = parse_json_text(output_file.read_text(encoding="utf-8")) if output_file.exists() else None
    if parsed is None:
        return False, {
            "status": STATUS_BLOCKED,
            "summary": "Codex produced non-JSON final output",
            "blocker": "Expected JSON object in output-last-message file.",
            "next_actions": [],
        }
    return True, parsed


def run_gemini_task(
    *,
    task: Task,
    repo: Path,
    objective_text: str,
    gemini_cmd: str,
    gemini_model: str | None,
    timeout_sec: int,
) -> tuple[bool, dict[str, Any]]:
    prompt = build_agent_prompt(task, objective_text, role="test-owner", repo_path=repo)
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
        return False, {
            "status": STATUS_BLOCKED,
            "summary": "Gemini command failed",
            "blocker": (result.stdout + "\n" + result.stderr).strip(),
            "next_actions": [],
        }
    parsed = parse_json_text(result.stdout)
    if parsed is None:
        parsed = {
            "status": STATUS_PARTIAL,
            "summary": "Gemini output was not strict JSON; treating as partial",
            "next_actions": [],
            "raw_output": result.stdout.strip(),
        }
    return True, parsed


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
    report_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


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
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--max-attempts", type=int, default=3)
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

    tasks = load_tasks(tasks_file)
    state = load_state(state_file, tasks)
    objective_text = _read_text(objective_file)
    save_state(state_file, state)

    _print(f"Starting autonomy runner with {len(tasks)} tasks")
    for cycle in range(1, args.max_cycles + 1):
        if all(state[t.id]["status"] == STATUS_DONE for t in tasks):
            _print("All tasks are marked done.")
            return 0

        task = select_next_task(tasks, state)
        if task is None:
            pending = [t.id for t in tasks if state[t.id]["status"] == STATUS_PENDING]
            blocked = [t.id for t in tasks if state[t.id]["status"] == STATUS_BLOCKED]
            _print(f"No ready tasks remain. Pending={pending}, Blocked={blocked}")
            return 2

        _print(f"Cycle {cycle}: selected task {task.id} ({task.owner})")
        state[task.id]["status"] = STATUS_IN_PROGRESS
        state[task.id]["last_update"] = _now_iso()
        state[task.id]["attempts"] = int(state[task.id]["attempts"]) + 1
        save_state(state_file, state)

        if args.dry_run:
            _print(f"Dry run enabled; skipping execution for task {task.id}")
            state[task.id]["status"] = STATUS_PENDING
            save_state(state_file, state)
            continue

        owner_repo = impl_repo if task.owner == "codex" else test_repo
        if task.owner == "gemini" and not owner_repo.exists():
            outcome = {
                "status": STATUS_BLOCKED,
                "summary": "Gemini task repository missing",
                "blocker": f"Test repo does not exist: {owner_repo}",
                "next_actions": [],
            }
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
            )
        else:
            ok, outcome = run_gemini_task(
                task=task,
                repo=owner_repo,
                objective_text=objective_text,
                gemini_cmd=args.gemini_cmd,
                gemini_model=args.gemini_model,
                timeout_sec=args.agent_timeout_sec,
            )

        summarize_run(task=task, repo=owner_repo, outcome=outcome, report_dir=artifacts_dir)
        status = str(outcome.get("status", STATUS_BLOCKED)).lower()

        if not ok or status == STATUS_BLOCKED:
            state[task.id]["status"] = STATUS_BLOCKED
            state[task.id]["last_error"] = str(outcome.get("blocker", "agent command failed"))
            state[task.id]["last_summary"] = str(outcome.get("summary", ""))
            state[task.id]["last_update"] = _now_iso()
            save_state(state_file, state)
            _print(f"Task {task.id} blocked: {state[task.id]['last_error']}")
            continue

        if status == STATUS_DONE:
            valid, details = run_validations(
                repo=owner_repo if task.owner == "gemini" else impl_repo,
                validate_commands=args.validate_command,
                timeout_sec=args.validate_timeout_sec,
            )
            if valid:
                state[task.id]["status"] = STATUS_DONE
                state[task.id]["last_error"] = ""
                state[task.id]["last_summary"] = str(outcome.get("summary", ""))
                state[task.id]["last_update"] = _now_iso()
                _print(f"Task {task.id} done.")
            else:
                attempts = int(state[task.id]["attempts"])
                state[task.id]["status"] = STATUS_BLOCKED if attempts >= args.max_attempts else STATUS_PENDING
                state[task.id]["last_error"] = details
                state[task.id]["last_summary"] = "Validation failed after agent reported done."
                state[task.id]["last_update"] = _now_iso()
                _print(f"Task {task.id} validation failed.")
            save_state(state_file, state)
            continue

        attempts = int(state[task.id]["attempts"])
        state[task.id]["status"] = STATUS_BLOCKED if attempts >= args.max_attempts else STATUS_PENDING
        state[task.id]["last_error"] = str(outcome.get("blocker", ""))
        state[task.id]["last_summary"] = str(outcome.get("summary", "partial progress"))
        state[task.id]["last_update"] = _now_iso()
        save_state(state_file, state)
        _print(f"Task {task.id} partial; queued for retry.")

    _print(f"Reached max cycles: {args.max_cycles}")
    return 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _print("Interrupted by user.")
        raise SystemExit(130) from None
