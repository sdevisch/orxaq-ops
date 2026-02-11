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


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        return int(getuid())
    return int(os.environ.get("UID", "0") or "0")


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
    if shutil.which("codex") is None:
        raise RuntimeError("codex CLI not found in PATH")
    if shutil.which("gemini") is None:
        raise RuntimeError("gemini CLI not found in PATH")
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


def tail_logs(config: ManagerConfig, lines: int = 40) -> str:
    if not config.log_file.exists():
        return ""
    content = config.log_file.read_text(encoding="utf-8").splitlines()
    return "\n".join(content[-lines:])
