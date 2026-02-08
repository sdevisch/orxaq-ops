"""Cross-platform autonomy supervisor and lifecycle manager."""

from __future__ import annotations

import datetime as dt
import hashlib
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


def _read_runner_lock_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        value = raw.get("pid")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


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
    metrics_file: Path
    metrics_summary_file: Path
    pricing_file: Path
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
            metrics_file=_path("ORXAQ_AUTONOMY_METRICS_FILE", artifacts / "response_metrics.ndjson"),
            metrics_summary_file=_path(
                "ORXAQ_AUTONOMY_METRICS_SUMMARY_FILE",
                artifacts / "response_metrics_summary.json",
            ),
            pricing_file=_path("ORXAQ_AUTONOMY_PRICING_FILE", root / "config" / "pricing.json"),
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
            "--metrics-file",
            str(config.metrics_file),
            "--metrics-summary-file",
            str(config.metrics_summary_file),
            "--pricing-file",
            str(config.pricing_file),
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


def _heartbeat_age_sec_from_file(path: Path) -> int:
    if not path.exists():
        return -1
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = str(raw.get("timestamp", "")).strip()
        if not ts:
            return -1
        parsed = dt.datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int((_now_utc() - parsed).total_seconds())
    except Exception:
        return -1


def _heartbeat_age_sec(config: ManagerConfig) -> int:
    return _heartbeat_age_sec_from_file(config.heartbeat_file)


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

    if config.lanes_file.exists():
        lane_payload = ensure_lanes_background(config)
        _log(
            "lane ensure: "
            f"ensured={lane_payload['ensured_count']} "
            f"started={lane_payload['started_count']} "
            f"restarted={lane_payload['restarted_count']} "
            f"failed={lane_payload['failed_count']}"
        )


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


def _autonomy_runtime_build_id(config: ManagerConfig, *, include_dashboard: bool = True) -> str:
    hasher = hashlib.sha256()
    files = ["src/orxaq_autonomy/manager.py", "src/orxaq_autonomy/cli.py", "src/orxaq_autonomy/runner.py"]
    if include_dashboard:
        files.append("src/orxaq_autonomy/dashboard.py")
    for rel in files:
        path = (config.root_dir / rel).resolve()
        hasher.update(rel.encode("utf-8"))
        if path.exists():
            hasher.update(path.read_bytes())
        else:
            hasher.update(b"missing")
    return hasher.hexdigest()[:12]


def _dashboard_build_id(config: ManagerConfig) -> str:
    return _autonomy_runtime_build_id(config, include_dashboard=True)


def _lane_build_id(config: ManagerConfig, lane: dict[str, Any]) -> str:
    hasher = hashlib.sha256()
    hasher.update(_autonomy_runtime_build_id(config, include_dashboard=False).encode("utf-8"))
    lane_keys = (
        "id",
        "owner",
        "tasks_file",
        "objective_file",
        "impl_repo",
        "test_repo",
        "metrics_file",
        "metrics_summary_file",
        "pricing_file",
    )
    for key in lane_keys:
        hasher.update(str(key).encode("utf-8"))
        hasher.update(str(lane.get(key, "")).encode("utf-8"))
    for item in lane.get("validate_commands", []):
        hasher.update(str(item).encode("utf-8"))
    return hasher.hexdigest()[:12]


def dashboard_status_snapshot(config: ManagerConfig) -> dict[str, Any]:
    pid = _read_pid(config.dashboard_pid_file)
    running = _pid_running(pid)
    meta = _read_json_file(config.dashboard_meta_file)
    url = str(meta.get("url", "")).strip()
    build_id = str(meta.get("build_id", "")).strip()
    expected_build_id = _dashboard_build_id(config)

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
        "build_id": build_id,
        "expected_build_id": expected_build_id,
        "build_current": bool(build_id and build_id == expected_build_id),
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
            "build_id": _dashboard_build_id(config),
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
    if snapshot["running"] and snapshot.get("build_current", True):
        return snapshot
    if snapshot["running"]:
        stop_dashboard_background(config)
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


