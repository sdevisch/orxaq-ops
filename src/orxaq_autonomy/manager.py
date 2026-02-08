"""Cross-platform autonomy supervisor and lifecycle manager."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ide import generate_workspace, open_in_ide


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _log(msg: str) -> None:
    print(f"[{_now_iso()}] {msg}", flush=True)


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


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        return int(getuid())
    return 0


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


def _resolve_binary(binary: str) -> str | None:
    raw = binary.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        if path.exists() and path.is_file():
            return str(path)
        return None
    resolved = shutil.which(raw)
    if resolved:
        return resolved
    return None


def _has_codex_auth(env: dict[str, str], codex_cmd: str) -> bool:
    if env.get("OPENAI_API_KEY") and env.get("OPENAI_API_KEY") != "replace_me":
        return True
    resolved = _resolve_binary(codex_cmd)
    if not resolved:
        return False
    try:
        status = subprocess.run([resolved, "login", "status"], check=False, capture_output=True, timeout=10)
    except Exception:
        return False
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


def _tasks_require_owner(tasks_file: Path, owner: str) -> bool:
    if not tasks_file.exists():
        return False
    try:
        raw = json.loads(tasks_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(raw, list):
        return False
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("owner", "")).strip().lower() == owner:
            return True
    return False


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
    conversation_log_file: Path
    runner_pid_file: Path
    supervisor_pid_file: Path
    dashboard_pid_file: Path
    dashboard_meta_file: Path
    log_file: Path
    dashboard_log_file: Path
    lanes_file: Path
    lanes_runtime_dir: Path
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
    heartbeat_poll_sec: int
    heartbeat_stale_sec: int
    supervisor_restart_delay_sec: int
    supervisor_max_backoff_sec: int
    supervisor_max_restarts: int
    validate_commands: list[str]
    skill_protocol_file: Path
    mcp_context_file: Path | None
    codex_startup_prompt_file: Path | None
    gemini_startup_prompt_file: Path | None
    claude_startup_prompt_file: Path | None
    codex_cmd: str
    gemini_cmd: str
    claude_cmd: str
    codex_model: str | None
    gemini_model: str | None
    claude_model: str | None

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

        artifacts = _path("ORXAQ_AUTONOMY_ARTIFACTS_DIR", root / "artifacts" / "autonomy")
        skill_protocol = _path("ORXAQ_AUTONOMY_SKILL_PROTOCOL_FILE", root / "config" / "skill_protocol.json")
        mcp_context_raw = merged.get("ORXAQ_AUTONOMY_MCP_CONTEXT_FILE", "").strip()
        mcp_context = Path(mcp_context_raw).resolve() if mcp_context_raw else None
        codex_prompt_raw = merged.get(
            "ORXAQ_AUTONOMY_CODEX_PROMPT_FILE",
            str(root / "config" / "prompts" / "codex_impl_prompt.md"),
        ).strip()
        gemini_prompt_raw = merged.get(
            "ORXAQ_AUTONOMY_GEMINI_PROMPT_FILE",
            str(root / "config" / "prompts" / "gemini_test_prompt.md"),
        ).strip()
        claude_prompt_raw = merged.get(
            "ORXAQ_AUTONOMY_CLAUDE_PROMPT_FILE",
            str(root / "config" / "prompts" / "claude_review_prompt.md"),
        ).strip()
        codex_prompt_file = Path(codex_prompt_raw).resolve() if codex_prompt_raw else None
        gemini_prompt_file = Path(gemini_prompt_raw).resolve() if gemini_prompt_raw else None
        claude_prompt_file = Path(claude_prompt_raw).resolve() if claude_prompt_raw else None
        codex_cmd = merged.get("ORXAQ_AUTONOMY_CODEX_CMD", "codex").strip() or "codex"
        gemini_cmd = merged.get("ORXAQ_AUTONOMY_GEMINI_CMD", "gemini").strip() or "gemini"
        claude_cmd = merged.get("ORXAQ_AUTONOMY_CLAUDE_CMD", "claude").strip() or "claude"
        codex_model = merged.get("ORXAQ_AUTONOMY_CODEX_MODEL", "").strip() or None
        gemini_model = merged.get("ORXAQ_AUTONOMY_GEMINI_MODEL", "").strip() or None
        claude_model = merged.get("ORXAQ_AUTONOMY_CLAUDE_MODEL", "").strip() or None

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
            conversation_log_file=_path(
                "ORXAQ_AUTONOMY_CONVERSATION_LOG_FILE",
                artifacts / "conversations.ndjson",
            ),
            runner_pid_file=_path("ORXAQ_AUTONOMY_RUNNER_PID_FILE", artifacts / "runner.pid"),
            supervisor_pid_file=_path("ORXAQ_AUTONOMY_SUPERVISOR_PID_FILE", artifacts / "supervisor.pid"),
            log_file=_path("ORXAQ_AUTONOMY_LOG_FILE", artifacts / "runner.log"),
            dashboard_pid_file=_path("ORXAQ_AUTONOMY_DASHBOARD_PID_FILE", artifacts / "dashboard.pid"),
            dashboard_meta_file=_path("ORXAQ_AUTONOMY_DASHBOARD_META_FILE", artifacts / "dashboard.json"),
            dashboard_log_file=_path("ORXAQ_AUTONOMY_DASHBOARD_LOG_FILE", artifacts / "dashboard.log"),
            lanes_file=_path("ORXAQ_AUTONOMY_LANES_FILE", root / "config" / "lanes.json"),
            lanes_runtime_dir=_path("ORXAQ_AUTONOMY_LANES_RUNTIME_DIR", artifacts / "lanes"),
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
            heartbeat_poll_sec=_int("ORXAQ_AUTONOMY_HEARTBEAT_POLL_SEC", 20),
            heartbeat_stale_sec=_int("ORXAQ_AUTONOMY_HEARTBEAT_STALE_SEC", 300),
            supervisor_restart_delay_sec=_int("ORXAQ_AUTONOMY_SUPERVISOR_RESTART_DELAY_SEC", 5),
            supervisor_max_backoff_sec=_int("ORXAQ_AUTONOMY_SUPERVISOR_MAX_BACKOFF_SEC", 300),
            supervisor_max_restarts=_int("ORXAQ_AUTONOMY_SUPERVISOR_MAX_RESTARTS", 0),
            validate_commands=validate_commands,
            skill_protocol_file=skill_protocol,
            mcp_context_file=mcp_context,
            codex_startup_prompt_file=codex_prompt_file,
            gemini_startup_prompt_file=gemini_prompt_file,
            claude_startup_prompt_file=claude_prompt_file,
            codex_cmd=codex_cmd,
            gemini_cmd=gemini_cmd,
            claude_cmd=claude_cmd,
            codex_model=codex_model,
            gemini_model=gemini_model,
            claude_model=claude_model,
        )


def runtime_diagnostics(config: ManagerConfig) -> dict[str, Any]:
    env = _load_env_file(config.env_file)
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    recommendations: list[str] = []

    codex_path = _resolve_binary(config.codex_cmd)
    if codex_path:
        checks.append(
            {
                "name": "codex_cli",
                "ok": True,
                "message": f"resolved {config.codex_cmd!r} to {codex_path}",
            }
        )
    else:
        msg = f"Codex CLI not found for command '{config.codex_cmd}'."
        checks.append({"name": "codex_cli", "ok": False, "message": msg})
        errors.append(msg)
        recommendations.append("Install Codex CLI and ensure it is available in PATH.")
        recommendations.append(f"Or set ORXAQ_AUTONOMY_CODEX_CMD in {config.env_file} to an absolute binary path.")

    gemini_path = _resolve_binary(config.gemini_cmd)
    if gemini_path:
        checks.append(
            {
                "name": "gemini_cli",
                "ok": True,
                "message": f"resolved {config.gemini_cmd!r} to {gemini_path}",
            }
        )
    else:
        msg = f"Gemini CLI not found for command '{config.gemini_cmd}'."
        checks.append({"name": "gemini_cli", "ok": False, "message": msg})
        errors.append(msg)
        recommendations.append("Install Gemini CLI and ensure it is available in PATH.")
        recommendations.append(f"Or set ORXAQ_AUTONOMY_GEMINI_CMD in {config.env_file} to an absolute binary path.")

    claude_required = _tasks_require_owner(config.tasks_file, "claude")
    claude_path = _resolve_binary(config.claude_cmd)
    if claude_path:
        checks.append(
            {
                "name": "claude_cli",
                "ok": True,
                "message": f"resolved {config.claude_cmd!r} to {claude_path}",
            }
        )
    elif claude_required:
        msg = f"Claude CLI not found for command '{config.claude_cmd}', but claude-owned tasks exist."
        checks.append({"name": "claude_cli", "ok": False, "message": msg})
        errors.append(msg)
        recommendations.append("Install Claude CLI and ensure it is available in PATH.")
        recommendations.append(f"Or set ORXAQ_AUTONOMY_CLAUDE_CMD in {config.env_file} to an absolute binary path.")
    else:
        checks.append(
            {
                "name": "claude_cli",
                "ok": True,
                "message": "Claude CLI not required for current tasks.",
            }
        )

    codex_auth_ok = _has_codex_auth(env, config.codex_cmd)
    checks.append(
        {
            "name": "codex_auth",
            "ok": codex_auth_ok,
            "message": "OPENAI_API_KEY present or `codex login status` succeeded.",
        }
    )
    if not codex_auth_ok:
        errors.append("Codex auth missing.")
        recommendations.append("Set OPENAI_API_KEY in .env.autonomy or run `codex login`.")

    gemini_auth_ok = _has_gemini_auth(env)
    checks.append(
        {
            "name": "gemini_auth",
            "ok": gemini_auth_ok,
            "message": "GEMINI_API_KEY / Google auth config present.",
        }
    )
    if not gemini_auth_ok:
        errors.append("Gemini auth missing.")
        recommendations.append("Set GEMINI_API_KEY in .env.autonomy or configure ~/.gemini/settings.json.")

    unique_recommendations: list[str] = []
    for recommendation in recommendations:
        if recommendation not in unique_recommendations:
            unique_recommendations.append(recommendation)

    return {
        "ok": len(errors) == 0,
        "checks": checks,
        "errors": errors,
        "recommendations": unique_recommendations,
    }


def ensure_runtime(config: ManagerConfig) -> None:
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = runtime_diagnostics(config)
    if diagnostics["ok"]:
        return
    message = "; ".join(diagnostics["errors"])
    raise RuntimeError(message)


def _repo_is_clean(repo: Path) -> tuple[bool, str]:
    ok, message = _repo_basic_check(repo)
    if not ok:
        return False, message
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    if dirty.returncode != 0:
        return False, f"unable to inspect git status: {repo}"
    if dirty.stdout.strip():
        return False, f"repo has local changes: {repo}"
    return True, "ok"


def _repo_basic_check(repo: Path) -> tuple[bool, str]:
    if not repo.exists():
        return False, f"missing repository: {repo}"
    if not repo.is_dir():
        return False, f"repository path is not a directory: {repo}"
    inside = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0:
        return False, f"not a git repo: {repo}"
    return True, "ok"


def preflight(config: ManagerConfig, *, require_clean: bool = True) -> dict[str, Any]:
    diagnostics = runtime_diagnostics(config)
    runtime_ok = bool(diagnostics["ok"])
    result: dict[str, Any] = {
        "runtime": "ok" if runtime_ok else "error",
        "runtime_checks": diagnostics["checks"],
        "runtime_errors": diagnostics["errors"],
        "runtime_recommendations": diagnostics["recommendations"],
        "impl_repo": str(config.impl_repo),
        "test_repo": str(config.test_repo),
        "clean": runtime_ok,
        "checks": [],
    }
    for repo in (config.impl_repo, config.test_repo):
        if require_clean:
            ok, message = _repo_is_clean(repo)
        else:
            ok, message = _repo_basic_check(repo)
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
        "--skill-protocol-file",
        str(config.skill_protocol_file),
    ]
    if config.mcp_context_file is not None:
        args.extend(["--mcp-context-file", str(config.mcp_context_file)])
    args.extend(
        [
            "--codex-cmd",
            config.codex_cmd,
            "--gemini-cmd",
            config.gemini_cmd,
            "--claude-cmd",
            config.claude_cmd,
            "--conversation-log-file",
            str(config.conversation_log_file),
        ]
    )
    if config.codex_model:
        args.extend(["--codex-model", config.codex_model])
    if config.gemini_model:
        args.extend(["--gemini-model", config.gemini_model])
    if config.claude_model:
        args.extend(["--claude-model", config.claude_model])
    if config.codex_startup_prompt_file is not None:
        args.extend(["--codex-startup-prompt-file", str(config.codex_startup_prompt_file)])
    if config.gemini_startup_prompt_file is not None:
        args.extend(["--gemini-startup-prompt-file", str(config.gemini_startup_prompt_file)])
    if config.claude_startup_prompt_file is not None:
        args.extend(["--claude-startup-prompt-file", str(config.claude_startup_prompt_file)])
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
    _write_pid(config.supervisor_pid_file, proc.pid)
    _log(f"autonomy supervisor started (pid={proc.pid})")


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


def ensure_background(config: ManagerConfig) -> None:
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


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dashboard_status_snapshot(config: ManagerConfig) -> dict[str, Any]:
    pid = _read_pid(config.dashboard_pid_file)
    running = _pid_running(pid)
    meta = _read_json_file(config.dashboard_meta_file)
    url = str(meta.get("url", "")).strip()

    # Best-effort: update URL from log banner if available.
    if config.dashboard_log_file.exists():
        for line in config.dashboard_log_file.read_text(encoding="utf-8").splitlines()[-20:]:
            if line.startswith("dashboard_url="):
                url = line.split("=", 1)[1].strip()
                break

    return {
        "running": running,
        "pid": pid,
        "url": url,
        "host": str(meta.get("host", "")),
        "port": int(meta.get("port", 0) or 0),
        "refresh_sec": int(meta.get("refresh_sec", 0) or 0),
        "log_file": str(config.dashboard_log_file),
        "pid_file": str(config.dashboard_pid_file),
        "meta_file": str(config.dashboard_meta_file),
    }


def start_dashboard_background(
    config: ManagerConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    refresh_sec: int = 5,
    open_browser: bool = True,
) -> dict[str, Any]:
    existing = dashboard_status_snapshot(config)
    if existing["running"]:
        return existing

    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_handle = config.dashboard_log_file.open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "orxaq_autonomy.cli",
        "--root",
        str(config.root_dir),
        "dashboard",
        "--host",
        host,
        "--port",
        str(int(port)),
        "--refresh-sec",
        str(int(refresh_sec)),
    ]
    if not open_browser:
        cmd.append("--no-browser")

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
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        log_handle.close()

    _write_pid(config.dashboard_pid_file, proc.pid)
    _write_json_file(
        config.dashboard_meta_file,
        {
            "started_at": _now_iso(),
            "host": host,
            "port": int(port),
            "refresh_sec": int(refresh_sec),
            "url": f"http://{host}:{int(port)}/",
        },
    )
    time.sleep(0.7)
    if not _pid_running(proc.pid):
        details = tail_dashboard_logs(config, lines=40)
        raise RuntimeError(f"Dashboard failed to stay running.\n{details}")
    return dashboard_status_snapshot(config)


def stop_dashboard_background(config: ManagerConfig) -> dict[str, Any]:
    pid = _read_pid(config.dashboard_pid_file)
    if pid:
        _terminate_pid(pid)
    config.dashboard_pid_file.unlink(missing_ok=True)
    return dashboard_status_snapshot(config)


def ensure_dashboard_background(
    config: ManagerConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    refresh_sec: int = 5,
    open_browser: bool = False,
) -> dict[str, Any]:
    snapshot = dashboard_status_snapshot(config)
    if snapshot["running"]:
        return snapshot
    return start_dashboard_background(
        config,
        host=host,
        port=port,
        refresh_sec=refresh_sec,
        open_browser=open_browser,
    )


def tail_dashboard_logs(config: ManagerConfig, lines: int = 80) -> str:
    if not config.dashboard_log_file.exists():
        return ""
    content = config.dashboard_log_file.read_text(encoding="utf-8").splitlines()
    return "\n".join(content[-max(1, int(lines)) :])


def _git_command(repo: Path, args: list[str], timeout_sec: int = 10) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception as err:
        return False, str(err)
    if result.returncode == 0:
        return True, result.stdout.strip()
    message = (result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed").strip()
    return False, message


def _repo_monitor_snapshot(repo: Path) -> dict[str, Any]:
    ok, message = _repo_basic_check(repo)
    if not ok:
        return {
            "path": str(repo),
            "ok": False,
            "error": message,
            "branch": "",
            "head": "",
            "dirty": False,
            "changed_files": 0,
        }

    branch_ok, branch_value = _git_command(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    head_ok, head_value = _git_command(repo, ["rev-parse", "--short", "HEAD"])
    dirty_ok, dirty_value = _git_command(repo, ["status", "--porcelain"])
    changed_files = len([line for line in dirty_value.splitlines() if line.strip()]) if dirty_ok else 0

    errors: list[str] = []
    if not branch_ok:
        errors.append(f"branch: {branch_value}")
    if not head_ok:
        errors.append(f"head: {head_value}")
    if not dirty_ok:
        errors.append(f"dirty: {dirty_value}")

    return {
        "path": str(repo),
        "ok": len(errors) == 0,
        "error": "; ".join(errors),
        "branch": branch_value if branch_ok else "",
        "head": head_value if head_ok else "",
        "dirty": bool(changed_files) if dirty_ok else False,
        "changed_files": changed_files,
    }


def _state_progress_snapshot(config: ManagerConfig) -> dict[str, Any]:
    counts = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 0}
    active_tasks: list[str] = []
    blocked_tasks: list[str] = []

    if config.state_file.exists():
        try:
            raw = json.loads(config.state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for task_id, payload in raw.items():
                    status = "unknown"
                    if isinstance(payload, dict):
                        status = str(payload.get("status", "unknown")).strip().lower()
                    if status in counts:
                        counts[status] += 1
                    else:
                        counts["unknown"] += 1
                    if status == "in_progress":
                        active_tasks.append(str(task_id))
                    if status == "blocked":
                        blocked_tasks.append(str(task_id))
        except Exception:
            counts["unknown"] += 1

    return {
        "counts": counts,
        "active_tasks": sorted(active_tasks),
        "blocked_tasks": sorted(blocked_tasks),
    }


def monitor_snapshot(config: ManagerConfig) -> dict[str, Any]:
    status = status_snapshot(config)
    progress = _state_progress_snapshot(config)
    lanes = lane_status_snapshot(config)
    conv = conversations_snapshot(config, lines=60, include_lanes=True)
    latest_log_line = tail_logs(config, lines=1, latest_run_only=True).strip()
    snapshot = {
        "timestamp": _now_iso(),
        "status": status,
        "progress": progress,
        "lanes": lanes,
        "conversations": {
            "total_events": conv.get("total_events", 0),
            "owner_counts": conv.get("owner_counts", {}),
            "latest": (conv.get("events", [])[-1] if conv.get("events") else {}),
        },
        "repos": {
            "implementation": _repo_monitor_snapshot(config.impl_repo),
            "tests": _repo_monitor_snapshot(config.test_repo),
        },
        "latest_log_line": latest_log_line,
    }
    monitor_file = config.artifacts_dir / "monitor.json"
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    monitor_file.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot["monitor_file"] = str(monitor_file)
    return snapshot


def render_monitor_text(snapshot: dict[str, Any]) -> str:
    status = snapshot.get("status", {})
    progress = snapshot.get("progress", {})
    counts = progress.get("counts", {})
    active = progress.get("active_tasks", [])
    repos = snapshot.get("repos", {})
    lanes = snapshot.get("lanes", {})
    conversations = snapshot.get("conversations", {})
    impl = repos.get("implementation", {})
    tests = repos.get("tests", {})
    latest_log_line = str(snapshot.get("latest_log_line", "")).strip()

    def _repo_line(label: str, payload: dict[str, Any]) -> str:
        if not payload:
            return f"{label}: unavailable"
        if not payload.get("ok", False):
            return f"{label}: error={payload.get('error', 'unknown')}"
        return (
            f"{label}: branch={payload.get('branch', '')} "
            f"head={payload.get('head', '')} "
            f"dirty={payload.get('dirty', False)} "
            f"changed_files={payload.get('changed_files', 0)}"
        )

    lines = [
        f"[{snapshot.get('timestamp', '')}] supervisor={status.get('supervisor_running', False)} "
        f"runner={status.get('runner_running', False)} heartbeat_age={status.get('heartbeat_age_sec', -1)}s",
        "tasks: "
        f"done={counts.get('done', 0)} "
        f"in_progress={counts.get('in_progress', 0)} "
        f"pending={counts.get('pending', 0)} "
        f"blocked={counts.get('blocked', 0)} "
        f"unknown={counts.get('unknown', 0)}",
        f"active_tasks: {', '.join(active) if active else 'none'}",
        (
            f"lanes: running={lanes.get('running_count', 0)}/"
            f"{lanes.get('total_count', 0)}"
        ),
        (
            f"conversations: events={conversations.get('total_events', 0)} "
            f"owners={conversations.get('owner_counts', {})}"
        ),
        _repo_line("impl_repo", impl),
        _repo_line("test_repo", tests),
    ]
    if latest_log_line:
        lines.append(f"log: {latest_log_line}")
    lines.append(f"monitor_file: {snapshot.get('monitor_file', '')}")
    return "\n".join(lines)


def monitor_loop(
    config: ManagerConfig,
    *,
    interval_sec: int = 15,
    cycles: int = 0,
    json_mode: bool = False,
) -> int:
    interval = max(1, int(interval_sec))
    remaining = int(cycles)
    while True:
        snapshot = monitor_snapshot(config)
        if json_mode:
            print(json.dumps(snapshot, sort_keys=True))
        else:
            print(render_monitor_text(snapshot), flush=True)
            print("", flush=True)

        if remaining > 0:
            remaining -= 1
            if remaining <= 0:
                return 0
        elif remaining == 0 and cycles > 0:
            return 0

        time.sleep(interval)


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
        payload = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>{label}</string>
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
        uid = _current_uid()
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
        uid = _current_uid()
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
        uid = _current_uid()
        result = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], check=False, capture_output=True)
        return {"platform": "macos", "label": label, "active": result.returncode == 0, "plist": str(plist)}
    return {"platform": sys.platform, "active": False, "note": "No native keepalive integration for this platform."}


def reset_state(config: ManagerConfig) -> None:
    config.state_file.unlink(missing_ok=True)


def tail_logs(config: ManagerConfig, lines: int = 40, latest_run_only: bool = False) -> str:
    if not config.log_file.exists():
        return ""
    content = config.log_file.read_text(encoding="utf-8").splitlines()
    if latest_run_only:
        for idx in range(len(content) - 1, -1, -1):
            if "supervisor: launching runner" in content[idx]:
                content = content[idx:]
                break
    return "\n".join(content[-lines:])


def _resolve_path(root: Path, raw: str, default: Path) -> Path:
    value = raw.strip()
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (root / path).resolve()
    return path.resolve()


def _tail_ndjson(path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def load_lane_specs(config: ManagerConfig) -> list[dict[str, Any]]:
    if not config.lanes_file.exists():
        return []
    raw = json.loads(config.lanes_file.read_text(encoding="utf-8"))
    lane_items = raw.get("lanes", []) if isinstance(raw, dict) else raw
    if not isinstance(lane_items, list):
        raise RuntimeError(f"Lane file must contain a list of lanes: {config.lanes_file}")

    lanes: list[dict[str, Any]] = []
    for item in lane_items:
        if not isinstance(item, dict):
            continue
        lane_id = str(item.get("id", "")).strip()
        owner = str(item.get("owner", "")).strip().lower()
        if not lane_id:
            continue
        if owner not in {"codex", "gemini", "claude"}:
            raise RuntimeError(f"Unsupported lane owner {owner!r} for lane {lane_id!r}")
        runtime_dir = (config.lanes_runtime_dir / lane_id).resolve()
        artifacts_dir = _resolve_path(config.root_dir, str(item.get("artifacts_dir", "")), runtime_dir)
        state_file = _resolve_path(
            config.root_dir,
            str(item.get("state_file", "")),
            artifacts_dir / "state.json",
        )
        heartbeat_file = _resolve_path(
            config.root_dir,
            str(item.get("heartbeat_file", "")),
            artifacts_dir / "heartbeat.json",
        )
        lock_file = _resolve_path(config.root_dir, str(item.get("lock_file", "")), artifacts_dir / "runner.lock")
        conversation_log_file = _resolve_path(
            config.root_dir,
            str(item.get("conversation_log_file", "")),
            artifacts_dir / "conversations.ndjson",
        )
        mcp_raw = str(item.get("mcp_context_file", "")).strip()
        mcp_default = config.mcp_context_file or (config.root_dir / "config" / "mcp_context.example.json")
        if mcp_raw or config.mcp_context_file is not None:
            mcp_context_file: Path | None = _resolve_path(config.root_dir, mcp_raw, mcp_default)
        else:
            mcp_context_file = None

        lane = {
            "id": lane_id,
            "enabled": bool(item.get("enabled", True)),
            "owner": owner,
            "description": str(item.get("description", "")).strip(),
            "impl_repo": _resolve_path(config.root_dir, str(item.get("impl_repo", "")), config.impl_repo),
            "test_repo": _resolve_path(config.root_dir, str(item.get("test_repo", "")), config.test_repo),
            "tasks_file": _resolve_path(config.root_dir, str(item.get("tasks_file", "")), config.tasks_file),
            "objective_file": _resolve_path(config.root_dir, str(item.get("objective_file", "")), config.objective_file),
            "schema_file": _resolve_path(config.root_dir, str(item.get("schema_file", "")), config.schema_file),
            "skill_protocol_file": _resolve_path(
                config.root_dir,
                str(item.get("skill_protocol_file", "")),
                config.skill_protocol_file,
            ),
            "mcp_context_file": mcp_context_file,
            "state_file": state_file,
            "artifacts_dir": artifacts_dir,
            "heartbeat_file": heartbeat_file,
            "lock_file": lock_file,
            "conversation_log_file": conversation_log_file,
            "owner_filter": [owner],
            "validate_commands": [
                str(cmd).strip() for cmd in item.get("validate_commands", config.validate_commands) if str(cmd).strip()
            ],
            "exclusive_paths": [str(path).strip() for path in item.get("exclusive_paths", []) if str(path).strip()],
            "runtime_dir": runtime_dir,
        }
        lanes.append(lane)
    return lanes


def _lane_pid_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "lane.pid").resolve()


def _lane_meta_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "lane.json").resolve()


def _lane_log_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "runner.log").resolve()


def lane_status_snapshot(config: ManagerConfig) -> dict[str, Any]:
    lanes = load_lane_specs(config)
    snapshots: list[dict[str, Any]] = []
    for lane in lanes:
        lane_id = lane["id"]
        pid_path = _lane_pid_file(config, lane_id)
        log_path = _lane_log_file(config, lane_id)
        meta_path = _lane_meta_file(config, lane_id)
        pid = _read_pid(pid_path)
        running = _pid_running(pid)
        meta = _read_json_file(meta_path)
        latest = ""
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            latest = lines[-1] if lines else ""
        snapshots.append(
            {
                "id": lane_id,
                "enabled": lane["enabled"],
                "owner": lane["owner"],
                "description": lane["description"],
                "running": running,
                "pid": pid,
                "tasks_file": str(lane["tasks_file"]),
                "objective_file": str(lane["objective_file"]),
                "impl_repo": str(lane["impl_repo"]),
                "test_repo": str(lane["test_repo"]),
                "exclusive_paths": lane["exclusive_paths"],
                "latest_log_line": latest,
                "log_file": str(log_path),
                "pid_file": str(pid_path),
                "meta": meta,
            }
        )
    return {
        "timestamp": _now_iso(),
        "lanes_file": str(config.lanes_file),
        "running_count": sum(1 for lane in snapshots if lane["running"]),
        "total_count": len(snapshots),
        "lanes": snapshots,
    }


def _lane_command_for_owner(config: ManagerConfig, owner: str) -> str:
    if owner == "codex":
        return config.codex_cmd
    if owner == "gemini":
        return config.gemini_cmd
    if owner == "claude":
        return config.claude_cmd
    raise RuntimeError(f"Unsupported lane owner {owner!r}")


def _lane_startup_prompt(config: ManagerConfig, owner: str) -> Path | None:
    if owner == "codex":
        return config.codex_startup_prompt_file
    if owner == "gemini":
        return config.gemini_startup_prompt_file
    if owner == "claude":
        return config.claude_startup_prompt_file
    return None


def _build_lane_runner_cmd(config: ManagerConfig, lane: dict[str, Any]) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "orxaq_autonomy.runner",
        "--impl-repo",
        str(lane["impl_repo"]),
        "--test-repo",
        str(lane["test_repo"]),
        "--tasks-file",
        str(lane["tasks_file"]),
        "--state-file",
        str(lane["state_file"]),
        "--objective-file",
        str(lane["objective_file"]),
        "--codex-schema",
        str(lane["schema_file"]),
        "--artifacts-dir",
        str(lane["artifacts_dir"]),
        "--heartbeat-file",
        str(lane["heartbeat_file"]),
        "--lock-file",
        str(lane["lock_file"]),
        "--conversation-log-file",
        str(lane["conversation_log_file"]),
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
        "--skill-protocol-file",
        str(lane["skill_protocol_file"]),
        "--codex-cmd",
        config.codex_cmd,
        "--gemini-cmd",
        config.gemini_cmd,
        "--claude-cmd",
        config.claude_cmd,
        "--owner-filter",
        lane["owner"],
    ]
    if lane["mcp_context_file"] is not None:
        cmd.extend(["--mcp-context-file", str(lane["mcp_context_file"])])
    if config.codex_model:
        cmd.extend(["--codex-model", config.codex_model])
    if config.gemini_model:
        cmd.extend(["--gemini-model", config.gemini_model])
    if config.claude_model:
        cmd.extend(["--claude-model", config.claude_model])

    codex_prompt = _lane_startup_prompt(config, "codex")
    gemini_prompt = _lane_startup_prompt(config, "gemini")
    claude_prompt = _lane_startup_prompt(config, "claude")
    if codex_prompt is not None:
        cmd.extend(["--codex-startup-prompt-file", str(codex_prompt)])
    if gemini_prompt is not None:
        cmd.extend(["--gemini-startup-prompt-file", str(gemini_prompt)])
    if claude_prompt is not None:
        cmd.extend(["--claude-startup-prompt-file", str(claude_prompt)])

    for validate in lane["validate_commands"]:
        cmd.extend(["--validate-command", validate])
    return cmd


def start_lane_background(config: ManagerConfig, lane_id: str) -> dict[str, Any]:
    lanes = load_lane_specs(config)
    lane = next((item for item in lanes if item["id"] == lane_id), None)
    if lane is None:
        raise RuntimeError(f"Unknown lane id {lane_id!r}. Update {config.lanes_file}.")

    cmd_name = _lane_command_for_owner(config, lane["owner"])
    if _resolve_binary(cmd_name) is None:
        raise RuntimeError(f"{lane['owner']} CLI not found in PATH: {cmd_name}")

    for repo in {lane["impl_repo"], lane["test_repo"]}:
        ok, message = _repo_basic_check(repo)
        if not ok:
            raise RuntimeError(f"Lane {lane_id}: {message}")
    for required_file_key in ("tasks_file", "objective_file", "schema_file", "skill_protocol_file"):
        path = lane[required_file_key]
        if not path.exists():
            raise RuntimeError(f"Lane {lane_id}: missing {required_file_key} at {path}")

    pid_path = _lane_pid_file(config, lane_id)
    log_path = _lane_log_file(config, lane_id)
    meta_path = _lane_meta_file(config, lane_id)
    existing_pid = _read_pid(pid_path)
    if _pid_running(existing_pid):
        status = lane_status_snapshot(config)
        existing = next((item for item in status["lanes"] if item["id"] == lane_id), None)
        return existing or {"id": lane_id, "running": True, "pid": existing_pid}

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    cmd = _build_lane_runner_cmd(config, lane)
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
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        log_handle.close()

    _write_pid(pid_path, proc.pid)
    _write_json_file(
        meta_path,
        {
            "started_at": _now_iso(),
            "lane_id": lane_id,
            "owner": lane["owner"],
            "command": cmd,
            "tasks_file": str(lane["tasks_file"]),
            "objective_file": str(lane["objective_file"]),
            "conversation_log_file": str(lane["conversation_log_file"]),
            "exclusive_paths": lane["exclusive_paths"],
        },
    )
    time.sleep(0.4)
    return next((item for item in lane_status_snapshot(config)["lanes"] if item["id"] == lane_id), {"id": lane_id})


def stop_lane_background(config: ManagerConfig, lane_id: str) -> dict[str, Any]:
    pid_path = _lane_pid_file(config, lane_id)
    pid = _read_pid(pid_path)
    if pid:
        _terminate_pid(pid)
    pid_path.unlink(missing_ok=True)
    status = lane_status_snapshot(config)
    return next((item for item in status["lanes"] if item["id"] == lane_id), {"id": lane_id, "running": False})


def start_lanes_background(config: ManagerConfig, lane_id: str | None = None) -> dict[str, Any]:
    lanes = load_lane_specs(config)
    selected = [lane for lane in lanes if lane["enabled"]] if lane_id is None else [lane for lane in lanes if lane["id"] == lane_id]
    if lane_id is not None and not selected:
        raise RuntimeError(f"Unknown lane id {lane_id!r}. Update {config.lanes_file}.")
    started: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for lane in selected:
        try:
            started.append(start_lane_background(config, lane["id"]))
        except Exception as err:
            failed.append({"id": lane["id"], "owner": lane["owner"], "error": str(err)})
    return {
        "timestamp": _now_iso(),
        "requested_lane": lane_id or "all_enabled",
        "started_count": len(started),
        "started": started,
        "failed_count": len(failed),
        "failed": failed,
        "ok": len(started) > 0 or len(failed) == 0,
    }


def stop_lanes_background(config: ManagerConfig, lane_id: str | None = None) -> dict[str, Any]:
    status = lane_status_snapshot(config)
    selected = status["lanes"] if lane_id is None else [lane for lane in status["lanes"] if lane["id"] == lane_id]
    if lane_id is not None and not selected:
        raise RuntimeError(f"Unknown lane id {lane_id!r}. Update {config.lanes_file}.")
    stopped: list[dict[str, Any]] = []
    for lane in selected:
        stopped.append(stop_lane_background(config, lane["id"]))
    return {
        "timestamp": _now_iso(),
        "requested_lane": lane_id or "all",
        "stopped_count": len(stopped),
        "stopped": stopped,
    }


def conversations_snapshot(config: ManagerConfig, lines: int = 200, include_lanes: bool = True) -> dict[str, Any]:
    line_limit = max(1, min(2000, int(lines)))
    sources: list[Path] = [config.conversation_log_file]
    if include_lanes:
        for lane in load_lane_specs(config):
            lane_file = Path(lane["conversation_log_file"])
            if lane_file not in sources:
                sources.append(lane_file)

    events: list[dict[str, Any]] = []
    for source in sources:
        for item in _tail_ndjson(source, line_limit):
            item["source"] = str(source)
            events.append(item)
    events = sorted(events, key=lambda item: str(item.get("timestamp", "")))[-line_limit:]

    owners: dict[str, int] = {}
    for event in events:
        owner = str(event.get("owner", "unknown")).strip() or "unknown"
        owners[owner] = owners.get(owner, 0) + 1

    return {
        "timestamp": _now_iso(),
        "conversation_files": [str(path) for path in sources],
        "total_events": len(events),
        "events": events,
        "owner_counts": owners,
    }


def _read_optional_text(path: Path | None) -> str:
    if path is None:
        return ""
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def write_startup_packet(config: ManagerConfig, workspace_file: Path) -> Path:
    codex_prompt_text = _read_optional_text(config.codex_startup_prompt_file)
    gemini_prompt_text = _read_optional_text(config.gemini_startup_prompt_file)
    claude_prompt_text = _read_optional_text(config.claude_startup_prompt_file)
    output = config.artifacts_dir / "startup_packet.md"
    output.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Orxaq Collaboration Startup Packet",
        "",
        "## Runtime",
        f"- Objective: `{config.objective_file}`",
        f"- Task queue: `{config.tasks_file}`",
        f"- Skill protocol: `{config.skill_protocol_file}`",
        f"- Workspace: `{workspace_file}`",
        f"- Supervisor log: `{config.log_file}`",
        f"- Conversations log: `{config.conversation_log_file}`",
        f"- Lane plan: `{config.lanes_file}`",
        "",
        "## AI Startup Prompts",
    ]
    if config.codex_startup_prompt_file is not None:
        lines.append(f"- Codex prompt source: `{config.codex_startup_prompt_file}`")
    if config.gemini_startup_prompt_file is not None:
        lines.append(f"- Gemini prompt source: `{config.gemini_startup_prompt_file}`")
    if config.claude_startup_prompt_file is not None:
        lines.append(f"- Claude prompt source: `{config.claude_startup_prompt_file}`")

    if codex_prompt_text:
        lines.extend(
            [
                "",
                "### Codex",
                "",
                "```text",
                codex_prompt_text,
                "```",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "### Codex",
                "",
                "_No Codex startup prompt file found._",
            ]
        )

    if gemini_prompt_text:
        lines.extend(
            [
                "",
                "### Gemini",
                "",
                "```text",
                gemini_prompt_text,
                "```",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "### Gemini",
                "",
                "_No Gemini startup prompt file found._",
            ]
        )

    if claude_prompt_text:
        lines.extend(
            [
                "",
                "### Claude",
                "",
                "```text",
                claude_prompt_text,
                "```",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "### Claude",
                "",
                "_No Claude startup prompt file found._",
            ]
        )

    output.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return output


def bootstrap_background(
    config: ManagerConfig,
    *,
    allow_dirty: bool = True,
    install_keepalive_job: bool = True,
    ide: str | None = "vscode",
    workspace_filename: str = "orxaq-dual-agent.code-workspace",
) -> dict[str, Any]:
    workspace_file = (config.root_dir / workspace_filename).resolve()
    workspace_reused = workspace_file.exists()
    if not workspace_reused:
        workspace_file = generate_workspace(
            config.root_dir,
            config.impl_repo,
            config.test_repo,
            workspace_file,
        )
    startup_packet = write_startup_packet(config, workspace_file=workspace_file)

    preflight_payload = preflight(config, require_clean=not allow_dirty)
    if not preflight_payload.get("clean", True):
        return {
            "ok": False,
            "reason": "preflight_failed",
            "preflight": preflight_payload,
            "workspace": str(workspace_file),
            "workspace_reused": workspace_reused,
            "startup_packet": str(startup_packet),
        }

    try:
        start_background(config)
    except Exception as err:
        return {
            "ok": False,
            "reason": "start_failed",
            "error": str(err),
            "preflight": preflight_payload,
            "workspace": str(workspace_file),
            "workspace_reused": workspace_reused,
            "startup_packet": str(startup_packet),
        }
    keepalive_info: dict[str, Any] = {"requested": install_keepalive_job, "active": False, "label": "", "error": ""}
    if install_keepalive_job:
        try:
            keepalive_info["label"] = install_keepalive(config)
            keepalive_info["active"] = True
        except Exception as err:  # pragma: no cover - defensive surface for OS-specific failures
            keepalive_info["error"] = str(err)

    ide_result = ""
    ide_error = ""
    if ide:
        ws = workspace_file if ide in {"vscode", "cursor"} else None
        try:
            ide_result = open_in_ide(ide=ide, root=config.root_dir, workspace_file=ws)
        except Exception as err:  # pragma: no cover - depends on local IDE installation
            ide_error = str(err)
    return {
        "ok": True,
        "workspace": str(workspace_file),
        "workspace_reused": workspace_reused,
        "preflight": preflight_payload,
        "supervisor": status_snapshot(config),
        "keepalive": keepalive_info,
        "ide": {"requested": ide or "none", "result": ide_result, "error": ide_error},
        "startup_packet": str(startup_packet),
        "prompts": {
            "codex": str(config.codex_startup_prompt_file) if config.codex_startup_prompt_file else "",
            "gemini": str(config.gemini_startup_prompt_file) if config.gemini_startup_prompt_file else "",
            "claude": str(config.claude_startup_prompt_file) if config.claude_startup_prompt_file else "",
        },
    }
