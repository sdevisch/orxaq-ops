"""Cross-platform autonomy supervisor and lifecycle manager."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_DEFAULT_TOOL_DIRS: tuple[Path, ...] = (
    # Homebrew (Apple Silicon)
    Path("/opt/homebrew/bin"),
    # Homebrew (Intel) + common unix installs
    Path("/usr/local/bin"),
)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _log(msg: str) -> None:
    print(f"[{_now_iso()}] {msg}", flush=True)


SECRET_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*([\"'])?[^\\s,\"']+\\2?",
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
        "[REDACTED_OPENAI_KEY]",
    ),
)


def sanitize_text(value: str) -> str:
    text = str(value)
    for pattern, replacement in SECRET_REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_repo_slug(remote_url: str) -> str:
    cleaned = remote_url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if cleaned.startswith("git@github.com:"):
        return cleaned.split("git@github.com:", 1)[1]
    if "github.com/" in cleaned:
        return cleaned.split("github.com/", 1)[1]
    return ""


def _repo_slug(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return _parse_repo_slug(result.stdout)


def _repo_branch(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _detect_health_score(config: ManagerConfig) -> int | None:
    candidates = [
        config.impl_repo / "artifacts" / "health.json",
        config.artifacts_dir / "health.json",
    ]
    for path in candidates:
        payload = _read_json_dict(path)
        score = payload.get("score")
        if isinstance(score, int):
            return score
    return None


def _select_last_task(state_payload: dict[str, Any]) -> dict[str, Any]:
    best_task: dict[str, Any] = {}
    best_key: tuple[int, str, int] = (0, "", -1)
    for task_id, raw in state_payload.items():
        if not isinstance(raw, dict):
            continue
        last_update = str(raw.get("last_update", "")).strip()
        attempts = int(raw.get("attempts", 0) or 0)
        key = (1 if last_update else 0, last_update, attempts)
        if key <= best_key:
            continue
        best_key = key
        best_task = {
            "task_id": str(task_id),
            "status": str(raw.get("status", "")).strip(),
            "attempts": attempts,
            "last_update": last_update,
            "last_summary": sanitize_text(str(raw.get("last_summary", "")).strip()),
            "last_error": sanitize_text(str(raw.get("last_error", "")).strip()),
        }
    return best_task


def _detect_last_ci_failure(config: ManagerConfig) -> dict[str, str]:
    repo_slug = _repo_slug(config.root_dir)
    branch = _repo_branch(config.root_dir)
    if not repo_slug or not branch:
        return {}

    pr_list = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo_slug,
            "--head",
            branch,
            "--json",
            "number,url,state",
            "--limit",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if pr_list.returncode != 0:
        return {}
    try:
        payload = json.loads(pr_list.stdout)
    except Exception:
        return {}
    if not isinstance(payload, list) or not payload:
        return {}
    first = payload[0] if isinstance(payload[0], dict) else {}
    pr_number = str(first.get("number", "")).strip()
    pr_url = str(first.get("url", "")).strip()
    if not pr_number:
        return {}

    checks = subprocess.run(
        ["gh", "pr", "checks", pr_number, "--repo", repo_slug],
        capture_output=True,
        text=True,
        check=False,
    )
    output = checks.stdout.strip()
    if not output:
        return {"pr_number": pr_number, "pr_url": pr_url}

    for line in output.splitlines():
        parts = [chunk.strip() for chunk in line.split("\t")]
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1].lower()
        if status not in {"fail", "cancel", "timed_out", "action_required"}:
            continue
        details_url = parts[3] if len(parts) >= 4 else ""
        return {
            "pr_number": pr_number,
            "pr_url": pr_url,
            "check_name": sanitize_text(name),
            "check_status": status,
            "details_url": details_url,
        }

    return {"pr_number": pr_number, "pr_url": pr_url}


def _suggest_smallest_fix_path(last_task: dict[str, Any], ci_failure: dict[str, str]) -> str:
    if ci_failure.get("check_name"):
        return (
            "Reproduce the failing CI check locally, patch the smallest failing unit, "
            "rerun the targeted command, then rerun full lint/test."
        )
    if last_task.get("task_id"):
        return (
            f"Resume from task `{last_task['task_id']}` using its last error/summary, "
            "apply the smallest scoped fix, then rerun validations."
        )
    return "Run `make preflight`, identify first hard failure, and patch only that blocker."


def build_stop_report_payload(config: ManagerConfig, *, reason: str) -> dict[str, Any]:
    state_payload = _read_json_dict(config.state_file)
    status_payload = status_snapshot(config)
    last_task = _select_last_task(state_payload)
    ci_failure = _detect_last_ci_failure(config)
    health_score = _detect_health_score(config)
    return {
        "generated_at": _now_iso(),
        "reason": sanitize_text(reason),
        "repo": str(config.root_dir),
        "branch": _repo_branch(config.root_dir),
        "health_score": health_score,
        "status": status_payload,
        "last_task": last_task,
        "last_ci_failure": ci_failure,
        "suggested_smallest_fix_path": _suggest_smallest_fix_path(last_task, ci_failure),
        "artifacts": {
            "state_file": str(config.state_file),
            "log_file": str(config.log_file),
            "heartbeat_file": str(config.heartbeat_file),
            "budget_report": str(config.budget_report_file),
        },
    }


def render_stop_report_markdown(payload: dict[str, Any]) -> str:
    last_task = payload.get("last_task", {}) if isinstance(payload.get("last_task"), dict) else {}
    ci_failure = payload.get("last_ci_failure", {}) if isinstance(payload.get("last_ci_failure"), dict) else {}
    artifacts = payload.get("artifacts", {}) if isinstance(payload.get("artifacts"), dict) else {}
    health_score = payload.get("health_score")
    health_display = "unknown" if health_score is None else str(health_score)
    lines = [
        "# AUTONOMY STOP REPORT",
        "",
        f"- generated_at: `{payload.get('generated_at', '')}`",
        f"- reason: `{payload.get('reason', '')}`",
        f"- repo: `{payload.get('repo', '')}`",
        f"- branch: `{payload.get('branch', '')}`",
        f"- health_score: `{health_display}`",
        "",
        "## Last Executed Task",
        "",
        f"- task_id: `{last_task.get('task_id', '')}`",
        f"- status: `{last_task.get('status', '')}`",
        f"- attempts: `{last_task.get('attempts', 0)}`",
        f"- last_update: `{last_task.get('last_update', '')}`",
        f"- last_summary: `{last_task.get('last_summary', '')}`",
        f"- last_error: `{last_task.get('last_error', '')}`",
        "",
        "## Last CI Failure",
        "",
        f"- pr_url: `{ci_failure.get('pr_url', '')}`",
        f"- check_name: `{ci_failure.get('check_name', '')}`",
        f"- check_status: `{ci_failure.get('check_status', '')}`",
        f"- details_url: `{ci_failure.get('details_url', '')}`",
        "",
        "## Suggested Smallest Fix Path",
        "",
        payload.get("suggested_smallest_fix_path", ""),
        "",
        "## Artifacts",
        "",
        f"- state_file: `{artifacts.get('state_file', '')}`",
        f"- log_file: `{artifacts.get('log_file', '')}`",
        f"- heartbeat_file: `{artifacts.get('heartbeat_file', '')}`",
        f"- budget_report: `{artifacts.get('budget_report', '')}`",
        "",
    ]
    return "\n".join(lines)


def build_stop_issue_payload(
    config: ManagerConfig,
    *,
    report_payload: dict[str, Any],
    report_path: Path,
    issue_repo: str = "",
    issue_title: str = "",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    repo_slug = issue_repo.strip() or _repo_slug(config.root_dir)
    ts = _now_utc().strftime("%Y-%m-%d %H:%M UTC")
    title = issue_title.strip() or f"AUTONOMY STOP: {config.root_dir.name} ({ts})"
    body = "\n".join(
        [
            "Autonomy run stopped and requires intervention.",
            "",
            f"- reason: `{report_payload.get('reason', '')}`",
            f"- health_score: `{report_payload.get('health_score', 'unknown')}`",
            f"- last_task: `{(report_payload.get('last_task') or {}).get('task_id', '')}`",
            f"- ci_failure: `{(report_payload.get('last_ci_failure') or {}).get('check_name', '')}`",
            "",
            f"Stop report: `{report_path}`",
            "",
            "Suggested smallest fix path:",
            report_payload.get("suggested_smallest_fix_path", ""),
        ]
    )
    sanitized_body = sanitize_text(body)
    cleaned_labels = [sanitize_text(lbl).strip() for lbl in (labels or []) if str(lbl).strip()]
    return {
        "repo_slug": sanitize_text(repo_slug),
        "title": sanitize_text(title),
        "body": sanitized_body,
        "labels": cleaned_labels,
    }


def _file_stop_issue(issue_payload: dict[str, Any]) -> str:
    if not issue_payload.get("repo_slug"):
        return ""
    cmd = [
        "gh",
        "issue",
        "create",
        "--repo",
        str(issue_payload["repo_slug"]),
        "--title",
        str(issue_payload["title"]),
        "--body",
        str(issue_payload["body"]),
    ]
    for label in issue_payload.get("labels", []):
        cmd.extend(["--label", str(label)])
    created = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if created.returncode != 0:
        return ""
    return created.stdout.strip().splitlines()[-1].strip()


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def _acquire_process_lock(path: Path) -> Any | None:
    """Acquire a non-blocking, cross-platform single-process lock.

    Returns a handle that must be passed to `_release_process_lock` or None when locked.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def _release_process_lock(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def _pid_running(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int, *, force_after_sec: int = 2) -> None:
    if not _pid_running(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + force_after_sec
    while time.time() < deadline:
        if not _pid_running(pid):
            return
        time.sleep(0.1)
    if _pid_running(pid):
        os.kill(pid, signal.SIGKILL)


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()
        key, value = raw.split("=", 1)
        data[key.strip()] = value.strip().strip("\"").strip("'")
    return data


def _has_codex_auth(env: dict[str, str]) -> bool:
    if env.get("OPENAI_API_KEY") and env.get("OPENAI_API_KEY") != "replace_me":
        return True
    status = subprocess.run(["codex", "login", "status"], check=False, capture_output=True)
    return status.returncode == 0


def _has_gemini_auth(env: dict[str, str]) -> bool:
    if env.get("GEMINI_API_KEY") and env.get("GEMINI_API_KEY") != "replace_me":
        return True
    if env.get("GOOGLE_GENAI_USE_VERTEXAI") == "true" or env.get("GOOGLE_GENAI_USE_GCA") == "true":
        return True
    settings = Path.home() / ".gemini" / "settings.json"
    if settings.exists() and '"selectedType"' in settings.read_text(encoding="utf-8"):
        return True
    return False


def _runtime_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if base:
        env.update(base)
    env.setdefault("CI", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("PIP_NO_INPUT", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("NO_COLOR", "1")
    return env


def _safe_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        try:
            return int(getuid())
        except Exception:
            return 0
    return 0


@dataclass(frozen=True)
class ManagerConfig:
    root_dir: Path
    env_file: Path
    impl_repo: Path
    test_repo: Path
    tasks_file: Path
    state_file: Path
    objective_file: Path
    schema_file: Path
    artifacts_dir: Path
    heartbeat_file: Path
    lock_file: Path
    checkpoint_dir: Path
    runner_pid_file: Path
    supervisor_pid_file: Path
    log_file: Path
    run_id: str
    resume_run_id: str
    max_cycles: int
    max_attempts: int
    max_retryable_blocked_retries: int
    retry_backoff_base_sec: int
    retry_backoff_max_sec: int
    git_lock_stale_sec: int
    validation_retries: int
    idle_sleep_sec: int
    agent_timeout_sec: int
    validate_timeout_sec: int
    max_runtime_sec: int
    max_total_tokens: int
    max_total_cost_usd: float
    max_total_retries: int
    budget_report_file: Path
    heartbeat_poll_sec: int
    heartbeat_stale_sec: int
    supervisor_restart_delay_sec: int
    supervisor_max_backoff_sec: int
    supervisor_max_restarts: int
    validate_commands: list[str]
    skill_protocol_file: Path
    mcp_context_file: Path | None

    @classmethod
    def from_root(cls, root: Path, env_file_override: Path | None = None) -> "ManagerConfig":
        root = root.resolve()
        env_file = env_file_override.resolve() if env_file_override else Path(
            os.environ.get("ORXAQ_AUTONOMY_ENV_FILE", str(root / ".env.autonomy"))
        ).resolve()
        from_file = _load_env_file(env_file)
        merged = {**from_file, **os.environ}

        def _path(key: str, default: Path) -> Path:
            return Path(merged.get(key, str(default))).resolve()

        def _int(key: str, default: int) -> int:
            raw = merged.get(key)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def _float(key: str, default: float) -> float:
            raw = merged.get(key)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        artifacts = _path("ORXAQ_AUTONOMY_ARTIFACTS_DIR", root / "artifacts" / "autonomy")
        skill_protocol = _path("ORXAQ_AUTONOMY_SKILL_PROTOCOL_FILE", root / "config" / "skill_protocol.json")
        mcp_context_raw = merged.get("ORXAQ_AUTONOMY_MCP_CONTEXT_FILE", "").strip()
        mcp_context = Path(mcp_context_raw).resolve() if mcp_context_raw else None

        validate_raw = merged.get("ORXAQ_AUTONOMY_VALIDATE_COMMANDS", "make lint;make test")
        validate_commands = [chunk.strip() for chunk in validate_raw.split(";") if chunk.strip()]

        return cls(
            root_dir=root,
            env_file=env_file,
            impl_repo=_path("ORXAQ_IMPL_REPO", root / "../orxaq"),
            test_repo=_path("ORXAQ_TEST_REPO", root / "../orxaq_gemini"),
            tasks_file=_path("ORXAQ_AUTONOMY_TASKS_FILE", root / "config" / "tasks.json"),
            state_file=_path("ORXAQ_AUTONOMY_STATE_FILE", root / "state" / "state.json"),
            objective_file=_path("ORXAQ_AUTONOMY_OBJECTIVE_FILE", root / "config" / "objective.md"),
            schema_file=_path("ORXAQ_AUTONOMY_SCHEMA_FILE", root / "config" / "codex_result.schema.json"),
            artifacts_dir=artifacts,
            heartbeat_file=_path("ORXAQ_AUTONOMY_HEARTBEAT_FILE", artifacts / "heartbeat.json"),
            lock_file=_path("ORXAQ_AUTONOMY_LOCK_FILE", artifacts / "runner.lock"),
            checkpoint_dir=_path("ORXAQ_AUTONOMY_CHECKPOINT_DIR", root / "artifacts" / "checkpoints"),
            runner_pid_file=_path("ORXAQ_AUTONOMY_RUNNER_PID_FILE", artifacts / "runner.pid"),
            supervisor_pid_file=_path("ORXAQ_AUTONOMY_SUPERVISOR_PID_FILE", artifacts / "supervisor.pid"),
            log_file=_path("ORXAQ_AUTONOMY_LOG_FILE", artifacts / "runner.log"),
            run_id=merged.get("ORXAQ_AUTONOMY_RUN_ID", "").strip(),
            resume_run_id=merged.get("ORXAQ_AUTONOMY_RESUME_RUN_ID", "").strip(),
            max_cycles=_int("ORXAQ_AUTONOMY_MAX_CYCLES", 10000),
            max_attempts=_int("ORXAQ_AUTONOMY_MAX_ATTEMPTS", 8),
            max_retryable_blocked_retries=_int("ORXAQ_AUTONOMY_MAX_RETRYABLE_BLOCKED_RETRIES", 20),
            retry_backoff_base_sec=_int("ORXAQ_AUTONOMY_RETRY_BACKOFF_BASE_SEC", 30),
            retry_backoff_max_sec=_int("ORXAQ_AUTONOMY_RETRY_BACKOFF_MAX_SEC", 1800),
            git_lock_stale_sec=_int("ORXAQ_AUTONOMY_GIT_LOCK_STALE_SEC", 300),
            validation_retries=_int("ORXAQ_AUTONOMY_VALIDATION_RETRIES", 1),
            idle_sleep_sec=_int("ORXAQ_AUTONOMY_IDLE_SLEEP_SEC", 10),
            agent_timeout_sec=_int("ORXAQ_AUTONOMY_AGENT_TIMEOUT_SEC", 3600),
            validate_timeout_sec=_int("ORXAQ_AUTONOMY_VALIDATE_TIMEOUT_SEC", 1800),
            max_runtime_sec=_int("ORXAQ_AUTONOMY_MAX_RUNTIME_SEC", 0),
            max_total_tokens=_int("ORXAQ_AUTONOMY_MAX_TOTAL_TOKENS", 0),
            max_total_cost_usd=_float("ORXAQ_AUTONOMY_MAX_TOTAL_COST_USD", 0.0),
            max_total_retries=_int("ORXAQ_AUTONOMY_MAX_TOTAL_RETRIES", 0),
            budget_report_file=_path("ORXAQ_AUTONOMY_BUDGET_REPORT_FILE", artifacts / "budget.json"),
            heartbeat_poll_sec=_int("ORXAQ_AUTONOMY_HEARTBEAT_POLL_SEC", 20),
            heartbeat_stale_sec=_int("ORXAQ_AUTONOMY_HEARTBEAT_STALE_SEC", 300),
            supervisor_restart_delay_sec=_int("ORXAQ_AUTONOMY_SUPERVISOR_RESTART_DELAY_SEC", 5),
            supervisor_max_backoff_sec=_int("ORXAQ_AUTONOMY_SUPERVISOR_MAX_BACKOFF_SEC", 300),
            supervisor_max_restarts=_int("ORXAQ_AUTONOMY_SUPERVISOR_MAX_RESTARTS", 0),
            validate_commands=validate_commands,
            skill_protocol_file=skill_protocol,
            mcp_context_file=mcp_context,
        )


def ensure_runtime(config: ManagerConfig) -> None:
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_tool(tool: str) -> None:
        if shutil.which(tool) is not None:
            return
        current = os.environ.get("PATH", "")
        parts = [p for p in current.split(os.pathsep) if p]
        for tool_dir in _DEFAULT_TOOL_DIRS:
            candidate = tool_dir / tool
            if os.name == "nt":
                candidate = candidate.with_suffix(".exe")
            if candidate.exists():
                tool_dir_str = str(tool_dir)
                if tool_dir_str not in parts:
                    os.environ["PATH"] = tool_dir_str + (os.pathsep + current if current else "")
                return
        raise RuntimeError(f"{tool} CLI not found in PATH")

    _ensure_tool("codex")
    _ensure_tool("gemini")
    env = _load_env_file(config.env_file)
    if not _has_codex_auth(env):
        raise RuntimeError("Codex auth missing. Configure OPENAI_API_KEY or run `codex login`.")
    if not _has_gemini_auth(env):
        raise RuntimeError("Gemini auth missing. Configure GEMINI_API_KEY or ~/.gemini/settings.json.")


def _repo_is_clean(repo: Path) -> tuple[bool, str]:
    if not repo.exists():
        return False, f"missing repository: {repo}"
    inside = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if inside.returncode != 0:
        return False, f"not a git repo: {repo}"
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    if dirty.returncode != 0:
        return False, f"unable to inspect git status: {repo}"
    if dirty.stdout.strip():
        return False, f"repo has local changes: {repo}"
    return True, "ok"


def preflight(config: ManagerConfig, *, require_clean: bool = True) -> dict[str, Any]:
    ensure_runtime(config)
    result: dict[str, Any] = {
        "runtime": "ok",
        "impl_repo": str(config.impl_repo),
        "test_repo": str(config.test_repo),
        "clean": True,
        "checks": [],
    }
    if require_clean:
        for repo in (config.impl_repo, config.test_repo):
            ok, message = _repo_is_clean(repo)
            result["checks"].append({"repo": str(repo), "ok": ok, "message": message})
            if not ok:
                result["clean"] = False
    return result


def runner_argv(config: ManagerConfig) -> list[str]:
    args: list[str] = [
        "--impl-repo",
        str(config.impl_repo),
        "--test-repo",
        str(config.test_repo),
        "--tasks-file",
        str(config.tasks_file),
        "--state-file",
        str(config.state_file),
        "--objective-file",
        str(config.objective_file),
        "--codex-schema",
        str(config.schema_file),
        "--artifacts-dir",
        str(config.artifacts_dir),
        "--heartbeat-file",
        str(config.heartbeat_file),
        "--lock-file",
        str(config.lock_file),
        "--checkpoint-dir",
        str(config.checkpoint_dir),
        "--max-cycles",
        str(config.max_cycles),
        "--max-attempts",
        str(config.max_attempts),
        "--max-retryable-blocked-retries",
        str(config.max_retryable_blocked_retries),
        "--retry-backoff-base-sec",
        str(config.retry_backoff_base_sec),
        "--retry-backoff-max-sec",
        str(config.retry_backoff_max_sec),
        "--git-lock-stale-sec",
        str(config.git_lock_stale_sec),
        "--validation-retries",
        str(config.validation_retries),
        "--idle-sleep-sec",
        str(config.idle_sleep_sec),
        "--agent-timeout-sec",
        str(config.agent_timeout_sec),
        "--validate-timeout-sec",
        str(config.validate_timeout_sec),
        "--max-runtime-sec",
        str(config.max_runtime_sec),
        "--max-total-tokens",
        str(config.max_total_tokens),
        "--max-total-cost-usd",
        str(config.max_total_cost_usd),
        "--max-total-retries",
        str(config.max_total_retries),
        "--budget-report-file",
        str(config.budget_report_file),
        "--skill-protocol-file",
        str(config.skill_protocol_file),
    ]
    if config.mcp_context_file is not None:
        args.extend(["--mcp-context-file", str(config.mcp_context_file)])
    if config.run_id:
        args.extend(["--run-id", config.run_id])
    if config.resume_run_id:
        args.extend(["--resume", config.resume_run_id])
    for cmd in config.validate_commands:
        args.extend(["--validate-command", cmd])
    return args


def _heartbeat_age_sec(config: ManagerConfig) -> int:
    if not config.heartbeat_file.exists():
        return -1
    try:
        raw = json.loads(config.heartbeat_file.read_text(encoding="utf-8"))
        ts = str(raw.get("timestamp", "")).strip()
        if not ts:
            return -1
        parsed = dt.datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int((_now_utc() - parsed).total_seconds())
    except Exception:
        return -1


def run_foreground(config: ManagerConfig) -> int:
    ensure_runtime(config)
    from . import runner

    _write_pid(config.runner_pid_file, os.getpid())
    try:
        return runner.main(runner_argv(config))
    finally:
        config.runner_pid_file.unlink(missing_ok=True)


def supervise_foreground(config: ManagerConfig) -> int:
    ensure_runtime(config)
    supervisor_lock_file = Path(
        os.environ.get(
            "ORXAQ_AUTONOMY_SUPERVISOR_LOCK_FILE",
            str(config.artifacts_dir / "supervisor.lock"),
        )
    ).resolve()
    lock_handle = _acquire_process_lock(supervisor_lock_file)
    if lock_handle is None:
        existing_pid = _read_pid(config.supervisor_pid_file)
        if _pid_running(existing_pid):
            _log(f"autonomy supervisor already running (pid={existing_pid})")
        else:
            _log("autonomy supervisor already running (lock held)")
        return 0

    _write_pid(config.supervisor_pid_file, os.getpid())
    restart_count = 0
    backoff = max(1, config.supervisor_restart_delay_sec)

    try:
        while True:
            with config.log_file.open("a", encoding="utf-8") as log:
                log.write(f"[{_now_iso()}] supervisor: launching runner\n")
                log.flush()
                child = subprocess.Popen(
                    [sys.executable, "-m", "orxaq_autonomy.cli", "--root", str(config.root_dir), "run"],
                    cwd=str(config.root_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=_runtime_env(_load_env_file(config.env_file)),
                )
            _write_pid(config.runner_pid_file, child.pid)

            while _pid_running(child.pid):
                time.sleep(max(1, config.heartbeat_poll_sec))
                age = _heartbeat_age_sec(config)
                if age != -1 and age > config.heartbeat_stale_sec:
                    with config.log_file.open("a", encoding="utf-8") as log:
                        log.write(
                            f"[{_now_iso()}] supervisor: stale heartbeat ({age}s), restarting runner pid={child.pid}\n"
                        )
                    _terminate_pid(child.pid)
                    break

            rc = child.wait()
            config.runner_pid_file.unlink(missing_ok=True)

            if rc == 0:
                with config.log_file.open("a", encoding="utf-8") as log:
                    log.write(f"[{_now_iso()}] supervisor: runner exited cleanly\n")
                return 0

            restart_count += 1
            with config.log_file.open("a", encoding="utf-8") as log:
                log.write(f"[{_now_iso()}] supervisor: runner rc={rc}; restart={restart_count}\n")

            if config.supervisor_max_restarts > 0 and restart_count >= config.supervisor_max_restarts:
                return rc

            time.sleep(backoff)
            backoff = min(config.supervisor_max_backoff_sec, backoff * 2)
    finally:
        config.supervisor_pid_file.unlink(missing_ok=True)
        _release_process_lock(lock_handle)


def start_background(config: ManagerConfig) -> None:
    if _pid_running(_read_pid(config.supervisor_pid_file)):
        _log(f"autonomy supervisor already running (pid={_read_pid(config.supervisor_pid_file)})")
        return
    ensure_runtime(config)
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_handle = config.log_file.open("a", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "cwd": str(config.root_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
        "env": _runtime_env(_load_env_file(config.env_file)),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "orxaq_autonomy.cli", "--root", str(config.root_dir), "supervise"],
            **kwargs,
        )
    finally:
        log_handle.close()
    _log(f"autonomy supervisor spawned (pid={proc.pid})")


def stop_background(config: ManagerConfig) -> None:
    supervisor_pid = _read_pid(config.supervisor_pid_file)
    runner_pid = _read_pid(config.runner_pid_file)
    if supervisor_pid:
        _terminate_pid(supervisor_pid)
    if runner_pid:
        _terminate_pid(runner_pid)
    config.supervisor_pid_file.unlink(missing_ok=True)
    config.runner_pid_file.unlink(missing_ok=True)
    _log("autonomy supervisor stopped")


def autonomy_stop(
    config: ManagerConfig,
    *,
    reason: str,
    file_issue: bool = False,
    issue_repo: str = "",
    issue_title: str = "",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    stop_background(config)
    report_payload = build_stop_report_payload(config, reason=reason)
    report_path = config.artifacts_dir / "AUTONOMY_STOP_REPORT.md"
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_stop_report_markdown(report_payload), encoding="utf-8")

    issue_url = ""
    issue_payload = build_stop_issue_payload(
        config,
        report_payload=report_payload,
        report_path=report_path,
        issue_repo=issue_repo,
        issue_title=issue_title,
        labels=labels,
    )
    if file_issue:
        issue_url = _file_stop_issue(issue_payload)

    return {
        "ok": True,
        "report_path": str(report_path),
        "issue_url": issue_url,
        "issue_payload": issue_payload,
        "stop_report": report_payload,
    }


def ensure_background(config: ManagerConfig) -> None:
    supervisor_lock_file = Path(
        os.environ.get(
            "ORXAQ_AUTONOMY_SUPERVISOR_LOCK_FILE",
            str(config.artifacts_dir / "supervisor.lock"),
        )
    ).resolve()
    lock_handle = _acquire_process_lock(supervisor_lock_file)
    if lock_handle is None:
        _log("autonomy supervisor already running (lock held)")
        return
    _release_process_lock(lock_handle)

    supervisor_pid = _read_pid(config.supervisor_pid_file)
    if not _pid_running(supervisor_pid):
        _log("autonomy supervisor not running; starting")
        start_background(config)
        return

    runner_pid = _read_pid(config.runner_pid_file)
    age = _heartbeat_age_sec(config)
    if runner_pid and _pid_running(runner_pid) and age != -1 and age > config.heartbeat_stale_sec:
        _log(f"runner heartbeat stale ({age}s); restarting runner pid={runner_pid}")
        _terminate_pid(runner_pid)
    else:
        _log("autonomy supervisor ensured")


def dashboard_ensure(
    config: ManagerConfig,
    *,
    artifacts_root: Path,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> dict[str, Any]:
    pid_file = config.artifacts_dir / "dashboard.pid"
    log_file = config.artifacts_dir / "dashboard.log"
    existing_pid = _read_pid(pid_file)
    url = f"http://{host}:{int(port)}/"
    if _pid_running(existing_pid):
        return {
            "ok": True,
            "running": True,
            "pid": existing_pid,
            "pid_file": str(pid_file),
            "log_file": str(log_file),
            "url": url,
        }

    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_handle = log_file.open("a", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "cwd": str(config.root_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
        "env": _runtime_env(_load_env_file(config.env_file)),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "orxaq_autonomy.cli",
                "--root",
                str(config.root_dir),
                "dashboard",
                "--artifacts-dir",
                str(artifacts_root.resolve()),
                "--host",
                str(host),
                "--port",
                str(int(port)),
            ],
            **kwargs,
        )
    finally:
        log_handle.close()

    _write_pid(pid_file, proc.pid)
    return {
        "ok": True,
        "running": True,
        "pid": proc.pid,
        "pid_file": str(pid_file),
        "log_file": str(log_file),
        "url": url,
    }


def status_snapshot(config: ManagerConfig) -> dict[str, Any]:
    supervisor_pid = _read_pid(config.supervisor_pid_file)
    runner_pid = _read_pid(config.runner_pid_file)
    age = _heartbeat_age_sec(config)
    return {
        "supervisor_running": _pid_running(supervisor_pid),
        "supervisor_pid": supervisor_pid,
        "runner_running": _pid_running(runner_pid),
        "runner_pid": runner_pid,
        "heartbeat_age_sec": age,
        "heartbeat_stale_threshold_sec": config.heartbeat_stale_sec,
        "state_file": str(config.state_file),
        "log_file": str(config.log_file),
    }


def health_snapshot(config: ManagerConfig) -> dict[str, Any]:
    state_counts = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 0}
    blocked_tasks: list[str] = []

    if config.state_file.exists():
        try:
            raw = json.loads(config.state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for task_id, item in raw.items():
                    status = "unknown"
                    if isinstance(item, dict):
                        status = str(item.get("status", "unknown")).strip().lower()
                    if status in state_counts:
                        state_counts[status] += 1
                    else:
                        state_counts["unknown"] += 1
                    if status == "blocked":
                        blocked_tasks.append(str(task_id))
        except Exception:
            state_counts["unknown"] += 1

    status = status_snapshot(config)
    heartbeat_age = int(status.get("heartbeat_age_sec", -1))
    stale = heartbeat_age != -1 and heartbeat_age > config.heartbeat_stale_sec
    out = {
        "timestamp": _now_iso(),
        "status": status,
        "state_counts": state_counts,
        "blocked_tasks": blocked_tasks,
        "heartbeat_stale": stale,
    }
    if config.budget_report_file.exists():
        try:
            out["budget"] = json.loads(config.budget_report_file.read_text(encoding="utf-8"))
        except Exception:
            out["budget"] = {"error": "invalid_budget_report"}
    health_file = config.artifacts_dir / "health.json"
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    health_file.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    out["health_file"] = str(health_file)
    return out


def install_keepalive(config: ManagerConfig) -> str:
    if os.name == "nt":
        task_name = "OrxaqAutonomyEnsure"
        command = f'"{sys.executable}" -m orxaq_autonomy.cli --root "{config.root_dir}" ensure'
        cmd = [
            "schtasks",
            "/Create",
            "/F",
            "/SC",
            "MINUTE",
            "/MO",
            "1",
            "/TN",
            task_name,
            "/TR",
            command,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stdout + "\n" + result.stderr)
        return task_name

    if sys.platform == "darwin":
        label = "com.orxaq.autonomy.ensure"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        log_file = config.artifacts_dir / "ensure.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        env_path = os.environ.get("PATH", "/usr/bin:/bin")
        env_path = env_path.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        payload = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>{label}</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>{env_path}</string>
  </dict>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string>
    <string>-m</string>
    <string>orxaq_autonomy.cli</string>
    <string>--root</string>
    <string>{config.root_dir}</string>
    <string>ensure</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>{log_file}</string>
  <key>StandardErrorPath</key><string>{log_file}</string>
</dict></plist>
"""
        plist.write_text(payload, encoding="utf-8")
        uid = _safe_uid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
        load = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=False, capture_output=True)
        if load.returncode != 0:
            raise RuntimeError(load.stdout.decode() + load.stderr.decode())
        return label

    raise RuntimeError("Automatic keepalive install currently supports Windows Task Scheduler and macOS launchd.")


def uninstall_keepalive(config: ManagerConfig) -> str:
    if os.name == "nt":
        task_name = "OrxaqAutonomyEnsure"
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], check=False, capture_output=True)
        return task_name
    if sys.platform == "darwin":
        label = "com.orxaq.autonomy.ensure"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        uid = _safe_uid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
        plist.unlink(missing_ok=True)
        return label
    raise RuntimeError("Automatic keepalive uninstall currently supports Windows and macOS only.")


def keepalive_status(config: ManagerConfig) -> dict[str, Any]:
    if os.name == "nt":
        task_name = "OrxaqAutonomyEnsure"
        result = subprocess.run(["schtasks", "/Query", "/TN", task_name], check=False, capture_output=True, text=True)
        return {"platform": "windows", "task_name": task_name, "active": result.returncode == 0}
    if sys.platform == "darwin":
        label = "com.orxaq.autonomy.ensure"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        uid = _safe_uid()
        result = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], check=False, capture_output=True)
        return {"platform": "macos", "label": label, "active": result.returncode == 0, "plist": str(plist)}
    return {"platform": sys.platform, "active": False, "note": "No native keepalive integration for this platform."}


def reset_state(config: ManagerConfig) -> None:
    config.state_file.unlink(missing_ok=True)


def tail_logs(config: ManagerConfig, lines: int = 40) -> str:
    if not config.log_file.exists():
        return ""
    content = config.log_file.read_text(encoding="utf-8").splitlines()
    return "\n".join(content[-lines:])