def _parse_rev_list_counts(raw: str) -> tuple[int, int] | None:
    parts = raw.strip().split()
    if len(parts) != 2:
        return None
    try:
        ahead = int(parts[0])
        behind = int(parts[1])
    except ValueError:
        return None
    return ahead, behind


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
            "upstream": "",
            "ahead": -1,
            "behind": -1,
            "sync_state": "unknown",
        }

    branch_ok, branch_value = _git_command(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    head_ok, head_value = _git_command(repo, ["rev-parse", "--short", "HEAD"])
    dirty_ok, dirty_value = _git_command(repo, ["status", "--porcelain"])
    upstream_ok, upstream_value = _git_command(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    changed_files = len([line for line in dirty_value.splitlines() if line.strip()]) if dirty_ok else 0
    ahead = -1
    behind = -1
    sync_state = "unknown"

    errors: list[str] = []
    if not branch_ok:
        errors.append(f"branch: {branch_value}")
    if not head_ok:
        errors.append(f"head: {head_value}")
    if not dirty_ok:
        errors.append(f"dirty: {dirty_value}")
    if not upstream_ok:
        errors.append(f"upstream: {upstream_value}")
        sync_state = "no_upstream"
    else:
        counts_ok, counts_value = _git_command(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
        if not counts_ok:
            errors.append(f"sync: {counts_value}")
        else:
            parsed = _parse_rev_list_counts(counts_value)
            if parsed is None:
                errors.append(f"sync_parse: {counts_value}")
            else:
                ahead, behind = parsed
                if ahead > 0 and behind > 0:
                    sync_state = "diverged"
                    errors.append(f"sync_diverged: ahead={ahead}, behind={behind}")
                elif ahead > 0:
                    sync_state = "ahead"
                elif behind > 0:
                    sync_state = "behind"
                    errors.append(f"sync_behind: ahead={ahead}, behind={behind}")
                else:
                    sync_state = "synced"

    return {
        "path": str(repo),
        "ok": len(errors) == 0,
        "error": "; ".join(errors),
        "branch": branch_value if branch_ok else "",
        "head": head_value if head_ok else "",
        "dirty": bool(changed_files) if dirty_ok else False,
        "changed_files": changed_files,
        "upstream": upstream_value if upstream_ok else "",
        "ahead": ahead,
        "behind": behind,
        "sync_state": sync_state,
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


def _combined_progress_snapshot(config: ManagerConfig, lanes: dict[str, Any]) -> dict[str, Any]:
    primary = _state_progress_snapshot(config)
    lane_items = lanes.get("lanes", []) if isinstance(lanes.get("lanes", []), list) else []
    if not lane_items:
        primary["source"] = "primary_state"
        return primary

    lane_counts = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 0}
    lane_active_tasks: list[str] = []
    lane_blocked_tasks: list[str] = []
    task_total = 0
    for lane in lane_items:
        if not isinstance(lane, dict):
            continue
        lane_state_counts = lane.get("state_counts", {})
        if not isinstance(lane_state_counts, dict):
            continue
        for key in lane_counts:
            lane_counts[key] += _int_value(lane_state_counts.get(key, 0), 0)
        task_total += _int_value(lane.get("task_total", 0), 0)
        lane_id = str(lane.get("id", "")).strip() or "unknown"
        if _int_value(lane_state_counts.get("in_progress", 0), 0) > 0:
            lane_active_tasks.append(f"lane:{lane_id}")
        if _int_value(lane_state_counts.get("blocked", 0), 0) > 0:
            lane_blocked_tasks.append(f"lane:{lane_id}")

    if sum(lane_counts.values()) == 0 and task_total == 0:
        primary["source"] = "primary_state"
        return primary

    primary_counts = primary.get("counts", {})
    counts = {
        key: _int_value(primary_counts.get(key, 0), 0) + _int_value(lane_counts.get(key, 0), 0)
        for key in {"pending", "in_progress", "done", "blocked", "unknown"}
    }
    primary_active = [str(item) for item in primary.get("active_tasks", []) if str(item).strip()]
    primary_blocked = [str(item) for item in primary.get("blocked_tasks", []) if str(item).strip()]

    return {
        "counts": counts,
        "active_tasks": sorted(set(primary_active + lane_active_tasks)),
        "blocked_tasks": sorted(set(primary_blocked + lane_blocked_tasks)),
        "source": "merged_states",
        "lane_task_total": task_total,
        "primary_state_counts": primary_counts,
        "lane_state_counts": lane_counts,
    }


def _int_value(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_value(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _build_exciting_stat(metrics: dict[str, Any]) -> dict[str, Any]:
    responses_total = _int_value(metrics.get("responses_total", 0), 0)
    tokens_total = _int_value(metrics.get("tokens_total", 0), 0)
    token_rate_per_minute = _float_value(metrics.get("token_rate_per_minute", 0.0), 0.0)
    first_time_pass_rate = _float_value(metrics.get("first_time_pass_rate", 0.0), 0.0)
    quality_score_avg = _float_value(metrics.get("quality_score_avg", 0.0), 0.0)

    if tokens_total > 0:
        return {
            "label": "Token Flow",
            "value": f"{tokens_total:,} tokens",
            "detail": (
                f"{token_rate_per_minute:.1f} tokens/min across {responses_total} responses "
                f"(exact coverage {round(_float_value(metrics.get('token_exact_coverage', 0.0), 0.0) * 100)}%)"
            ),
            "kind": "throughput",
        }
    if responses_total > 0:
        return {
            "label": "First-Time Pass",
            "value": f"{first_time_pass_rate * 100:.1f}%",
            "detail": f"quality={quality_score_avg:.2f} across {responses_total} responses",
            "kind": "quality",
        }
    return {
        "label": "Awaiting Data",
        "value": "0",
        "detail": "No response metrics recorded yet.",
        "kind": "idle",
    }


def _empty_response_metrics(error: str = "") -> dict[str, Any]:
    message = str(error).strip()
    return {
        "timestamp": _now_iso(),
        "summary_file": "",
        "sources": [],
        "ok": False,
        "partial": True,
        "errors": [message] if message else [],
        "responses_total": 0,
        "quality_score_avg": 0.0,
        "latency_sec_avg": 0.0,
        "prompt_difficulty_score_avg": 0.0,
        "first_time_pass_count": 0,
        "first_time_pass_rate": 0.0,
        "acceptance_pass_count": 0,
        "acceptance_pass_rate": 0.0,
        "exact_cost_count": 0,
        "exact_cost_coverage": 0.0,
        "cost_usd_total": 0.0,
        "cost_usd_avg": 0.0,
        "tokens_total": 0,
        "tokens_input_total": 0,
        "tokens_output_total": 0,
        "tokens_avg": 0.0,
        "token_exact_count": 0,
        "token_exact_coverage": 0.0,
        "token_rate_per_minute": 0.0,
        "currency": "USD",
        "by_owner": {},
        "latest_metric": {},
        "optimization_recommendations": [],
        "exciting_stat": {
            "label": "Awaiting Data",
            "value": "0",
            "detail": "No response metrics recorded yet.",
            "kind": "idle",
        },
    }


def _response_metrics_snapshot(config: ManagerConfig, lane_items: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_paths: list[Path] = [config.metrics_summary_file]
    for lane in lane_items:
        raw = str(lane.get("metrics_summary_file", "")).strip()
        if not raw:
            continue
        candidate_paths.append(Path(raw))

    paths: list[Path] = []
    seen: set[str] = set()
    for path in candidate_paths:
        normalized = str(path.resolve())
        if normalized in seen:
            continue
        seen.add(normalized)
        paths.append(path.resolve())

    reports: list[dict[str, Any]] = []
    errors: list[str] = []
    recommendations: list[str] = []
    by_owner: dict[str, dict[str, Any]] = {}
    latest_metric: dict[str, Any] = {}
    latest_timestamp = ""

    responses_total = 0
    quality_sum = 0.0
    latency_sum = 0.0
    prompt_difficulty_sum = 0.0
    first_time_pass_count = 0
    acceptance_pass_count = 0
    exact_cost_count = 0
    cost_usd_total = 0.0
    tokens_total = 0
    tokens_input_total = 0
    tokens_output_total = 0
    token_exact_count = 0
    currency = "USD"

    for path in paths:
        report: dict[str, Any] = {
            "path": str(path),
            "ok": True,
            "missing": False,
            "responses_total": 0,
            "cost_usd_total": 0.0,
            "tokens_total": 0,
            "error": "",
        }
        if not path.exists():
            report["missing"] = True
            report["note"] = "metrics summary not created yet"
            reports.append(report)
            continue

        summary = _read_json_file(path)
        if not summary:
            report["note"] = "metrics summary is empty"
            reports.append(report)
            continue

        if not isinstance(summary, dict):
            report["ok"] = False
            report["error"] = "metrics summary must be a JSON object"
            errors.append(f"{path}: {report['error']}")
            reports.append(report)
            continue

        source_responses = _int_value(summary.get("responses_total", 0), 0)
        source_quality_sum = _float_value(
            summary.get("quality_score_sum", _float_value(summary.get("quality_score_avg", 0.0), 0.0) * source_responses),
            0.0,
        )
        source_latency_sum = _float_value(
            summary.get("latency_sec_sum", _float_value(summary.get("latency_sec_avg", 0.0), 0.0) * source_responses),
            0.0,
        )
        source_prompt_difficulty_sum = _float_value(
            summary.get(
                "prompt_difficulty_score_sum",
                _float_value(summary.get("prompt_difficulty_score_avg", 0.0), 0.0) * source_responses,
            ),
            0.0,
        )
        source_first_time_pass = _int_value(
            summary.get("first_time_pass_count", round(_float_value(summary.get("first_time_pass_rate", 0.0), 0.0) * source_responses)),
            0,
        )
        source_acceptance_pass = _int_value(
            summary.get("acceptance_pass_count", round(_float_value(summary.get("acceptance_pass_rate", 0.0), 0.0) * source_responses)),
            0,
        )
        source_exact_cost = _int_value(
            summary.get("exact_cost_count", round(_float_value(summary.get("exact_cost_coverage", 0.0), 0.0) * source_responses)),
            0,
        )
        source_cost = _float_value(summary.get("cost_usd_total", 0.0), 0.0)
        source_tokens_total = _int_value(summary.get("tokens_total", 0), 0)
        source_tokens_input_total = _int_value(summary.get("tokens_input_total", 0), 0)
        source_tokens_output_total = _int_value(summary.get("tokens_output_total", 0), 0)
        source_token_exact_count = _int_value(
            summary.get("token_exact_count", round(_float_value(summary.get("token_exact_coverage", 0.0), 0.0) * source_responses)),
            0,
        )

        responses_total += source_responses
        quality_sum += source_quality_sum
        latency_sum += source_latency_sum
        prompt_difficulty_sum += source_prompt_difficulty_sum
        first_time_pass_count += source_first_time_pass
        acceptance_pass_count += source_acceptance_pass
        exact_cost_count += source_exact_cost
        cost_usd_total += source_cost
        tokens_total += source_tokens_total
        tokens_input_total += source_tokens_input_total
        tokens_output_total += source_tokens_output_total
        token_exact_count += source_token_exact_count

        report["responses_total"] = source_responses
        report["cost_usd_total"] = round(source_cost, 8)
        report["tokens_total"] = source_tokens_total
        reports.append(report)

        owner_map = summary.get("by_owner", {})
        if isinstance(owner_map, dict):
            for owner_name, owner_payload in owner_map.items():
                if not isinstance(owner_payload, dict):
                    continue
                owner = str(owner_name)
                aggregate_owner = by_owner.get(
                    owner,
                    {
                        "responses": 0,
                        "first_time_pass": 0,
                        "validation_passed": 0,
                        "cost_usd_total": 0.0,
                        "tokens_total": 0,
                    },
                )
                aggregate_owner["responses"] = _int_value(aggregate_owner.get("responses", 0), 0) + _int_value(
                    owner_payload.get("responses", 0),
                    0,
                )
                aggregate_owner["first_time_pass"] = _int_value(
                    aggregate_owner.get("first_time_pass", 0),
                    0,
                ) + _int_value(owner_payload.get("first_time_pass", 0), 0)
                aggregate_owner["validation_passed"] = _int_value(
                    aggregate_owner.get("validation_passed", 0),
                    0,
                ) + _int_value(owner_payload.get("validation_passed", 0), 0)
                aggregate_owner["cost_usd_total"] = _float_value(aggregate_owner.get("cost_usd_total", 0.0), 0.0) + _float_value(
                    owner_payload.get("cost_usd_total", 0.0),
                    0.0,
                )
                aggregate_owner["tokens_total"] = _int_value(aggregate_owner.get("tokens_total", 0), 0) + _int_value(
                    owner_payload.get("tokens_total", 0),
                    0,
                )
                by_owner[owner] = aggregate_owner

        latest = summary.get("latest_metric", {})
        if isinstance(latest, dict):
            ts = str(latest.get("timestamp", "")).strip()
            if ts and ts >= latest_timestamp:
                latest_timestamp = ts
                latest_metric = latest

        for item in summary.get("optimization_recommendations", []):
            text = str(item).strip()
            if text and text not in recommendations:
                recommendations.append(text)
        currency = str(summary.get("currency", currency)).strip() or currency

    for owner_payload in by_owner.values():
        owner_responses = max(1, _int_value(owner_payload.get("responses", 0), 0))
        owner_payload["first_time_pass_rate"] = round(
            _int_value(owner_payload.get("first_time_pass", 0), 0) / owner_responses,
            6,
        )
        owner_payload["validation_pass_rate"] = round(
            _int_value(owner_payload.get("validation_passed", 0), 0) / owner_responses,
            6,
        )
        owner_payload["cost_usd_total"] = round(_float_value(owner_payload.get("cost_usd_total", 0.0), 0.0), 8)
        owner_payload["tokens_total"] = _int_value(owner_payload.get("tokens_total", 0), 0)
        owner_payload["tokens_avg"] = round(
            _int_value(owner_payload.get("tokens_total", 0), 0) / owner_responses,
            6,
        )

    coverage = round(exact_cost_count / max(1, responses_total), 6)
    token_exact_coverage = round(token_exact_count / max(1, responses_total), 6)
    token_rate_per_minute = 0.0
    if latency_sum > 0.0:
        token_rate_per_minute = (float(tokens_total) / latency_sum) * 60.0
    snapshot = {
        "timestamp": _now_iso(),
        "summary_file": str(config.metrics_summary_file),
        "sources": reports,
        "ok": len(errors) == 0,
        "partial": len(errors) > 0,
        "errors": errors,
        "responses_total": responses_total,
        "quality_score_avg": round(quality_sum / max(1, responses_total), 6),
        "latency_sec_avg": round(latency_sum / max(1, responses_total), 6),
        "prompt_difficulty_score_avg": round(prompt_difficulty_sum / max(1, responses_total), 6),
        "first_time_pass_count": first_time_pass_count,
        "first_time_pass_rate": round(first_time_pass_count / max(1, responses_total), 6),
        "acceptance_pass_count": acceptance_pass_count,
        "acceptance_pass_rate": round(acceptance_pass_count / max(1, responses_total), 6),
        "exact_cost_count": exact_cost_count,
        "exact_cost_coverage": coverage,
        "cost_usd_total": round(cost_usd_total, 8),
        "cost_usd_avg": round(cost_usd_total / max(1, responses_total), 8),
        "tokens_total": tokens_total,
        "tokens_input_total": tokens_input_total,
        "tokens_output_total": tokens_output_total,
        "tokens_avg": round(tokens_total / max(1, responses_total), 6),
        "token_exact_count": token_exact_count,
        "token_exact_coverage": token_exact_coverage,
        "token_rate_per_minute": round(token_rate_per_minute, 6),
        "currency": currency,
        "by_owner": by_owner,
        "latest_metric": latest_metric,
        "optimization_recommendations": recommendations,
    }
    snapshot["exciting_stat"] = _build_exciting_stat(snapshot)
    return snapshot


def monitor_snapshot(config: ManagerConfig) -> dict[str, Any]:
    status = status_snapshot(config)
    diagnostics: dict[str, Any] = {"ok": True, "errors": [], "sources": {}}

    def _mark_source(name: str, ok: bool, error: str = "") -> None:
        diagnostics["sources"][name] = {"ok": bool(ok), "error": error}
        if not ok:
            diagnostics["ok"] = False
            if error:
                diagnostics["errors"].append(f"{name}: {error}")

    lanes: dict[str, Any] = {
        "timestamp": _now_iso(),
        "lanes_file": str(config.lanes_file),
        "running_count": 0,
        "total_count": 0,
        "lanes": [],
        "health_counts": {},
        "owner_counts": {},
        "ok": False,
        "errors": [],
    }
    try:
        lanes = lane_status_snapshot(config)
    except Exception as err:
        lanes["errors"] = [str(err)]
        lanes["error"] = str(err)
    _mark_source("lanes", bool(lanes.get("ok", False)), "; ".join(lanes.get("errors", [])))
    progress = _combined_progress_snapshot(config, lanes)

    conv: dict[str, Any] = {
        "timestamp": _now_iso(),
        "conversation_files": [str(config.conversation_log_file)],
        "total_events": 0,
        "events": [],
        "owner_counts": {},
        "ok": False,
        "partial": True,
        "errors": [],
        "sources": [],
    }
    try:
        conv = conversations_snapshot(config, lines=60, include_lanes=True)
    except Exception as err:
        conv["errors"] = [str(err)]
        conv["partial"] = True
    _mark_source("conversations", bool(conv.get("ok", False)), "; ".join(conv.get("errors", [])))
    conv_events = conv.get("events", [])
    if not isinstance(conv_events, list):
        conv_events = []
    recent_conversations = [item for item in conv_events if isinstance(item, dict)][-20:]
    lane_items = lanes.get("lanes", []) if isinstance(lanes.get("lanes", []), list) else []
    try:
        response_metrics = _response_metrics_snapshot(config, lane_items)
    except Exception as err:
        response_metrics = _empty_response_metrics(str(err))
    _mark_source("response_metrics", bool(response_metrics.get("ok", False)), "; ".join(response_metrics.get("errors", [])))
    lane_operational_states = {"ok", "paused", "idle"}
    operational_lanes = [
        lane
        for lane in lane_items
        if str(lane.get("health", "")).strip().lower() in lane_operational_states
    ]
    degraded_lanes = [
        lane
        for lane in lane_items
        if str(lane.get("health", "")).strip().lower() not in lane_operational_states
    ]

    try:
        impl_repo = _repo_monitor_snapshot(config.impl_repo)
    except Exception as err:
        impl_repo = {
            "path": str(config.impl_repo),
            "ok": False,
            "error": str(err),
            "branch": "",
            "head": "",
            "dirty": False,
            "changed_files": 0,
        }
    _mark_source("implementation_repo", bool(impl_repo.get("ok", False)), str(impl_repo.get("error", "")))

    try:
        test_repo = _repo_monitor_snapshot(config.test_repo)
    except Exception as err:
        test_repo = {
            "path": str(config.test_repo),
            "ok": False,
            "error": str(err),
            "branch": "",
            "head": "",
            "dirty": False,
            "changed_files": 0,
        }
    _mark_source("tests_repo", bool(test_repo.get("ok", False)), str(test_repo.get("error", "")))

    latest_log_line = ""
    log_error = ""
    try:
        latest_log_line = tail_logs(config, lines=1, latest_run_only=True).strip()
    except Exception as err:
        log_error = str(err)
        latest_log_line = f"log read error: {err}"
    _mark_source("logs", log_error == "", log_error)
    handoff_dir = (config.artifacts_dir / "handoffs").resolve()
    to_codex_file = handoff_dir / "to_codex.ndjson"
    to_gemini_file = handoff_dir / "to_gemini.ndjson"
    handoff_errors: list[str] = []
    try:
        to_codex_events = _tail_ndjson(to_codex_file, 500)
    except Exception as err:
        to_codex_events = []
        handoff_errors.append(f"to_codex: {err}")
    try:
        to_gemini_events = _tail_ndjson(to_gemini_file, 500)
    except Exception as err:
        to_gemini_events = []
        handoff_errors.append(f"to_gemini: {err}")
    _mark_source("handoffs", len(handoff_errors) == 0, "; ".join(handoff_errors))

    snapshot = {
        "timestamp": _now_iso(),
        "status": status,
        "runtime": {
            "primary_runner_running": bool(status.get("runner_running", False)),
            "lane_agents_running": int(lanes.get("running_count", 0)) > 0,
            "effective_agents_running": bool(status.get("runner_running", False)) or int(lanes.get("running_count", 0)) > 0,
            "lane_operational_count": len(operational_lanes),
            "lane_degraded_count": len(degraded_lanes),
            "lane_health_counts": lanes.get("health_counts", {}),
            "lane_owner_health": lanes.get("owner_counts", {}),
        },
        "progress": progress,
        "lanes": lanes,
        "response_metrics": response_metrics,
        "conversations": {
            "total_events": conv.get("total_events", 0),
            "owner_counts": conv.get("owner_counts", {}),
            "latest": (recent_conversations[-1] if recent_conversations else {}),
            "recent_events": recent_conversations,
            "partial": bool(conv.get("partial", False)),
            "errors": conv.get("errors", []),
            "sources": conv.get("sources", []),
        },
        "repos": {
            "implementation": impl_repo,
            "tests": test_repo,
        },
        "handoffs": {
            "dir": str(handoff_dir),
            "to_codex_events": len(to_codex_events),
            "to_gemini_events": len(to_gemini_events),
            "latest_to_codex": (to_codex_events[-1] if to_codex_events else {}),
            "latest_to_gemini": (to_gemini_events[-1] if to_gemini_events else {}),
        },
        "latest_log_line": latest_log_line,
        "diagnostics": diagnostics,
    }
    monitor_file = config.artifacts_dir / "monitor.json"
    try:
        config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        monitor_file.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        snapshot["monitor_file"] = str(monitor_file)
    except Exception as err:
        _mark_source("monitor_file", False, str(err))
        snapshot["monitor_file"] = ""
    return snapshot


def render_monitor_text(snapshot: dict[str, Any]) -> str:
    status = snapshot.get("status", {})
    progress = snapshot.get("progress", {})
    counts = progress.get("counts", {})
    active = progress.get("active_tasks", [])
    repos = snapshot.get("repos", {})
    handoffs = snapshot.get("handoffs", {})
    lanes = snapshot.get("lanes", {})
    response_metrics = snapshot.get("response_metrics", {})
    exciting_stat = response_metrics.get("exciting_stat", {}) if isinstance(response_metrics.get("exciting_stat", {}), dict) else {}
    conversations = snapshot.get("conversations", {})
    diagnostics = snapshot.get("diagnostics", {})
    lane_owner_counts = lanes.get("owner_counts", {}) if isinstance(lanes.get("owner_counts", {}), dict) else {}
    owner_summary_parts: list[str] = []
    for owner in sorted(lane_owner_counts):
        payload = lane_owner_counts.get(owner, {})
        if not isinstance(payload, dict):
            continue
        owner_summary_parts.append(
            f"{owner}(total={payload.get('total', 0)},running={payload.get('running', 0)},"
            f"healthy={payload.get('healthy', 0)},degraded={payload.get('degraded', 0)})"
        )
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
            f"upstream={payload.get('upstream', '')} "
            f"sync={payload.get('sync_state', '')} "
            f"ahead={payload.get('ahead', -1)} "
            f"behind={payload.get('behind', -1)} "
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
        f"lane_owners: {' | '.join(owner_summary_parts) if owner_summary_parts else 'none'}",
        (
            f"conversations: events={conversations.get('total_events', 0)} "
            f"owners={conversations.get('owner_counts', {})}"
        ),
        (
            f"handoffs: to_codex={handoffs.get('to_codex_events', 0)} "
            f"to_gemini={handoffs.get('to_gemini_events', 0)}"
        ),
        (
            f"diagnostics: ok={diagnostics.get('ok', True)} "
            f"errors={len(diagnostics.get('errors', []))}"
        ),
        (
            f"response_metrics: responses={response_metrics.get('responses_total', 0)} "
            f"first_time_pass_rate={response_metrics.get('first_time_pass_rate', 0.0)} "
            f"latency_avg={response_metrics.get('latency_sec_avg', 0.0)}s "
            f"cost_total=${response_metrics.get('cost_usd_total', 0.0)}"
        ),
        (
            f"exciting_stat: {exciting_stat.get('label', 'n/a')}={exciting_stat.get('value', 'n/a')} "
            f"({exciting_stat.get('detail', '')})"
        ),
        _repo_line("impl_repo", impl),
        _repo_line("test_repo", tests),
    ]
    if diagnostics.get("errors"):
        lines.append(f"diagnostic_errors: {' | '.join(str(item) for item in diagnostics['errors'])}")
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


def _read_lane_items(config: ManagerConfig) -> tuple[list[dict[str, Any]], list[str]]:
    if not config.lanes_file.exists():
        return [], []
    try:
        raw = json.loads(config.lanes_file.read_text(encoding="utf-8"))
    except Exception as err:
        return [], [f"lanes_file: {err}"]
    lane_items = raw.get("lanes", []) if isinstance(raw, dict) else raw
    if not isinstance(lane_items, list):
        return [], [f"Lane file must contain a list of lanes: {config.lanes_file}"]

    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(lane_items):
        if not isinstance(item, dict):
            errors.append(f"lane[{index}]: entry must be an object")
            continue
        out.append(item)
    return out, errors


def _build_lane_spec(config: ManagerConfig, item: dict[str, Any]) -> dict[str, Any] | None:
    lane_id = str(item.get("id", "")).strip()
    owner = str(item.get("owner", "")).strip().lower()
    if not lane_id:
        return None
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
    metrics_file = _resolve_path(
        config.root_dir,
        str(item.get("metrics_file", "")),
        artifacts_dir / "response_metrics.ndjson",
    )
    metrics_summary_file = _resolve_path(
        config.root_dir,
        str(item.get("metrics_summary_file", "")),
        artifacts_dir / "response_metrics_summary.json",
    )
    pricing_file = _resolve_path(
        config.root_dir,
        str(item.get("pricing_file", "")),
        config.pricing_file,
    )
    mcp_raw = str(item.get("mcp_context_file", "")).strip()
    mcp_default = config.mcp_context_file or (config.root_dir / "config" / "mcp_context.example.json")
    if mcp_raw or config.mcp_context_file is not None:
        mcp_context_file: Path | None = _resolve_path(config.root_dir, mcp_raw, mcp_default)
    else:
        mcp_context_file = None

    return {
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
        "dependency_state_file": _resolve_path(
            config.root_dir,
            str(item.get("dependency_state_file", "")),
            config.state_file,
        ),
        "handoff_dir": _resolve_path(
            config.root_dir,
            str(item.get("handoff_dir", "")),
            config.artifacts_dir / "handoffs",
        ),
        "artifacts_dir": artifacts_dir,
        "heartbeat_file": heartbeat_file,
        "lock_file": lock_file,
        "conversation_log_file": conversation_log_file,
        "metrics_file": metrics_file,
        "metrics_summary_file": metrics_summary_file,
        "pricing_file": pricing_file,
        "owner_filter": [owner],
        "validate_commands": [
            str(cmd).strip() for cmd in item.get("validate_commands", config.validate_commands) if str(cmd).strip()
        ],
        "exclusive_paths": [str(path).strip() for path in item.get("exclusive_paths", []) if str(path).strip()],
        "runtime_dir": runtime_dir,
    }


def _load_lane_specs_resilient(config: ManagerConfig) -> tuple[list[dict[str, Any]], list[str]]:
    lane_items, errors = _read_lane_items(config)
    lanes: list[dict[str, Any]] = []
    for index, item in enumerate(lane_items):
        lane_label = str(item.get("id", "")).strip() or f"lane[{index}]"
        try:
            lane = _build_lane_spec(config, item)
        except Exception as err:
            errors.append(f"{lane_label}: {err}")
            continue
        if lane is not None:
            lanes.append(lane)
    return lanes, errors


def load_lane_specs(config: ManagerConfig) -> list[dict[str, Any]]:
    lanes, errors = _load_lane_specs_resilient(config)
    if errors:
        raise RuntimeError("; ".join(errors))
    return lanes


def _lane_pid_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "lane.pid").resolve()


def _lane_meta_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "lane.json").resolve()


def _lane_log_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "runner.log").resolve()


def _lane_events_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "events.ndjson").resolve()


def _lane_pause_file(config: ManagerConfig, lane_id: str) -> Path:
    return (config.lanes_runtime_dir / lane_id / "paused.flag").resolve()


def _append_lane_event(config: ManagerConfig, lane_id: str, event_type: str, payload: dict[str, Any]) -> None:
    path = _lane_events_file(config, lane_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": _now_iso(),
        "lane_id": lane_id,
        "event_type": event_type,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _read_lane_state_counts(state_file: Path) -> dict[str, int]:
    counts = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 0}
    if not state_file.exists():
        return counts
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        counts["unknown"] += 1
        return counts
    if not isinstance(raw, dict):
        counts["unknown"] += 1
        return counts
    for item in raw.values():
        status = "unknown"
        if isinstance(item, dict):
            status = str(item.get("status", "unknown")).strip().lower()
        if status in counts:
            counts[status] += 1
        else:
            counts["unknown"] += 1
    return counts


def _read_lane_state_progress(state_file: Path, tasks_file: Path) -> dict[str, Any]:
    task_ids: list[str] = []
    try:
        raw_tasks = json.loads(tasks_file.read_text(encoding="utf-8")) if tasks_file.exists() else []
    except Exception:
        raw_tasks = []
    if isinstance(raw_tasks, list):
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id", "")).strip()
            if task_id:
                task_ids.append(task_id)

    if not task_ids:
        counts = _read_lane_state_counts(state_file)
        known_total = sum(counts.get(key, 0) for key in ("pending", "in_progress", "done", "blocked"))
        return {
            "counts": counts,
            "task_total": known_total,
            "state_entries": known_total + int(counts.get("unknown", 0)),
            "missing_state_entries": 0,
            "extra_state_entries": 0,
        }

    raw_state: dict[str, Any] = {}
    if state_file.exists():
        try:
            candidate = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                raw_state = candidate
        except Exception:
            raw_state = {}

    counts = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 0}
    covered = 0
    for task_id in task_ids:
        payload = raw_state.get(task_id, {})
        status = "pending"
        if isinstance(payload, dict):
            status = str(payload.get("status", "pending")).strip().lower()
        if status in counts:
            counts[status] += 1
        else:
            counts["unknown"] += 1
        if task_id in raw_state:
            covered += 1

    extra = 0
    for key in raw_state:
        if str(key) not in task_ids:
            extra += 1

    return {
        "counts": counts,
        "task_total": len(task_ids),
        "state_entries": len(raw_state),
        "missing_state_entries": max(0, len(task_ids) - covered),
        "extra_state_entries": extra,
    }


def _lane_health_state(
    *,
    running: bool,
    heartbeat_age_sec: int,
    heartbeat_stale: bool,
    paused: bool,
    state_counts: dict[str, int],
) -> str:
    if paused and not running:
        return "paused"
    if not running:
        if int(state_counts.get("in_progress", 0)) > 0:
            return "stopped_unexpected"
        if (
            int(state_counts.get("done", 0)) > 0
            and int(state_counts.get("pending", 0)) == 0
            and int(state_counts.get("blocked", 0)) == 0
        ):
            return "idle"
        return "stopped"
    if heartbeat_stale:
        return "stale"
    if heartbeat_age_sec == -1:
        return "unknown"
    return "ok"


def lane_status_snapshot(config: ManagerConfig) -> dict[str, Any]:
    lanes, load_errors = _load_lane_specs_resilient(config)
    snapshots: list[dict[str, Any]] = []
    errors: list[str] = list(load_errors)
    for lane in lanes:
        lane_id = lane["id"]
        pid_path = _lane_pid_file(config, lane_id)
        log_path = _lane_log_file(config, lane_id)
        meta_path = _lane_meta_file(config, lane_id)
        events_path = _lane_events_file(config, lane_id)
        pause_path = _lane_pause_file(config, lane_id)
        heartbeat_file = Path(lane["heartbeat_file"])
        try:
            pid = _read_pid(pid_path)
            running = _pid_running(pid)
            if not running:
                lock_pid = _read_runner_lock_pid(Path(lane["lock_file"]))
                if lock_pid and _pid_running(lock_pid):
                    pid = lock_pid
                    running = True
                    _write_pid(pid_path, lock_pid)
            meta = _read_json_file(meta_path)
            expected_build_id = _lane_build_id(config, lane)
            build_id = str(meta.get("build_id", "")).strip()
            build_current = bool(build_id and build_id == expected_build_id)
            paused = pause_path.exists()
            state_progress = _read_lane_state_progress(Path(lane["state_file"]), Path(lane["tasks_file"]))
            state_counts = state_progress["counts"]
            last_event = _tail_ndjson(events_path, 1)
            latest = ""
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8").splitlines()
                latest = lines[-1] if lines else ""
            heartbeat_age_sec = _heartbeat_age_sec_from_file(heartbeat_file)
            heartbeat_stale = heartbeat_age_sec != -1 and heartbeat_age_sec > config.heartbeat_stale_sec
            health = _lane_health_state(
                running=running,
                heartbeat_age_sec=heartbeat_age_sec,
                heartbeat_stale=heartbeat_stale,
                paused=paused,
                state_counts=state_counts,
            )
            snapshots.append(
                {
                    "id": lane_id,
                    "enabled": lane["enabled"],
                    "owner": lane["owner"],
                    "description": lane["description"],
                    "running": running,
                    "pid": pid,
                    "tasks_file": str(lane["tasks_file"]),
                    "dependency_state_file": str(lane["dependency_state_file"]),
                    "handoff_dir": str(lane["handoff_dir"]),
                    "objective_file": str(lane["objective_file"]),
                    "impl_repo": str(lane["impl_repo"]),
                    "test_repo": str(lane["test_repo"]),
                    "metrics_file": str(lane["metrics_file"]),
                    "metrics_summary_file": str(lane["metrics_summary_file"]),
                    "pricing_file": str(lane["pricing_file"]),
                    "exclusive_paths": lane["exclusive_paths"],
                    "latest_log_line": latest,
                    "log_file": str(log_path),
                    "pid_file": str(pid_path),
                    "meta_file": str(meta_path),
                    "events_file": str(events_path),
                    "pause_file": str(pause_path),
                    "heartbeat_file": str(heartbeat_file),
                    "heartbeat_age_sec": heartbeat_age_sec,
                    "heartbeat_stale": heartbeat_stale,
                    "paused": paused,
                    "build_id": build_id,
                    "expected_build_id": expected_build_id,
                    "build_current": build_current,
                    "state_counts": state_counts,
                    "task_total": int(state_progress.get("task_total", 0)),
                    "state_entries": int(state_progress.get("state_entries", 0)),
                    "missing_state_entries": int(state_progress.get("missing_state_entries", 0)),
                    "extra_state_entries": int(state_progress.get("extra_state_entries", 0)),
                    "last_event": last_event[0] if last_event else {},
                    "health": health,
                    "meta": meta,
                }
            )
        except Exception as err:
            errors.append(f"{lane_id}: {err}")
            snapshots.append(
                {
                    "id": lane_id,
                    "enabled": lane["enabled"],
                    "owner": lane["owner"],
                    "description": lane["description"],
                    "running": False,
                    "pid": None,
                    "tasks_file": str(lane["tasks_file"]),
                    "dependency_state_file": str(lane["dependency_state_file"]),
                    "handoff_dir": str(lane["handoff_dir"]),
                    "objective_file": str(lane["objective_file"]),
                    "impl_repo": str(lane["impl_repo"]),
                    "test_repo": str(lane["test_repo"]),
                    "metrics_file": str(lane["metrics_file"]),
                    "metrics_summary_file": str(lane["metrics_summary_file"]),
                    "pricing_file": str(lane["pricing_file"]),
                    "exclusive_paths": lane["exclusive_paths"],
                    "latest_log_line": "",
                    "log_file": str(log_path),
                    "pid_file": str(pid_path),
                    "meta_file": str(meta_path),
                    "events_file": str(events_path),
                    "pause_file": str(pause_path),
                    "heartbeat_file": str(heartbeat_file),
                    "heartbeat_age_sec": -1,
                    "heartbeat_stale": False,
                    "paused": False,
                    "build_id": "",
                    "expected_build_id": _lane_build_id(config, lane),
                    "build_current": False,
                    "state_counts": {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 1},
                    "task_total": 0,
                    "state_entries": 0,
                    "missing_state_entries": 0,
                    "extra_state_entries": 0,
                    "last_event": {},
                    "health": "error",
                    "error": str(err),
                    "meta": {},
                }
            )
    health_counts: dict[str, int] = {}
    owner_counts: dict[str, dict[str, int]] = {}
    healthy_states = {"ok", "paused", "idle"}
    for lane in snapshots:
        health = str(lane.get("health", "unknown")).strip().lower() or "unknown"
        health_counts[health] = health_counts.get(health, 0) + 1
        owner = str(lane.get("owner", "unknown")).strip() or "unknown"
        owner_entry = owner_counts.setdefault(owner, {"total": 0, "running": 0, "healthy": 0, "degraded": 0})
        owner_entry["total"] += 1
        if bool(lane.get("running", False)):
            owner_entry["running"] += 1
        if health in healthy_states:
            owner_entry["healthy"] += 1
        else:
            owner_entry["degraded"] += 1
    return {
        "timestamp": _now_iso(),
        "lanes_file": str(config.lanes_file),
        "running_count": sum(1 for lane in snapshots if lane["running"]),
        "total_count": len(snapshots),
        "lanes": snapshots,
        "health_counts": health_counts,
        "owner_counts": owner_counts,
        "ok": len(errors) == 0,
        "errors": errors,
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
        "--dependency-state-file",
        str(lane["dependency_state_file"]),
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
        "--handoff-dir",
        str(lane["handoff_dir"]),
        "--metrics-file",
        str(lane["metrics_file"]),
        "--metrics-summary-file",
        str(lane["metrics_summary_file"]),
        "--pricing-file",
        str(lane["pricing_file"]),
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
        "--continuous",
        "--continuous-recycle-delay-sec",
        "90",
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
    _append_lane_event(config, lane_id, "start_requested", {"owner": lane["owner"]})

    cmd_name = _lane_command_for_owner(config, lane["owner"])
    if _resolve_binary(cmd_name) is None:
        _append_lane_event(config, lane_id, "start_failed", {"reason": f"missing_cli:{cmd_name}"})
        raise RuntimeError(f"{lane['owner']} CLI not found in PATH: {cmd_name}")

    for repo in {lane["impl_repo"], lane["test_repo"]}:
        ok, message = _repo_basic_check(repo)
        if not ok:
            _append_lane_event(config, lane_id, "start_failed", {"reason": message})
            raise RuntimeError(f"Lane {lane_id}: {message}")
    for required_file_key in ("tasks_file", "objective_file", "schema_file", "skill_protocol_file"):
        path = lane[required_file_key]
        if not path.exists():
            _append_lane_event(
                config,
                lane_id,
                "start_failed",
                {"reason": f"missing_{required_file_key}", "path": str(path)},
            )
            raise RuntimeError(f"Lane {lane_id}: missing {required_file_key} at {path}")

    pid_path = _lane_pid_file(config, lane_id)
    log_path = _lane_log_file(config, lane_id)
    meta_path = _lane_meta_file(config, lane_id)
    existing_pid = _read_pid(pid_path)
    if _pid_running(existing_pid):
        _append_lane_event(config, lane_id, "start_skipped", {"reason": "already_running", "pid": existing_pid})
        status = lane_status_snapshot(config)
        existing = next((item for item in status["lanes"] if item["id"] == lane_id), None)
        return existing or {"id": lane_id, "running": True, "pid": existing_pid}
    lock_pid = _read_runner_lock_pid(Path(lane["lock_file"]))
    if lock_pid and _pid_running(lock_pid):
        _write_pid(pid_path, lock_pid)
        _append_lane_event(config, lane_id, "start_skipped", {"reason": "lock_pid_running", "pid": lock_pid})
        status = lane_status_snapshot(config)
        existing = next((item for item in status["lanes"] if item["id"] == lane_id), None)
        return existing or {"id": lane_id, "running": True, "pid": lock_pid}

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
    _lane_pause_file(config, lane_id).unlink(missing_ok=True)
    _write_json_file(
        meta_path,
        {
            "started_at": _now_iso(),
            "lane_id": lane_id,
            "owner": lane["owner"],
            "command": cmd,
            "tasks_file": str(lane["tasks_file"]),
            "dependency_state_file": str(lane["dependency_state_file"]),
            "handoff_dir": str(lane["handoff_dir"]),
            "objective_file": str(lane["objective_file"]),
            "conversation_log_file": str(lane["conversation_log_file"]),
            "metrics_file": str(lane["metrics_file"]),
            "metrics_summary_file": str(lane["metrics_summary_file"]),
            "pricing_file": str(lane["pricing_file"]),
            "build_id": _lane_build_id(config, lane),
            "exclusive_paths": lane["exclusive_paths"],
        },
    )
    _append_lane_event(
        config,
        lane_id,
        "started",
        {
            "pid": proc.pid,
            "owner": lane["owner"],
            "log_file": str(log_path),
            "tasks_file": str(lane["tasks_file"]),
        },
    )
    time.sleep(0.4)
    if not _pid_running(proc.pid):
        pid_path.unlink(missing_ok=True)
        log_tail = ""
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            log_tail = "\n".join(lines[-8:])
        _append_lane_event(
            config,
            lane_id,
            "start_failed",
            {
                "reason": "process_exited_early",
                "pid": proc.pid,
                "log_tail": log_tail[-1200:],
            },
        )
        raise RuntimeError(f"Lane {lane_id} exited immediately after start.")
    return next((item for item in lane_status_snapshot(config)["lanes"] if item["id"] == lane_id), {"id": lane_id})


def stop_lane_background(config: ManagerConfig, lane_id: str, *, reason: str = "manual") -> dict[str, Any]:
    pid_path = _lane_pid_file(config, lane_id)
    pid = _read_pid(pid_path)
    lanes = load_lane_specs(config)
    lane = next((item for item in lanes if item["id"] == lane_id), None)
    lock_pid = _read_runner_lock_pid(Path(lane["lock_file"])) if lane else None
    _append_lane_event(config, lane_id, "stop_requested", {"pid": pid, "lock_pid": lock_pid, "reason": reason})
    targets: list[int] = []
    for candidate in (pid, lock_pid):
        if candidate and candidate not in targets:
            targets.append(candidate)
    for target in targets:
        _terminate_pid(target)
    pid_path.unlink(missing_ok=True)
    if reason == "manual":
        _lane_pause_file(config, lane_id).write_text("manual\n", encoding="utf-8")
    else:
        _lane_pause_file(config, lane_id).unlink(missing_ok=True)
    _append_lane_event(config, lane_id, "stopped", {"pid": pid, "reason": reason})
    status = lane_status_snapshot(config)
    return next((item for item in status["lanes"] if item["id"] == lane_id), {"id": lane_id, "running": False})


def ensure_lanes_background(config: ManagerConfig, lane_id: str | None = None) -> dict[str, Any]:
    lanes = load_lane_specs(config)
    selected = [lane for lane in lanes if lane["enabled"]] if lane_id is None else [lane for lane in lanes if lane["id"] == lane_id]
    if lane_id is not None and not selected:
        raise RuntimeError(f"Unknown lane id {lane_id!r}. Update {config.lanes_file}.")
    ensured: list[dict[str, Any]] = []
    started: list[dict[str, Any]] = []
    restarted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    snapshot = lane_status_snapshot(config)
    by_id = {lane["id"]: lane for lane in snapshot.get("lanes", [])}

    for lane in selected:
        if not lane["enabled"]:
            skipped.append({"id": lane["id"], "reason": "disabled"})
            continue
        lane_id = lane["id"]
        pause_file = _lane_pause_file(config, lane_id)
        current = by_id.get(lane_id, {})
        running = bool(current.get("running", False))
        stale = bool(current.get("heartbeat_stale", False))
        build_current = bool(current.get("build_current", False))
        if pause_file.exists():
            skipped.append({"id": lane_id, "reason": "manually_paused"})
            continue
        if running and not stale and build_current:
            ensured.append({"id": lane_id, "status": "running"})
            continue
        try:
            if running and (stale or not build_current):
                reason = "stale_heartbeat" if stale else "build_update"
                stop_lane_background(config, lane_id, reason=reason)
                started_lane = start_lane_background(config, lane_id)
                restarted.append({"id": lane_id, "status": "restarted", "pid": started_lane.get("pid")})
                _append_lane_event(config, lane_id, "auto_restarted", {"reason": reason})
            else:
                started_lane = start_lane_background(config, lane_id)
                started.append({"id": lane_id, "status": "started", "pid": started_lane.get("pid")})
                _append_lane_event(config, lane_id, "auto_started", {"reason": "not_running"})
        except Exception as err:
            failed.append({"id": lane_id, "error": str(err)})
            _append_lane_event(config, lane_id, "ensure_failed", {"error": str(err)})

    return {
        "timestamp": _now_iso(),
        "requested_lane": lane_id or "all_enabled",
        "ensured_count": len(ensured),
        "started_count": len(started),
        "restarted_count": len(restarted),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "ensured": ensured,
        "started": started,
        "restarted": restarted,
        "skipped": skipped,
        "failed": failed,
        "ok": len(failed) == 0,
    }


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
        stopped.append(stop_lane_background(config, lane["id"], reason="manual"))
    return {
        "timestamp": _now_iso(),
        "requested_lane": lane_id or "all",
        "stopped_count": len(stopped),
        "stopped": stopped,
    }


def _conversation_content_from_lane_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type", "")).strip()
    if not event_type:
        return ""
    payload = event.get("payload")
    if isinstance(payload, dict) and payload:
        parts = [f"{key}={payload[key]}" for key in sorted(payload)]
        return f"{event_type}: {', '.join(parts)}"
    if payload not in (None, "", {}):
        return f"{event_type}: {payload}"
    return event_type


def _normalize_conversation_event(
    item: dict[str, Any],
    *,
    source_path: Path,
    source_kind: str,
    lane_id: str = "",
    owner: str = "",
) -> dict[str, Any]:
    event = dict(item)
    event["source"] = str(source_path)
    event["source_kind"] = source_kind

    normalized_lane = str(event.get("lane_id", "")).strip()
    if lane_id and not normalized_lane:
        event["lane_id"] = lane_id

    normalized_owner = str(event.get("owner", "")).strip()
    if owner and not normalized_owner:
        event["owner"] = owner
    if not str(event.get("owner", "")).strip():
        event["owner"] = "unknown"

    if source_kind == "lane_events" and not str(event.get("content", "")).strip():
        event["content"] = _conversation_content_from_lane_event(event)

    return event


def conversations_snapshot(config: ManagerConfig, lines: int = 200, include_lanes: bool = True) -> dict[str, Any]:
    line_limit = max(1, min(2000, int(lines)))
    source_specs: list[dict[str, str]] = [
        {
            "path": str(config.conversation_log_file),
            "kind": "primary",
            "lane_id": "",
            "owner": "",
            "fallback_path": "",
        }
    ]
    errors: list[str] = []
    if include_lanes:
        lanes, lane_errors = _load_lane_specs_resilient(config)
        errors.extend(f"lane_specs: {err}" for err in lane_errors)
        for lane in lanes:
            lane_file = Path(lane["conversation_log_file"])
            lane_path = str(lane_file)
            exists = any(item["path"] == lane_path for item in source_specs)
            if not exists:
                lane_id = str(lane["id"])
                source_specs.append(
                    {
                        "path": lane_path,
                        "kind": "lane",
                        "lane_id": lane_id,
                        "owner": str(lane["owner"]),
                        "fallback_path": str(_lane_events_file(config, lane_id)),
                    }
                )

    events: list[dict[str, Any]] = []
    source_reports: list[dict[str, Any]] = []
    for source in source_specs:
        source_path = Path(source["path"])
        source_events: list[dict[str, Any]] = []
        source_error = ""
        source_ok = True
        source_kind = source["kind"]
        resolved_path = source_path
        fallback_used = False
        missing = False
        try:
            if source_path.exists():
                source_events = _tail_ndjson(source_path, line_limit)
            else:
                missing = True
                fallback_path_raw = str(source.get("fallback_path", "")).strip()
                fallback_path = Path(fallback_path_raw) if fallback_path_raw else None
                if fallback_path and fallback_path.exists():
                    source_events = _tail_ndjson(fallback_path, line_limit)
                    resolved_path = fallback_path
                    source_kind = "lane_events"
                    fallback_used = True
                elif source["kind"] == "lane":
                    source_ok = False
                    source_error = f"missing lane conversation and event sources at {source_path}"
                    lane_label = source.get("lane_id", "").strip() or source["path"]
                    errors.append(f"{lane_label}: {source_error}")
            source_lane_id = str(source.get("lane_id", "")).strip()
            source_owner = str(source.get("owner", "")).strip()
            for item in source_events:
                if not isinstance(item, dict):
                    continue
                events.append(
                    _normalize_conversation_event(
                        item,
                        source_path=resolved_path,
                        source_kind=source_kind,
                        lane_id=source_lane_id,
                        owner=source_owner,
                    )
                )
        except Exception as err:
            source_ok = False
            source_error = str(err)
            errors.append(f"{source['path']}: {err}")
        source_reports.append(
            {
                "path": source["path"],
                "resolved_path": str(resolved_path),
                "kind": source["kind"],
                "resolved_kind": source_kind,
                "lane_id": source["lane_id"],
                "owner": source["owner"],
                "ok": source_ok,
                "missing": missing,
                "fallback_used": fallback_used,
                "error": source_error,
                "event_count": len(source_events),
            }
        )
    events = sorted(events, key=lambda item: str(item.get("timestamp", "")))[-line_limit:]

    owners: dict[str, int] = {}
    for event in events:
        owner = str(event.get("owner", "unknown")).strip() or "unknown"
        owners[owner] = owners.get(owner, 0) + 1

    partial = bool(errors) or any(not item["ok"] for item in source_reports)
    return {
        "timestamp": _now_iso(),
        "conversation_files": [item["path"] for item in source_specs],
        "total_events": len(events),
        "events": events,
        "owner_counts": owners,
        "sources": source_reports,
        "partial": partial,
        "ok": not partial,
        "errors": errors,
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
