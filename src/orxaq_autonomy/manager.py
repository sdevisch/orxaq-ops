"""Cross-platform autonomy supervisor and lifecycle manager."""

from __future__ import annotations

import datetime as dt
import hashlib
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
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from urllib.parse import urlparse

from .ide import generate_workspace, open_in_ide
from .provider_costs import (
    CANONICAL_SOURCE_AUTHORITATIVE,
    CANONICAL_SOURCE_ESTIMATED,
    aggregate_canonical_records,
    load_canonical_records,
)

PROCESS_WATCHDOG_ORDER = ("supervisor", "runner")
PROCESS_WATCHDOG_HEALTHY_STATUSES = {"healthy", "restarted"}
RUNNER_IDLE_HEARTBEAT_PHASES = {"completed", "idle_all_done", "task_completed", "task_queue_wait"}
EXTRA_HIGH_MIN_MAX_CYCLES = 1_000_000_000


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _parse_iso_timestamp(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _format_local_timestamp(value: Any) -> str:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        return str(value or "").strip()
    local = parsed.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def _log(msg: str) -> None:
    print(f"[{_now_iso()}] {msg}", flush=True)


def _normalize_execution_profile(raw: Any, *, default: str = "high") -> str:
    text = str(raw or "").strip().lower()
    if text in {"extra-high", "extrahigh", "xhigh"}:
        return "extra_high"
    if text in {"standard", "high", "extra_high"}:
        return text
    fallback = str(default or "high").strip().lower()
    if fallback in {"extra-high", "extrahigh", "xhigh"}:
        return "extra_high"
    if fallback in {"standard", "high", "extra_high"}:
        return fallback
    return "high"


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


def _acquire_process_lock(path: Path) -> Any | None:
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
    if os.name != "nt":
        try:
            proc = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                timeout=1,
            )
            stat = (proc.stdout or "").strip()
            # On Unix/macOS, exited-but-not-reaped processes can show up as zombies (STAT includes 'Z').
            # Treat zombies as not-running so supervisors can restart workers instead of wedging.
            if proc.returncode == 0 and "Z" in stat:
                return False
        except Exception:
            pass
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
        if path.exists() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    if path.parent != Path("."):
        candidate = (Path.cwd() / path).resolve()
        if candidate.exists() and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    resolved = shutil.which(raw)
    if resolved:
        return resolved
    for fallback in (
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ):
        candidate = Path(fallback) / path.name
        if candidate.exists() and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
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


def _runtime_env(base: dict[str, str] | None = None, *, root_dir: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if base:
        env.update(base)
    path_entries = [part for part in str(env.get("PATH", "")).split(os.pathsep) if part.strip()]
    preferred_path_entries = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    merged_path: list[str] = []
    for part in [*preferred_path_entries, *path_entries]:
        token = part.strip()
        if token and token not in merged_path:
            merged_path.append(token)
    if merged_path:
        env["PATH"] = os.pathsep.join(merged_path)
    if root_dir is not None:
        src_dir = (Path(root_dir).resolve() / "src")
        if src_dir.exists():
            py_entries = [part for part in str(env.get("PYTHONPATH", "")).split(os.pathsep) if part.strip()]
            merged_py: list[str] = [str(src_dir)]
            for part in py_entries:
                if part not in merged_py:
                    merged_py.append(part)
            env["PYTHONPATH"] = os.pathsep.join(merged_py)
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
    supervisor_lock_file: Path
    conversation_log_file: Path
    metrics_file: Path
    metrics_summary_file: Path
    provider_cost_records_file: Path
    provider_cost_summary_file: Path
    provider_cost_stale_sec: int
    pricing_file: Path
    routellm_policy_file: Path
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
    gemini_fallback_models: list[str]
    claude_model: str | None
    routellm_enabled: bool
    routellm_url: str
    routellm_timeout_sec: int
    execution_profile: str
    scaling_enabled: bool
    scaling_decision_file: Path
    scaling_min_marginal_npv_usd: float
    scaling_daily_budget_usd: float
    scaling_max_parallel_agents: int
    scaling_max_subagents_per_agent: int
    swarm_daily_budget_usd: float
    swarm_budget_warning_ratio: float
    swarm_budget_enforce_hard_stop: bool
    parallel_capacity_state_file: Path
    parallel_capacity_log_file: Path
    parallel_capacity_default_limit: int
    parallel_capacity_recovery_cycles: int
    parallel_capacity_max_limit: int
    auto_push_guard: bool
    auto_push_interval_sec: int

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

        def _bool(key: str, default: bool) -> bool:
            raw = merged.get(key)
            if raw is None:
                return default
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

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
        resolved_codex_cmd = _resolve_binary(codex_cmd)
        if resolved_codex_cmd:
            codex_cmd = resolved_codex_cmd
        resolved_gemini_cmd = _resolve_binary(gemini_cmd)
        if resolved_gemini_cmd:
            gemini_cmd = resolved_gemini_cmd
        resolved_claude_cmd = _resolve_binary(claude_cmd)
        if resolved_claude_cmd:
            claude_cmd = resolved_claude_cmd
        codex_model = merged.get("ORXAQ_AUTONOMY_CODEX_MODEL", "").strip() or None
        gemini_model = merged.get("ORXAQ_AUTONOMY_GEMINI_MODEL", "").strip() or None
        gemini_fallback_models_raw = merged.get(
            "ORXAQ_AUTONOMY_GEMINI_FALLBACK_MODELS",
            "gemini-2.5-flash,gemini-2.0-flash",
        )
        gemini_fallback_models = [
            item.strip()
            for item in re.split(r"[;,]", gemini_fallback_models_raw)
            if item.strip()
        ]
        claude_model = merged.get("ORXAQ_AUTONOMY_CLAUDE_MODEL", "").strip() or None
        routellm_enabled = _bool("ORXAQ_AUTONOMY_ROUTELLM_ENABLED", False)
        routellm_url = merged.get("ORXAQ_AUTONOMY_ROUTELLM_URL", "").strip()
        routellm_timeout_sec = max(1, _int("ORXAQ_AUTONOMY_ROUTELLM_TIMEOUT_SEC", 5))
        execution_profile = _normalize_execution_profile(merged.get("ORXAQ_AUTONOMY_EXECUTION_PROFILE", "high"))
        default_scale_enabled = _bool("ORXAQ_ROUTELLM_SCALE_ENABLED", False)
        default_scale_min_npv = _float(
            "ORXAQ_ROUTELLM_SCALE_MIN_NPV_USD",
            _float("ORXAQ_ROUTELLM_MIN_NPV_USD", 0.0),
        )
        default_scale_daily_budget = _float("ORXAQ_ROUTELLM_DAILY_BUDGET_USD", 0.0)
        default_scale_max_parallel = max(1, _int("ORXAQ_ROUTELLM_MAX_PARALLEL_AGENTS", 1))
        default_scale_max_subagents = max(1, _int("ORXAQ_ROUTELLM_MAX_SUBAGENTS_PER_AGENT", 1))
        scaling_enabled = _bool("ORXAQ_AUTONOMY_SCALING_ENABLED", default_scale_enabled)
        scaling_decision_file = _path(
            "ORXAQ_AUTONOMY_SCALING_DECISION_FILE",
            artifacts / "scaling_decision.json",
        )
        scaling_min_marginal_npv_usd = max(
            0.0,
            _float("ORXAQ_AUTONOMY_SCALING_MIN_NPV_USD", default_scale_min_npv),
        )
        scaling_daily_budget_usd = max(
            0.0,
            _float("ORXAQ_AUTONOMY_SCALING_DAILY_BUDGET_USD", default_scale_daily_budget),
        )
        scaling_max_parallel_agents = max(
            1,
            _int("ORXAQ_AUTONOMY_SCALING_MAX_PARALLEL_AGENTS", default_scale_max_parallel),
        )
        scaling_max_subagents_per_agent = max(
            1,
            _int("ORXAQ_AUTONOMY_SCALING_MAX_SUBAGENTS_PER_AGENT", default_scale_max_subagents),
        )
        swarm_daily_budget_usd = max(
            0.0,
            _float(
                "ORXAQ_AUTONOMY_SWARM_DAILY_BUDGET_USD",
                _float("ORXAQ_SWARM_DAILY_BUDGET_USD", 100.0),
            ),
        )
        swarm_budget_warning_ratio = _float(
            "ORXAQ_AUTONOMY_SWARM_BUDGET_WARNING_RATIO",
            _float("ORXAQ_SWARM_BUDGET_WARNING_RATIO", 0.8),
        )
        if not (0.0 < swarm_budget_warning_ratio < 1.0):
            swarm_budget_warning_ratio = 0.8
        swarm_budget_enforce_hard_stop = _bool(
            "ORXAQ_AUTONOMY_SWARM_BUDGET_ENFORCE_HARD_STOP",
            _bool("ORXAQ_SWARM_BUDGET_ENFORCE_HARD_STOP", True),
        )
        parallel_capacity_state_file = _path(
            "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_STATE_FILE",
            artifacts / "parallel_capacity_state.json",
        )
        parallel_capacity_log_file = _path(
            "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_LOG_FILE",
            artifacts / "parallel_capacity.ndjson",
        )
        parallel_capacity_default_limit = max(
            1,
            _int("ORXAQ_AUTONOMY_PARALLEL_CAPACITY_DEFAULT_LIMIT", 2),
        )
        parallel_capacity_recovery_cycles = max(
            1,
            _int("ORXAQ_AUTONOMY_PARALLEL_CAPACITY_RECOVERY_CYCLES", 3),
        )
        parallel_capacity_max_limit = max(
            1,
            _int("ORXAQ_AUTONOMY_PARALLEL_CAPACITY_MAX_LIMIT", 24),
        )

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
            supervisor_lock_file=_path("ORXAQ_AUTONOMY_SUPERVISOR_LOCK_FILE", artifacts / "supervisor.lock"),
            conversation_log_file=_path(
                "ORXAQ_AUTONOMY_CONVERSATION_LOG_FILE",
                artifacts / "conversations.ndjson",
            ),
            metrics_file=_path("ORXAQ_AUTONOMY_METRICS_FILE", artifacts / "response_metrics.ndjson"),
            metrics_summary_file=_path(
                "ORXAQ_AUTONOMY_METRICS_SUMMARY_FILE",
                artifacts / "response_metrics_summary.json",
            ),
            provider_cost_records_file=_path(
                "ORXAQ_AUTONOMY_PROVIDER_COST_RECORDS_FILE",
                artifacts / "provider_costs" / "records.ndjson",
            ),
            provider_cost_summary_file=_path(
                "ORXAQ_AUTONOMY_PROVIDER_COST_SUMMARY_FILE",
                artifacts / "provider_costs" / "summary.json",
            ),
            provider_cost_stale_sec=max(1, _int("ORXAQ_AUTONOMY_PROVIDER_COST_STALE_SEC", 900)),
            pricing_file=_path("ORXAQ_AUTONOMY_PRICING_FILE", root / "config" / "pricing.json"),
            routellm_policy_file=_path("ORXAQ_AUTONOMY_ROUTELLM_POLICY_FILE", root / "config" / "routellm_policy.json"),
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
            gemini_fallback_models=gemini_fallback_models,
            claude_model=claude_model,
            routellm_enabled=routellm_enabled,
            routellm_url=routellm_url,
            routellm_timeout_sec=routellm_timeout_sec,
            execution_profile=execution_profile,
            scaling_enabled=scaling_enabled,
            scaling_decision_file=scaling_decision_file,
            scaling_min_marginal_npv_usd=scaling_min_marginal_npv_usd,
            scaling_daily_budget_usd=scaling_daily_budget_usd,
            scaling_max_parallel_agents=scaling_max_parallel_agents,
            scaling_max_subagents_per_agent=scaling_max_subagents_per_agent,
            swarm_daily_budget_usd=swarm_daily_budget_usd,
            swarm_budget_warning_ratio=swarm_budget_warning_ratio,
            swarm_budget_enforce_hard_stop=swarm_budget_enforce_hard_stop,
            parallel_capacity_state_file=parallel_capacity_state_file,
            parallel_capacity_log_file=parallel_capacity_log_file,
            parallel_capacity_default_limit=parallel_capacity_default_limit,
            parallel_capacity_recovery_cycles=parallel_capacity_recovery_cycles,
            parallel_capacity_max_limit=parallel_capacity_max_limit,
            auto_push_guard=_bool("ORXAQ_AUTONOMY_AUTO_PUSH_GUARD", True),
            auto_push_interval_sec=_int("ORXAQ_AUTONOMY_AUTO_PUSH_INTERVAL_SEC", 180),
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


def _slug_token(value: str, *, fallback: str, max_len: int) -> str:
    token = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value).strip().lower()).strip("-._")
    if not token:
        token = fallback
    return token[:max_len]


def _first_nonempty_line(text: str) -> str:
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line:
            return line
    return ""


def _git_output(repo: Path, args: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    text = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, text


def _lane_worktree_branch_name(lane_id: str, role: str) -> str:
    lane_token = _slug_token(lane_id, fallback="lane", max_len=40)
    role_token = _slug_token(role, fallback="repo", max_len=20)
    return f"codex/lane-{lane_token}-{role_token}"


def _lane_worktree_recovery_branch_name(lane_id: str, role: str) -> str:
    return f"{_lane_worktree_branch_name(lane_id, role)}-recovery"


def _lane_worktree_recovery_target(worktree_root: Path, role: str) -> Path:
    return (worktree_root / f"{role}-recovery").resolve()


def _has_checkout_overwrite_conflict(output: str) -> bool:
    lowered = str(output or "").lower()
    if "would be overwritten by checkout" in lowered and "local changes" in lowered:
        return True
    return "please commit your changes or stash them before you switch branches" in lowered and "aborting" in lowered


def _resolve_worktree_base_ref(repo: Path, preferred: str | None = None) -> str:
    ordered: list[str] = []
    preferred_ref = str(preferred or "").strip()
    if preferred_ref:
        ordered.append(preferred_ref)
    for candidate in ("origin/main", "origin/master", "main", "master", "HEAD"):
        if candidate not in ordered:
            ordered.append(candidate)
    for candidate in ordered:
        ok, _ = _git_output(repo, ["rev-parse", "--verify", candidate])
        if ok:
            return candidate
    return "HEAD"


def _prepare_lane_worktree_checkout(
    *,
    repo: Path,
    lane_id: str,
    role: str,
    worktree_root: Path,
    base_ref: str | None = None,
) -> tuple[Path, str]:
    ok, message = _repo_basic_check(repo)
    if not ok:
        raise RuntimeError(message)

    top_repo = Path(repo).resolve()
    ok_common, common_out = _git_output(top_repo, ["rev-parse", "--git-common-dir"])
    if not ok_common:
        raise RuntimeError(f"unable to resolve git common dir for {repo}: {common_out}")
    resolved_base_ref = _resolve_worktree_base_ref(top_repo, preferred=base_ref)
    branch = _lane_worktree_branch_name(lane_id, role)
    target = (worktree_root / role).resolve()

    if target.exists():
        ok_existing, existing_message = _repo_basic_check(target)
        if not ok_existing:
            raise RuntimeError(f"lane worktree path is invalid: {target} ({existing_message})")
        ok_branch, branch_out = _git_output(target, ["rev-parse", "--abbrev-ref", "HEAD"])
        if not ok_branch:
            raise RuntimeError(f"unable to read branch in lane worktree {target}: {branch_out}")
        current_branch = _first_nonempty_line(branch_out)
        if current_branch != branch:
            ok_branch_exists, _ = _git_output(top_repo, ["show-ref", "--verify", f"refs/heads/{branch}"])
            switch_cmd = ["git", "-C", str(target), "checkout", branch]
            if not ok_branch_exists:
                switch_cmd = ["git", "-C", str(target), "checkout", "-b", branch, resolved_base_ref]
            switch = subprocess.run(
                switch_cmd,
                capture_output=True,
                text=True,
            )
            if switch.returncode != 0:
                switch_out = (switch.stdout + "\n" + switch.stderr).strip()
                if _has_checkout_overwrite_conflict(switch_out):
                    recovery_target = _lane_worktree_recovery_target(worktree_root, role)
                    recovery_branch = _lane_worktree_recovery_branch_name(lane_id, role)
                    recovery_base_ref = branch if ok_branch_exists else resolved_base_ref
                    if recovery_target.exists():
                        ok_recovery_existing, recovery_existing_message = _repo_basic_check(recovery_target)
                        if not ok_recovery_existing:
                            raise RuntimeError(
                                f"lane recovery worktree path is invalid: {recovery_target} ({recovery_existing_message})"
                            )
                        ok_recovery_branch, recovery_branch_out = _git_output(
                            recovery_target, ["rev-parse", "--abbrev-ref", "HEAD"]
                        )
                        if not ok_recovery_branch:
                            raise RuntimeError(
                                f"unable to read recovery lane worktree branch in {recovery_target}: {recovery_branch_out}"
                            )
                        current_recovery_branch = _first_nonempty_line(recovery_branch_out)
                        if current_recovery_branch != recovery_branch:
                            ok_recovery_branch_exists, _ = _git_output(
                                top_repo, ["show-ref", "--verify", f"refs/heads/{recovery_branch}"]
                            )
                            recovery_switch_cmd = ["git", "-C", str(recovery_target), "checkout", recovery_branch]
                            if not ok_recovery_branch_exists:
                                recovery_switch_cmd = [
                                    "git",
                                    "-C",
                                    str(recovery_target),
                                    "checkout",
                                    "-b",
                                    recovery_branch,
                                    recovery_base_ref,
                                ]
                            recovery_switch = subprocess.run(
                                recovery_switch_cmd,
                                capture_output=True,
                                text=True,
                            )
                            if recovery_switch.returncode != 0:
                                recovery_switch_out = (recovery_switch.stdout + "\n" + recovery_switch.stderr).strip()
                                raise RuntimeError(
                                    "unable to switch lane recovery worktree branch "
                                    f"in {recovery_target} to {recovery_branch}: {recovery_switch_out}"
                                )
                        return recovery_target, recovery_branch
                    recovery_target.parent.mkdir(parents=True, exist_ok=True)
                    ok_recovery_branch_exists, _ = _git_output(
                        top_repo, ["show-ref", "--verify", f"refs/heads/{recovery_branch}"]
                    )
                    recovery_add_cmd = [
                        "git",
                        "-C",
                        str(top_repo),
                        "worktree",
                        "add",
                        "--force",
                        "--checkout",
                    ]
                    if ok_recovery_branch_exists:
                        recovery_add_cmd.extend([str(recovery_target), recovery_branch])
                    else:
                        recovery_add_cmd.extend(["-b", recovery_branch, str(recovery_target), recovery_base_ref])
                    recovery_add = subprocess.run(
                        recovery_add_cmd,
                        capture_output=True,
                        text=True,
                    )
                    if recovery_add.returncode == 0:
                        return recovery_target, recovery_branch
                    recovery_out = (recovery_add.stdout + "\n" + recovery_add.stderr).strip()
                    raise RuntimeError(
                        "unable to recover lane worktree after checkout conflict "
                        f"(target={target}, recovery_target={recovery_target}, branch={recovery_branch}): "
                        f"{recovery_out}"
                    )
                raise RuntimeError(
                    f"unable to switch lane worktree branch in {target} to {branch}: {switch_out}"
                )
        return target, branch

    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(top_repo), "worktree", "prune"],
        check=False,
        capture_output=True,
        text=True,
    )
    ok_branch_exists, _ = _git_output(top_repo, ["show-ref", "--verify", f"refs/heads/{branch}"])
    add_cmd = [
        "git",
        "-C",
        str(top_repo),
        "worktree",
        "add",
        "--force",
        "--checkout",
    ]
    if ok_branch_exists:
        add_cmd.extend([str(target), branch])
    else:
        add_cmd.extend(["-b", branch, str(target), resolved_base_ref])
    add = subprocess.run(
        add_cmd,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        add_out = (add.stdout + "\n" + add.stderr).strip()
        raise RuntimeError(
            f"unable to create lane worktree for lane={lane_id} role={role} at {target}: {add_out}"
        )
    return target, branch


def _prepare_lane_runtime_repos(config: ManagerConfig, lane: dict[str, Any]) -> dict[str, Any]:
    if not bool(lane.get("isolated_worktree", True)):
        return {
            "runtime_impl_repo": Path(lane["impl_repo"]).resolve(),
            "runtime_test_repo": Path(lane["test_repo"]).resolve(),
            "worktree_branches": {},
        }

    worktree_root = Path(lane.get("worktree_root", lane["runtime_dir"] / "worktrees")).resolve()
    impl_repo = Path(lane["impl_repo"]).resolve()
    test_repo = Path(lane["test_repo"]).resolve()
    branches: dict[str, str] = {}

    same_repo = False
    try:
        same_repo = os.path.samefile(impl_repo, test_repo)
    except OSError:
        same_repo = str(impl_repo) == str(test_repo)

    if same_repo:
        shared_repo, shared_branch = _prepare_lane_worktree_checkout(
            repo=impl_repo,
            lane_id=str(lane["id"]),
            role="shared",
            worktree_root=worktree_root,
            base_ref=str(lane.get("worktree_base_ref", "")).strip(),
        )
        branches["shared"] = shared_branch
        return {
            "runtime_impl_repo": shared_repo,
            "runtime_test_repo": shared_repo,
            "worktree_branches": branches,
        }

    impl_checkout, impl_branch = _prepare_lane_worktree_checkout(
        repo=impl_repo,
        lane_id=str(lane["id"]),
        role="impl",
        worktree_root=worktree_root,
        base_ref=str(lane.get("worktree_base_ref", "")).strip(),
    )
    test_checkout, test_branch = _prepare_lane_worktree_checkout(
        repo=test_repo,
        lane_id=str(lane["id"]),
        role="test",
        worktree_root=worktree_root,
        base_ref=str(lane.get("worktree_base_ref", "")).strip(),
    )
    branches["impl"] = impl_branch
    branches["test"] = test_branch
    return {
        "runtime_impl_repo": impl_checkout,
        "runtime_test_repo": test_checkout,
        "worktree_branches": branches,
    }


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
    task_queue_file = (config.artifacts_dir / "task_queue.ndjson").resolve()
    task_queue_state_file = (config.artifacts_dir / "task_queue_claimed.json").resolve()
    args: list[str] = [
        "--impl-repo",
        str(config.impl_repo),
        "--test-repo",
        str(config.test_repo),
        "--tasks-file",
        str(config.tasks_file),
        "--state-file",
        str(config.state_file),
        "--task-queue-file",
        str(task_queue_file),
        "--task-queue-state-file",
        str(task_queue_state_file),
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
            "--routellm-policy-file",
            str(config.routellm_policy_file),
            "--routellm-timeout-sec",
            str(config.routellm_timeout_sec),
        ]
    )
    args.append("--routellm-enabled" if config.routellm_enabled else "--no-routellm-enabled")
    if config.routellm_url.strip():
        args.extend(["--routellm-url", config.routellm_url.strip()])
    if config.codex_model:
        args.extend(["--codex-model", config.codex_model])
    if config.gemini_model:
        args.extend(["--gemini-model", config.gemini_model])
    for model in config.gemini_fallback_models:
        args.extend(["--gemini-fallback-model", model])
    if config.claude_model:
        args.extend(["--claude-model", config.claude_model])
    args.extend(
        [
            "--auto-push-interval-sec",
            str(config.auto_push_interval_sec),
            ("--auto-push-guard" if config.auto_push_guard else "--no-auto-push-guard"),
        ]
    )
    if config.codex_startup_prompt_file is not None:
        args.extend(["--codex-startup-prompt-file", str(config.codex_startup_prompt_file)])
    if config.gemini_startup_prompt_file is not None:
        args.extend(["--gemini-startup-prompt-file", str(config.gemini_startup_prompt_file)])
    if config.claude_startup_prompt_file is not None:
        args.extend(["--claude-startup-prompt-file", str(config.claude_startup_prompt_file)])
    args.extend(["--execution-profile", config.execution_profile])
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
    lock_handle = _acquire_process_lock(config.supervisor_lock_file)
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
                    env=_runtime_env(_load_env_file(config.env_file), root_dir=config.root_dir),
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


def start_background(config: ManagerConfig) -> int | None:
    existing_pid = _read_pid(config.supervisor_pid_file)
    if _pid_running(existing_pid):
        _log(f"autonomy supervisor already running (pid={existing_pid})")
        _emit_mesh_event(
            config,
            topic="monitoring",
            event_type="supervisor.already_running",
            payload={"pid": existing_pid},
        )
        return existing_pid
    ensure_runtime(config)
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_handle = config.log_file.open("a", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "cwd": str(config.root_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
        "env": _runtime_env(_load_env_file(config.env_file), root_dir=config.root_dir),
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
    _log(f"autonomy supervisor started (pid={proc.pid})")
    _emit_mesh_event(
        config,
        topic="monitoring",
        event_type="supervisor.started",
        payload={"pid": proc.pid},
    )
    _dispatch_mesh_events(config, max_events=32)
    return int(proc.pid)


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
    _emit_mesh_event(
        config,
        topic="monitoring",
        event_type="supervisor.stopped",
        payload={"supervisor_pid": supervisor_pid, "runner_pid": runner_pid},
    )
    _dispatch_mesh_events(config, max_events=32)


def ensure_background(config: ManagerConfig) -> None:
    _emit_mesh_event(
        config,
        topic="monitoring",
        event_type="supervisor.ensure_invoked",
        payload={},
    )
    supervisor_pid = _read_pid(config.supervisor_pid_file)
    if not _pid_running(supervisor_pid):
        _log("autonomy supervisor not running; starting")
        _emit_mesh_event(
            config,
            topic="monitoring",
            event_type="supervisor.missing",
            payload={"supervisor_pid": supervisor_pid},
        )
        start_background(config)
        return

    runner_pid = _read_pid(config.runner_pid_file)
    age = _heartbeat_age_sec(config)
    if runner_pid and _pid_running(runner_pid) and age != -1 and age > config.heartbeat_stale_sec:
        _log(f"runner heartbeat stale ({age}s); restarting runner pid={runner_pid}")
        _terminate_pid(runner_pid)
        _emit_mesh_event(
            config,
            topic="monitoring",
            event_type="runner.restarted_stale_heartbeat",
            payload={"runner_pid": runner_pid, "heartbeat_age_sec": age},
        )
    else:
        _log("autonomy supervisor ensured")
        _emit_mesh_event(
            config,
            topic="monitoring",
            event_type="supervisor.ensured",
            payload={"runner_pid": runner_pid, "heartbeat_age_sec": age},
        )

    if config.lanes_file.exists():
        lane_payload = ensure_lanes_background(config)
        parallel_groups = (
            lane_payload.get("parallel_capacity", {}).get("groups", {})
            if isinstance(lane_payload.get("parallel_capacity", {}), dict)
            else {}
        )
        at_limit = 0
        if isinstance(parallel_groups, dict):
            at_limit = sum(
                1
                for payload in parallel_groups.values()
                if isinstance(payload, dict)
                and _int_value(payload.get("running_count", 0), 0) >= _int_value(payload.get("effective_limit", 1), 1)
            )
        _log(
            "lane ensure: "
            f"ensured={lane_payload['ensured_count']} "
            f"started={lane_payload['started_count']} "
            f"restarted={lane_payload['restarted_count']} "
            f"scaled_up={lane_payload.get('scaled_up_count', 0)} "
            f"scaled_down={lane_payload.get('scaled_down_count', 0)} "
            f"parallel_groups_at_limit={at_limit} "
            f"failed={lane_payload['failed_count']}"
        )
        _emit_mesh_event(
            config,
            topic="scheduling",
            event_type="lanes.ensure.completed",
            payload={
                "requested_lane": lane_payload.get("requested_lane", "all_enabled"),
                "ensured_count": lane_payload.get("ensured_count", 0),
                "started_count": lane_payload.get("started_count", 0),
                "restarted_count": lane_payload.get("restarted_count", 0),
                "scaled_up_count": lane_payload.get("scaled_up_count", 0),
                "scaled_down_count": lane_payload.get("scaled_down_count", 0),
                "parallel_groups_at_limit": at_limit,
                "failed_count": lane_payload.get("failed_count", 0),
                "ok": bool(lane_payload.get("ok", False)),
            },
        )
        _dispatch_mesh_events(config, max_events=64)


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


def _append_ndjson_record(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def _manager_env(config: ManagerConfig) -> dict[str, str]:
    return {**_load_env_file(config.env_file), **os.environ}


def _path_from_env(env: dict[str, str], key: str, default: Path) -> Path:
    raw = str(env.get(key, "")).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return default.resolve()


def _int_from_env(env: dict[str, str], key: str, default: int, *, min_value: int = 0) -> int:
    value = _int_value(env.get(key, default), default)
    return max(min_value, value)


def _emit_mesh_event(
    config: ManagerConfig,
    *,
    topic: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    source: str = "manager",
) -> None:
    try:
        from .event_mesh import EventMeshConfig, publish_event

        publish_event(
            EventMeshConfig.from_root(config.root_dir),
            topic=topic,
            event_type=event_type,
            payload=payload if isinstance(payload, dict) else {},
            source=source,
        )
    except Exception:
        # Mesh publication is best-effort during migration to event-driven flows.
        return


def _dispatch_mesh_events(config: ManagerConfig, *, max_events: int = 128) -> None:
    try:
        from .event_mesh import EventMeshConfig, dispatch_events

        dispatch_events(EventMeshConfig.from_root(config.root_dir), max_events=max_events)
    except Exception:
        return


def _latest_mesh_scaling_decision(config: ManagerConfig, requested_lane: str = "") -> dict[str, Any]:
    try:
        from .event_mesh import EventMeshConfig, read_event_log

        events = read_event_log(EventMeshConfig.from_root(config.root_dir).events_file)
    except Exception:
        return {}
    lane_filter = (requested_lane or "all_enabled").strip().lower() or "all_enabled"
    now_utc = _now_utc()
    freshness_window_sec = 300
    seen_state_file = config.state_file.parent / "mesh_scaling_command_seen.json"
    seen_state = _read_json_file(seen_state_file)
    last_leader_epoch = _int_value(seen_state.get("last_leader_epoch", -1), -1) if isinstance(seen_state, dict) else -1
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        if str(event.get("topic", "")).strip().lower() != "scaling":
            continue
        if str(event.get("event_type", "")).strip().lower() != "decision.made":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        decision_lane = str(payload.get("requested_lane", "all_enabled")).strip().lower() or "all_enabled"
        if lane_filter not in {"all", "all_enabled"} and decision_lane not in {lane_filter, "all_enabled"}:
            continue
        parsed = _parse_iso_timestamp(event.get("timestamp"))
        if parsed is None:
            continue
        age_sec = int((now_utc - parsed).total_seconds())
        if age_sec < 0 or age_sec > freshness_window_sec:
            continue
        leader_epoch = _int_value(payload.get("leader_epoch", -1), -1)
        if leader_epoch >= 0 and leader_epoch < max(0, last_leader_epoch):
            continue
        out = dict(payload)
        out["event_id"] = str(event.get("event_id", "")).strip()
        out["timestamp"] = parsed.isoformat()
        out["age_sec"] = age_sec
        return out
    return {}


def _consume_mesh_scaling_commands(
    config: ManagerConfig,
    *,
    lane_map: dict[str, dict[str, Any]],
    status_by_id: dict[str, dict[str, Any]],
    requested_lane: str = "",
    max_commands: int = 4,
) -> dict[str, Any]:
    state_file = config.state_file.parent / "mesh_scaling_command_seen.json"
    seen_payload = _read_json_file(state_file)
    seen_ids_raw = seen_payload.get("event_ids", []) if isinstance(seen_payload, dict) else []
    seen_ids = {
        str(item).strip()
        for item in seen_ids_raw
        if str(item).strip()
    }
    last_leader_epoch = _int_value(seen_payload.get("last_leader_epoch", -1), -1)
    lane_filter = (requested_lane or "").strip().lower()
    now_utc = _now_utc()
    freshness_window_sec = 300
    processed = 0
    started = 0
    stopped = 0
    skipped: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    command_log_file = _path_from_env(
        _manager_env(config),
        "ORXAQ_AUTONOMY_MESH_COMMAND_LOG_FILE",
        config.artifacts_dir / "event_mesh" / "commands.ndjson",
    )
    dag_state_file = _path_from_env(
        _manager_env(config),
        "ORXAQ_AUTONOMY_MESH_DAG_STATE_FILE",
        config.state_file.parent / "mesh_dag_state.json",
    )
    dag_state_payload = _read_json_file(dag_state_file)
    dag_state = dag_state_payload if isinstance(dag_state_payload, dict) else {}
    env = _manager_env(config)
    enforce_leader_fence = _coerce_bool(env.get("ORXAQ_AUTONOMY_ENFORCE_MESH_LEADER_FENCE", "0"), False)
    lane_items_for_budget = [
        item for item in lane_map.values() if isinstance(item, dict)
    ]
    swarm_budget = _current_swarm_budget_status(config, lane_items_for_budget)
    swarm_budget_hard_stop = bool(swarm_budget.get("hard_stop", False))
    lease_snapshot: dict[str, Any] = {}
    if enforce_leader_fence:
        try:
            from .leader_lease import LeaderLeaseConfig, read_lease_snapshot

            lease_snapshot = read_lease_snapshot(LeaderLeaseConfig.from_root(config.root_dir))
        except Exception:
            lease_snapshot = {}

    def _append_command_record(
        *,
        event_id: str,
        action: str,
        lane_id: str,
        outcome: str,
        leader_epoch: int,
        command_id: str,
        reason: str = "",
        error: str = "",
        decision_table_version: str = "",
        execution_dag_id: str = "",
        causal_hypothesis_id: str = "",
        causal_gate: dict[str, Any] | None = None,
        dag_claim_key: str = "",
    ) -> None:
        record = {
            "timestamp": _now_iso(),
            "event_id": event_id,
            "command_id": command_id or event_id,
            "action": action,
            "lane_id": lane_id,
            "outcome": outcome,
            "reason": reason,
            "error": error,
            "leader_epoch": max(0, int(leader_epoch)),
            "decision_table_version": decision_table_version,
            "execution_dag_id": execution_dag_id,
            "causal_hypothesis_id": causal_hypothesis_id,
            "causal_gate": causal_gate or {},
            "dag_claim_key": dag_claim_key,
        }
        try:
            _append_ndjson_record(command_log_file, record)
        except Exception:
            return

    try:
        from .event_mesh import EventMeshConfig, read_event_log

        events = read_event_log(EventMeshConfig.from_root(config.root_dir).events_file)
    except Exception as err:
        return {
            "processed_count": 0,
            "started_count": 0,
            "stopped_count": 0,
            "skipped_count": 1,
            "skipped": [{"reason": "mesh_log_unavailable", "error": str(err)}],
            "actions": [],
            "ok": False,
        }

    for event in events:
        if processed >= max(1, int(max_commands)):
            break
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id", "")).strip()
        if not event_id or event_id in seen_ids:
            continue
        if str(event.get("topic", "")).strip().lower() != "scaling":
            continue
        if str(event.get("event_type", "")).strip().lower() != "command.requested":
            continue
        parsed_ts = _parse_iso_timestamp(event.get("timestamp"))
        if parsed_ts is None:
            skipped.append({"event_id": event_id, "reason": "invalid_timestamp"})
            _append_command_record(
                event_id=event_id,
                action="",
                lane_id="",
                outcome="rejected_invalid_timestamp",
                leader_epoch=max(0, last_leader_epoch),
                command_id=event_id,
                reason="invalid_timestamp",
            )
            seen_ids.add(event_id)
            continue
        age_sec = int((now_utc - parsed_ts).total_seconds())
        if age_sec < 0 or age_sec > freshness_window_sec:
            skipped.append({"event_id": event_id, "reason": "stale", "age_sec": age_sec})
            _append_command_record(
                event_id=event_id,
                action="",
                lane_id="",
                outcome="rejected_stale_timestamp",
                leader_epoch=max(0, last_leader_epoch),
                command_id=event_id,
                reason="stale_timestamp",
            )
            seen_ids.add(event_id)
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            skipped.append({"event_id": event_id, "reason": "invalid_payload"})
            _append_command_record(
                event_id=event_id,
                action="",
                lane_id="",
                outcome="rejected_invalid_payload",
                leader_epoch=max(0, last_leader_epoch),
                command_id=event_id,
                reason="invalid_payload",
            )
            seen_ids.add(event_id)
            continue
        command_id = str(payload.get("command_id", "")).strip() or event_id
        decision_table_version = str(payload.get("decision_table_version", "")).strip()
        execution_dag_id = str(payload.get("execution_dag_id", "")).strip()
        causal_hypothesis_id = str(payload.get("causal_hypothesis_id", "")).strip()
        causal_gate = payload.get("causal_gate", {})
        if not isinstance(causal_gate, dict):
            causal_gate = {}
        if not causal_gate:
            try:
                from .causal_decision_bridge import enforce_causal_metadata_gate

                causal_gate = enforce_causal_metadata_gate(
                    action=str(payload.get("action", "")).strip().lower(),
                    requested_lane=str(payload.get("requested_lane", "")).strip(),
                    causal_hypothesis_id=causal_hypothesis_id,
                )
            except Exception:
                causal_gate = {}
        raw_epoch = str(payload.get("leader_epoch", "")).strip()
        if raw_epoch:
            command_leader_epoch = max(0, _int_value(raw_epoch, 0))
            epoch_explicit = True
        else:
            command_leader_epoch = max(0, last_leader_epoch)
            epoch_explicit = False
        if epoch_explicit and command_leader_epoch < max(0, last_leader_epoch):
            skipped.append(
                {
                    "event_id": event_id,
                    "reason": "stale_epoch",
                    "leader_epoch": command_leader_epoch,
                    "last_leader_epoch": last_leader_epoch,
                }
            )
            _append_command_record(
                event_id=event_id,
                action=str(payload.get("action", "")).strip().lower(),
                lane_id=str(payload.get("target_lane", "")).strip(),
                outcome="rejected_stale_epoch",
                leader_epoch=command_leader_epoch,
                command_id=command_id,
                reason="stale_epoch",
                decision_table_version=decision_table_version,
                execution_dag_id=execution_dag_id,
                causal_hypothesis_id=causal_hypothesis_id,
            )
            seen_ids.add(event_id)
            processed += 1
            continue
        if command_leader_epoch > last_leader_epoch:
            last_leader_epoch = command_leader_epoch
        action = str(payload.get("action", "")).strip().lower()
        if enforce_leader_fence and action in {"scale_up", "scale_down"}:
            lease_ok = bool(lease_snapshot.get("ok", False))
            lease_epoch = max(0, _int_value(lease_snapshot.get("epoch", 0), 0))
            lease_leader_id = str(lease_snapshot.get("leader_id", "")).strip()
            lease_is_leader = bool(lease_snapshot.get("is_leader", False))
            lease_expired = bool(lease_snapshot.get("expired", True))
            command_leader_id = str(payload.get("leader_id", "")).strip()
            enforceable = lease_ok and bool(lease_leader_id) and lease_epoch > 0 and not lease_expired
            if enforceable and not lease_is_leader:
                skipped.append({"event_id": event_id, "reason": "leader_fence_local_follower"})
                _append_command_record(
                    event_id=event_id,
                    action=action,
                    lane_id=str(payload.get("target_lane", "")).strip(),
                    outcome="rejected_leader_fence",
                    leader_epoch=command_leader_epoch,
                    command_id=command_id,
                    reason="leader_fence_local_follower",
                    decision_table_version=decision_table_version,
                    execution_dag_id=execution_dag_id,
                    causal_hypothesis_id=causal_hypothesis_id,
                    causal_gate=causal_gate,
                )
                seen_ids.add(event_id)
                processed += 1
                continue
            if enforceable and command_leader_epoch != lease_epoch:
                skipped.append(
                    {
                        "event_id": event_id,
                        "reason": "leader_fence_epoch_mismatch",
                        "leader_epoch": command_leader_epoch,
                        "lease_epoch": lease_epoch,
                    }
                )
                _append_command_record(
                    event_id=event_id,
                    action=action,
                    lane_id=str(payload.get("target_lane", "")).strip(),
                    outcome="rejected_leader_fence",
                    leader_epoch=command_leader_epoch,
                    command_id=command_id,
                    reason="leader_fence_epoch_mismatch",
                    decision_table_version=decision_table_version,
                    execution_dag_id=execution_dag_id,
                    causal_hypothesis_id=causal_hypothesis_id,
                    causal_gate=causal_gate,
                )
                seen_ids.add(event_id)
                processed += 1
                continue
            if enforceable and command_leader_id and command_leader_id != lease_leader_id:
                skipped.append({"event_id": event_id, "reason": "leader_fence_mismatched_leader_id"})
                _append_command_record(
                    event_id=event_id,
                    action=action,
                    lane_id=str(payload.get("target_lane", "")).strip(),
                    outcome="rejected_leader_fence",
                    leader_epoch=command_leader_epoch,
                    command_id=command_id,
                    reason="leader_fence_mismatched_leader_id",
                    decision_table_version=decision_table_version,
                    execution_dag_id=execution_dag_id,
                    causal_hypothesis_id=causal_hypothesis_id,
                    causal_gate=causal_gate,
                )
                seen_ids.add(event_id)
                processed += 1
                continue
        if causal_gate and not bool(causal_gate.get("allowed", True)):
            skipped.append({"event_id": event_id, "reason": str(causal_gate.get("status", "causal_gate_rejected")).strip()})
            _append_command_record(
                event_id=event_id,
                action=action,
                lane_id=str(payload.get("target_lane", "")).strip(),
                outcome="rejected_causal_gate",
                leader_epoch=command_leader_epoch,
                command_id=command_id,
                reason=str(causal_gate.get("status", "causal_gate_rejected")).strip(),
                decision_table_version=decision_table_version,
                execution_dag_id=execution_dag_id,
                causal_hypothesis_id=causal_hypothesis_id,
                causal_gate=causal_gate,
            )
            seen_ids.add(event_id)
            processed += 1
            continue
        target_lane = str(payload.get("target_lane", "")).strip()
        requested = str(payload.get("requested_lane", "")).strip()
        if not target_lane and requested not in {"", "all", "all_enabled"}:
            target_lane = requested
        if not target_lane:
            scoped_ids = [
                lane_id
                for lane_id, lane_payload in lane_map.items()
                if isinstance(lane_payload, dict)
                and bool(lane_payload.get("enabled", False))
                and (not lane_filter or lane_id.lower() == lane_filter)
            ]
            if scoped_ids:
                def _rank_key(lane_id: str) -> tuple[int, str]:
                    lane_payload = lane_map.get(lane_id, {})
                    rank = _int_value(lane_payload.get("scaling_rank", 1), 1)
                    return (rank, lane_id)

                running_ids = [lane_id for lane_id in scoped_ids if bool(status_by_id.get(lane_id, {}).get("running", False))]
                stopped_ids = [lane_id for lane_id in scoped_ids if not bool(status_by_id.get(lane_id, {}).get("running", False))]
                if action == "scale_down" and running_ids:
                    # Scale down the least-preferred running lane first.
                    target_lane = sorted(running_ids, key=_rank_key, reverse=True)[0]
                elif action == "scale_up" and stopped_ids:
                    # Scale up the highest-preferred stopped lane first.
                    target_lane = sorted(stopped_ids, key=_rank_key)[0]
        if not target_lane:
            skipped.append({"event_id": event_id, "reason": "no_specific_target"})
            _append_command_record(
                event_id=event_id,
                action=action,
                lane_id="",
                outcome="rejected_no_target",
                leader_epoch=command_leader_epoch,
                command_id=command_id,
                reason="no_specific_target",
                decision_table_version=decision_table_version,
                execution_dag_id=execution_dag_id,
                causal_hypothesis_id=causal_hypothesis_id,
            )
            seen_ids.add(event_id)
            continue
        if lane_filter and target_lane.lower() != lane_filter:
            continue
        lane = lane_map.get(target_lane)
        if lane is None:
            skipped.append({"event_id": event_id, "reason": "unknown_lane", "lane_id": target_lane})
            _append_command_record(
                event_id=event_id,
                action=action,
                lane_id=target_lane,
                outcome="rejected_unknown_lane",
                leader_epoch=command_leader_epoch,
                command_id=command_id,
                reason="unknown_lane",
                decision_table_version=decision_table_version,
                execution_dag_id=execution_dag_id,
                causal_hypothesis_id=causal_hypothesis_id,
            )
            seen_ids.add(event_id)
            continue
        current_status = status_by_id.get(target_lane, {})
        running = bool(current_status.get("running", False))
        try:
            dag_claim_key = ""
            if execution_dag_id:
                try:
                    from .dag_scheduler import replay_safe_claim, transition_node_state

                    claim_result = replay_safe_claim(
                        dag_state=dag_state,
                        node_id=command_id,
                        task_id=command_id,
                        attempt=1,
                        leader_epoch=command_leader_epoch,
                    )
                    dag_claim_key = str(claim_result.get("claim_key", "")).strip()
                    transition_node_state(
                        dag_state=dag_state,
                        node_id=command_id,
                        next_state="running",
                        reason="command_requested",
                    )
                except Exception:
                    dag_claim_key = ""
            if action == "scale_down":
                if running:
                    stop_lane_background(
                        config,
                        target_lane,
                        reason="mesh_scale_down",
                        pause=False,
                    )
                    status_by_id[target_lane] = {**current_status, "running": False}
                    stopped += 1
                    actions.append(
                        {
                            "event_id": event_id,
                            "command_id": command_id,
                            "leader_epoch": command_leader_epoch,
                            "action": action,
                            "lane_id": target_lane,
                        }
                    )
                    _append_command_record(
                        event_id=event_id,
                        action=action,
                        lane_id=target_lane,
                        outcome="applied",
                        leader_epoch=command_leader_epoch,
                        command_id=command_id,
                        reason="stopped_lane",
                        decision_table_version=decision_table_version,
                        execution_dag_id=execution_dag_id,
                        causal_hypothesis_id=causal_hypothesis_id,
                        causal_gate=causal_gate,
                        dag_claim_key=dag_claim_key,
                    )
                    if execution_dag_id:
                        try:
                            from .dag_scheduler import transition_node_state

                            transition_node_state(
                                dag_state=dag_state,
                                node_id=command_id,
                                next_state="success",
                                reason="stopped_lane",
                            )
                        except Exception:
                            pass
                else:
                    skipped.append({"event_id": event_id, "reason": "already_stopped", "lane_id": target_lane})
                    _append_command_record(
                        event_id=event_id,
                        action=action,
                        lane_id=target_lane,
                        outcome="noop",
                        leader_epoch=command_leader_epoch,
                        command_id=command_id,
                        reason="already_stopped",
                        decision_table_version=decision_table_version,
                        execution_dag_id=execution_dag_id,
                        causal_hypothesis_id=causal_hypothesis_id,
                        causal_gate=causal_gate,
                        dag_claim_key=dag_claim_key,
                    )
            elif action == "scale_up":
                if (
                    not running
                    and bool(lane.get("enabled", False))
                    and swarm_budget_hard_stop
                ):
                    skipped.append(
                        {
                            "event_id": event_id,
                            "reason": "swarm_daily_budget_cap",
                            "lane_id": target_lane,
                            "daily_spend_usd": _float_value(swarm_budget.get("daily_spend_usd", 0.0), 0.0),
                            "daily_budget_usd": _float_value(swarm_budget.get("daily_budget_usd", 0.0), 0.0),
                        }
                    )
                    _append_command_record(
                        event_id=event_id,
                        action=action,
                        lane_id=target_lane,
                        outcome="rejected_budget_cap",
                        leader_epoch=command_leader_epoch,
                        command_id=command_id,
                        reason="swarm_daily_budget_cap",
                        decision_table_version=decision_table_version,
                        execution_dag_id=execution_dag_id,
                        causal_hypothesis_id=causal_hypothesis_id,
                        causal_gate=causal_gate,
                        dag_claim_key=dag_claim_key,
                    )
                elif not running and bool(lane.get("enabled", False)):
                    lane_payload = start_lane_background(config, target_lane)
                    status_by_id[target_lane] = {
                        **current_status,
                        "running": True,
                        "pid": lane_payload.get("pid", current_status.get("pid")),
                    }
                    started += 1
                    actions.append(
                        {
                            "event_id": event_id,
                            "command_id": command_id,
                            "leader_epoch": command_leader_epoch,
                            "action": action,
                            "lane_id": target_lane,
                        }
                    )
                    _append_command_record(
                        event_id=event_id,
                        action=action,
                        lane_id=target_lane,
                        outcome="applied",
                        leader_epoch=command_leader_epoch,
                        command_id=command_id,
                        reason="started_lane",
                        decision_table_version=decision_table_version,
                        execution_dag_id=execution_dag_id,
                        causal_hypothesis_id=causal_hypothesis_id,
                        causal_gate=causal_gate,
                        dag_claim_key=dag_claim_key,
                    )
                    if execution_dag_id:
                        try:
                            from .dag_scheduler import transition_node_state

                            transition_node_state(
                                dag_state=dag_state,
                                node_id=command_id,
                                next_state="success",
                                reason="started_lane",
                            )
                        except Exception:
                            pass
                else:
                    skipped.append({"event_id": event_id, "reason": "already_running_or_disabled", "lane_id": target_lane})
                    _append_command_record(
                        event_id=event_id,
                        action=action,
                        lane_id=target_lane,
                        outcome="noop",
                        leader_epoch=command_leader_epoch,
                        command_id=command_id,
                        reason="already_running_or_disabled",
                        decision_table_version=decision_table_version,
                        execution_dag_id=execution_dag_id,
                        causal_hypothesis_id=causal_hypothesis_id,
                        causal_gate=causal_gate,
                        dag_claim_key=dag_claim_key,
                    )
            else:
                skipped.append({"event_id": event_id, "reason": "unsupported_action", "action": action})
                _append_command_record(
                    event_id=event_id,
                    action=action,
                    lane_id=target_lane,
                    outcome="rejected_unsupported_action",
                    leader_epoch=command_leader_epoch,
                    command_id=command_id,
                    reason="unsupported_action",
                    decision_table_version=decision_table_version,
                    execution_dag_id=execution_dag_id,
                    causal_hypothesis_id=causal_hypothesis_id,
                    causal_gate=causal_gate,
                    dag_claim_key=dag_claim_key,
                )
            seen_ids.add(event_id)
            processed += 1
        except Exception as err:
            skipped.append({"event_id": event_id, "reason": "actuation_failed", "lane_id": target_lane, "error": str(err)})
            _append_command_record(
                event_id=event_id,
                action=action,
                lane_id=target_lane,
                outcome="actuation_failed",
                leader_epoch=command_leader_epoch,
                command_id=command_id,
                reason="actuation_failed",
                error=str(err),
                decision_table_version=decision_table_version,
                execution_dag_id=execution_dag_id,
                causal_hypothesis_id=causal_hypothesis_id,
                causal_gate=causal_gate,
                dag_claim_key=dag_claim_key,
            )
            if execution_dag_id:
                try:
                    from .dag_scheduler import transition_node_state

                    transition_node_state(
                        dag_state=dag_state,
                        node_id=command_id,
                        next_state="failed",
                        reason="actuation_failed",
                    )
                except Exception:
                    pass
            seen_ids.add(event_id)
            processed += 1

    _write_json_file(
        state_file,
        {
            "event_ids": sorted(seen_ids),
            "last_leader_epoch": max(-1, int(last_leader_epoch)),
            "updated_at": _now_iso(),
        },
    )
    _write_json_file(dag_state_file, dag_state)
    return {
        "processed_count": processed,
        "started_count": started,
        "stopped_count": stopped,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "actions": actions,
        "swarm_daily_budget": swarm_budget,
        "ok": True,
    }


def _local_model_fleet_snapshot(config: ManagerConfig) -> dict[str, Any]:
    status_file = (config.artifacts_dir / "local_models" / "fleet_status.json").resolve()
    payload = _read_json_file(status_file)
    if not payload:
        return {
            "ok": False,
            "status_file": str(status_file),
            "summary": {},
            "benchmark": {},
            "sync": {},
            "error": "fleet_status_missing",
        }

    probe = payload.get("probe", {}) if isinstance(payload.get("probe", {}), dict) else {}
    summary = probe.get("summary", {}) if isinstance(probe.get("summary", {}), dict) else {}
    benchmark = payload.get("benchmark", {}) if isinstance(payload.get("benchmark", {}), dict) else {}
    benchmark_summary = benchmark.get("summary", {}) if isinstance(benchmark.get("summary", {}), dict) else {}
    sync_payload = payload.get("sync", {}) if isinstance(payload.get("sync", {}), dict) else {}
    return {
        "ok": True,
        "status_file": str(status_file),
        "timestamp": str(payload.get("timestamp", "")).strip(),
        "summary": summary,
        "benchmark": benchmark_summary,
        "sync": sync_payload,
    }


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
        "task_queue_file",
        "task_queue_state_file",
        "objective_file",
        "impl_repo",
        "test_repo",
        "metrics_file",
        "metrics_summary_file",
        "pricing_file",
        "routellm_policy_file",
        "routellm_enabled",
        "routellm_url",
        "routellm_timeout_sec",
        "execution_profile",
        "codex_cmd",
        "gemini_cmd",
        "claude_cmd",
        "codex_model",
        "gemini_model",
        "claude_model",
        "scaling_mode",
        "scaling_group",
        "scaling_rank",
        "scaling_decision_file",
        "scaling_min_marginal_npv_usd",
        "scaling_daily_budget_usd",
        "scaling_max_parallel_agents",
        "scaling_max_subagents_per_agent",
        "max_cycles",
        "max_attempts",
        "continuous",
        "continuous_recycle_delay_sec",
        "isolated_worktree",
        "worktree_root",
        "worktree_base_ref",
    )
    for key in lane_keys:
        hasher.update(str(key).encode("utf-8"))
        hasher.update(str(lane.get(key, "")).encode("utf-8"))
    for item in lane.get("gemini_fallback_models", []):
        hasher.update(str(item).encode("utf-8"))
    for item in lane.get("validate_commands", []):
        hasher.update(str(item).encode("utf-8"))
    return hasher.hexdigest()[:12]


def _dashboard_http_version_snapshot(config: ManagerConfig, meta: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Best-effort probe of the dashboard's /api/version endpoint.

    The dashboard is sometimes started outside of the background manager, leaving stale pid/meta files behind.
    This probe lets us treat an already-live dashboard as running and reconcile pid/meta deterministically.
    """

    candidates: list[str] = []
    meta_url = str(meta.get("url", "")).strip()
    if meta_url:
        candidates.append(meta_url.rstrip("/"))

    host = str(meta.get("host", "")).strip() or "127.0.0.1"
    port = _int_value(meta.get("port", 0), 0)
    if port > 0:
        candidates.append(f"http://{host}:{int(port)}")

    if not candidates:
        return None, ""

    # Deterministic fallbacks to avoid flapping on older meta files.
    candidates.extend([f"http://{host}:8765", f"http://{host}:8876"])

    dedup: list[str] = []
    seen: set[str] = set()
    for base in candidates:
        token = base.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        dedup.append(token)

    for base in dedup:
        url = f"{base.rstrip('/')}/api/version"
        try:
            with urlopen(url, timeout=0.8) as response:  # noqa: S310
                status = _int_value(getattr(response, "status", 200), 200)
                body = response.read(64 * 1024)
        except HTTPError:
            continue
        except (URLError, TimeoutError, OSError):
            continue
        if not (200 <= status < 500):
            continue
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if not bool(payload.get("ok", False)):
            continue
        if str(payload.get("root_dir", "")).strip() != str(config.root_dir):
            continue
        return payload, base

    return None, ""


def dashboard_status_snapshot(config: ManagerConfig) -> dict[str, Any]:
    pid = _read_pid(config.dashboard_pid_file)
    running_pid = _pid_running(pid)
    meta = _read_json_file(config.dashboard_meta_file)
    url = str(meta.get("url", "")).strip()
    resolved_host = str(meta.get("host", "")).strip()
    resolved_port = int(meta.get("port", 0) or 0)
    resolved_refresh_sec = int(meta.get("refresh_sec", 0) or 0)
    build_id = str(meta.get("build_id", "")).strip()
    expected_build_id = _dashboard_build_id(config)

    # Best-effort: update URL from log banner if available.
    if config.dashboard_log_file.exists():
        for line in config.dashboard_log_file.read_text(encoding="utf-8").splitlines()[-20:]:
            if line.startswith("dashboard_url="):
                url = line.split("=", 1)[1].strip()
                break

    meta_for_http = dict(meta)
    if url:
        meta_for_http["url"] = url
    version_payload, version_base = _dashboard_http_version_snapshot(config, meta_for_http)
    running_http = version_payload is not None
    if version_payload:
        http_pid = _int_value(version_payload.get("pid", 0), 0)
        if http_pid > 0:
            pid = http_pid
            # Reconcile stale PID file so background stop/restart is safe and deterministic.
            if _read_pid(config.dashboard_pid_file) != http_pid:
                _write_pid(config.dashboard_pid_file, http_pid)

        parsed = urlparse(version_base) if version_base else None
        host = (parsed.hostname if parsed else "") or resolved_host or "127.0.0.1"
        bound_port = _int_value(version_payload.get("bound_port", 0), 0)
        port = bound_port if bound_port > 0 else (resolved_port or 0)
        refresh_sec = _int_value(version_payload.get("refresh_sec", resolved_refresh_sec), 0)
        build_id = str(version_payload.get("build_id", "")).strip() or build_id

        scheme = (parsed.scheme if parsed else "") or "http"
        if host and port > 0:
            url = f"{scheme}://{host}:{port}/"

        resolved_host = host
        resolved_port = int(port)
        resolved_refresh_sec = int(refresh_sec)

        meta_out = {
            "started_at": str(version_payload.get("started_at", "")).strip() or str(meta.get("started_at", "")).strip(),
            "host": host,
            "port": int(port),
            "refresh_sec": int(refresh_sec),
            "url": url,
            "build_id": build_id,
        }
        if meta_out != meta:
            _write_json_file(config.dashboard_meta_file, meta_out)
            meta = meta_out

    running = running_pid or running_http

    return {
        "running": running,
        "running_pid": running_pid,
        "running_http": running_http,
        "pid": pid,
        "url": url,
        "host": resolved_host,
        "port": resolved_port,
        "refresh_sec": resolved_refresh_sec,
        "build_id": build_id,
        "expected_build_id": expected_build_id,
        # Missing build-id is treated as current to avoid flapping restarts when meta files are stale.
        "build_current": bool((not build_id) or (build_id == expected_build_id)),
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
        # Background orchestration must be deterministic: never hop to a different
        # port when the requested one is occupied, otherwise users keep seeing stale UIs.
        "--port-scan",
        "0",
    ]
    if not open_browser:
        cmd.append("--no-browser")

    kwargs: dict[str, Any] = {
        "cwd": str(config.root_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
        "env": _runtime_env(_load_env_file(config.env_file), root_dir=config.root_dir),
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


def _dedupe_resolved_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _response_metrics_timeseries_snapshot(config: ManagerConfig, lane_items: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_paths: list[Path] = [config.metrics_file]
    for lane in lane_items:
        raw = str(lane.get("metrics_file", "")).strip()
        if raw:
            candidate_paths.append(Path(raw))
    paths = _dedupe_resolved_paths(candidate_paths)

    now_utc = _now_utc()
    window_starts: dict[str, dt.datetime] = {
        "last_hour": now_utc - dt.timedelta(hours=1),
        "today": now_utc.replace(hour=0, minute=0, second=0, microsecond=0),
        "last_7d": now_utc - dt.timedelta(days=7),
        "last_30d": now_utc - dt.timedelta(days=30),
    }
    window_costs = {key: 0.0 for key in window_starts}
    window_tokens = {key: 0 for key in window_starts}
    window_responses = {key: 0 for key in window_starts}

    bucket_end = now_utc.replace(minute=0, second=0, microsecond=0)
    bucket_start = bucket_end - dt.timedelta(hours=23)
    bucket_rows: dict[str, dict[str, Any]] = {}
    series_hourly_24h: list[dict[str, Any]] = []
    for index in range(24):
        bucket_time = bucket_start + dt.timedelta(hours=index)
        key = bucket_time.isoformat()
        row = {
            "bucket_start": key,
            "cost_usd_total": 0.0,
            "tokens_total": 0,
            "responses": 0,
        }
        bucket_rows[key] = row
        series_hourly_24h.append(row)

    provider_cost_raw: dict[str, dict[str, Any]] = {}
    model_cost_raw: dict[str, dict[str, Any]] = {}
    latest_event_timestamp = ""
    latest_event_parsed: dt.datetime | None = None
    files_scanned = 0
    events_scanned = 0
    errors: list[str] = []

    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        files_scanned += 1
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    parsed_ts = _parse_iso_timestamp(payload.get("timestamp"))
                    if parsed_ts is None or parsed_ts > now_utc:
                        continue

                    events_scanned += 1
                    if latest_event_parsed is None or parsed_ts >= latest_event_parsed:
                        latest_event_parsed = parsed_ts
                        latest_event_timestamp = parsed_ts.isoformat()

                    event_cost = _float_value(payload.get("cost_usd", 0.0), 0.0)
                    if event_cost < 0.0:
                        event_cost = 0.0
                    event_tokens = _int_value(payload.get("total_tokens", 0), 0)
                    if event_tokens < 0:
                        event_tokens = 0

                    for key, start in window_starts.items():
                        if parsed_ts >= start:
                            window_costs[key] += event_cost
                            window_tokens[key] += event_tokens
                            window_responses[key] += 1

                    if parsed_ts >= window_starts["last_30d"]:
                        provider = (
                            str(payload.get("routing_provider", "")).strip()
                            or str(payload.get("owner", "")).strip()
                            or "unknown"
                        )
                        model = (
                            str(payload.get("routing_selected_model", "")).strip()
                            or str(payload.get("model", "")).strip()
                            or "unknown"
                        )
                        provider_row = provider_cost_raw.setdefault(
                            provider,
                            {"responses": 0, "cost_usd_total": 0.0, "tokens_total": 0},
                        )
                        provider_row["responses"] = _int_value(provider_row.get("responses", 0), 0) + 1
                        provider_row["cost_usd_total"] = _float_value(
                            provider_row.get("cost_usd_total", 0.0),
                            0.0,
                        ) + event_cost
                        provider_row["tokens_total"] = _int_value(provider_row.get("tokens_total", 0), 0) + event_tokens

                        model_row = model_cost_raw.setdefault(
                            model,
                            {"responses": 0, "cost_usd_total": 0.0, "tokens_total": 0},
                        )
                        model_row["responses"] = _int_value(model_row.get("responses", 0), 0) + 1
                        model_row["cost_usd_total"] = _float_value(
                            model_row.get("cost_usd_total", 0.0),
                            0.0,
                        ) + event_cost
                        model_row["tokens_total"] = _int_value(model_row.get("tokens_total", 0), 0) + event_tokens

                    hour_bucket = parsed_ts.replace(minute=0, second=0, microsecond=0)
                    if bucket_start <= hour_bucket <= bucket_end:
                        row = bucket_rows.get(hour_bucket.isoformat())
                        if row is not None:
                            row["cost_usd_total"] = _float_value(row.get("cost_usd_total", 0.0), 0.0) + event_cost
                            row["tokens_total"] = _int_value(row.get("tokens_total", 0), 0) + event_tokens
                            row["responses"] = _int_value(row.get("responses", 0), 0) + 1
        except Exception as err:
            errors.append(f"{path}: {err}")

    def _finalize_cost_split(source_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        ordered = sorted(
            source_map.items(),
            key=lambda item: (
                -_float_value(item[1].get("cost_usd_total", 0.0), 0.0),
                str(item[0]),
            ),
        )
        out: dict[str, dict[str, Any]] = {}
        for name, payload in ordered:
            responses = _int_value(payload.get("responses", 0), 0)
            cost_total = _float_value(payload.get("cost_usd_total", 0.0), 0.0)
            tokens_total = _int_value(payload.get("tokens_total", 0), 0)
            out[str(name)] = {
                "responses": responses,
                "cost_usd_total": round(cost_total, 8),
                "tokens_total": tokens_total,
                "cost_per_million_tokens": round(
                    ((cost_total * 1_000_000.0) / tokens_total) if tokens_total > 0 else 0.0,
                    6,
                ),
            }
        return out

    for row in series_hourly_24h:
        row["cost_usd_total"] = round(_float_value(row.get("cost_usd_total", 0.0), 0.0), 8)
        row["tokens_total"] = _int_value(row.get("tokens_total", 0), 0)
        row["responses"] = _int_value(row.get("responses", 0), 0)

    freshness_age_sec = -1
    if latest_event_parsed is not None:
        freshness_age_sec = max(0, int((now_utc - latest_event_parsed).total_seconds()))
    stale_threshold_sec = 900
    stale = freshness_age_sec < 0 or freshness_age_sec > stale_threshold_sec

    return {
        "cost_windows_usd": {
            key: round(_float_value(value, 0.0), 8) for key, value in window_costs.items()
        },
        "cost_windows_tokens": {
            key: _int_value(value, 0) for key, value in window_tokens.items()
        },
        "cost_windows_responses": {
            key: _int_value(value, 0) for key, value in window_responses.items()
        },
        "provider_cost_30d": _finalize_cost_split(provider_cost_raw),
        "model_cost_30d": _finalize_cost_split(model_cost_raw),
        "cost_series_hourly_24h": series_hourly_24h,
        "data_freshness": {
            "latest_event_timestamp": latest_event_timestamp,
            "age_sec": freshness_age_sec,
            "stale": stale,
            "stale_threshold_sec": stale_threshold_sec,
            "files_scanned": files_scanned,
            "events_scanned": events_scanned,
        },
        "timeseries_files": [str(path) for path in paths],
        "timeseries_errors": errors,
    }


def _provider_authoritative_cost_snapshot(config: ManagerConfig, lane_items: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_paths: list[Path] = [config.provider_cost_records_file]
    for lane in lane_items:
        raw = str(lane.get("provider_cost_records_file", "")).strip()
        if raw:
            candidate_paths.append(Path(raw))
    record_paths = _dedupe_resolved_paths(candidate_paths)

    records, errors, meta = load_canonical_records(record_paths)
    summary_payload: dict[str, Any] = {}
    if config.provider_cost_summary_file.exists():
        try:
            raw_summary = json.loads(config.provider_cost_summary_file.read_text(encoding="utf-8"))
            if isinstance(raw_summary, dict):
                summary_payload = raw_summary
        except Exception as err:
            errors.append(f"{config.provider_cost_summary_file}: {err}")

    aggregate = aggregate_canonical_records(
        records,
        now_utc=_now_utc(),
        stale_threshold_sec=max(1, config.provider_cost_stale_sec),
    )
    records_total = _int_value(meta.get("records_total", 0), 0)
    if records_total <= 0 and summary_payload:
        aggregate = {
            "records_total": _int_value(summary_payload.get("records_total", 0), 0),
            "currency": str(summary_payload.get("currency", "USD")).strip().upper() or "USD",
            "source_of_truth": str(
                summary_payload.get("source_of_truth", CANONICAL_SOURCE_AUTHORITATIVE)
            ).strip()
            or CANONICAL_SOURCE_AUTHORITATIVE,
            "cost_windows_usd": summary_payload.get("cost_windows_usd", {}),
            "cost_windows_tokens": summary_payload.get("cost_windows_tokens", {}),
            "cost_windows_responses": summary_payload.get("cost_windows_responses", {}),
            "provider_cost_30d": summary_payload.get("provider_cost_30d", {}),
            "model_cost_30d": summary_payload.get("model_cost_30d", {}),
            "cost_series_hourly_24h": summary_payload.get("cost_series_hourly_24h", []),
            "data_freshness": summary_payload.get("data_freshness", {}),
        }
    freshness = (
        aggregate.get("data_freshness", {})
        if isinstance(aggregate.get("data_freshness", {}), dict)
        else {}
    )
    freshness["files_scanned"] = max(
        _int_value(meta.get("files_scanned", 0), 0),
        _int_value(freshness.get("files_scanned", 0), 0),
    )
    freshness["events_scanned"] = max(
        _int_value(meta.get("records_total", 0), 0),
        _int_value(freshness.get("events_scanned", 0), 0),
    )
    aggregate["data_freshness"] = freshness

    records_total = max(
        _int_value(aggregate.get("records_total", 0), 0),
        _int_value(meta.get("records_total", 0), 0),
    )
    return {
        **aggregate,
        "records_total": records_total,
        "ok": records_total > 0 and len(errors) == 0,
        "partial": records_total > 0 and len(errors) > 0,
        "errors": errors,
        "records_files": [str(path) for path in record_paths],
        "summary_file": str(config.provider_cost_summary_file),
    }


def _swarm_budget_status(
    *,
    daily_budget_usd: float,
    warning_ratio: float,
    enforce_hard_stop: bool,
    daily_spend_usd: float,
    rolling_7d_spend_usd: float,
    source_of_truth: str,
    data_freshness: dict[str, Any] | None,
) -> dict[str, Any]:
    budget = max(0.0, _float_value(daily_budget_usd, 0.0))
    warn_ratio = _float_value(warning_ratio, 0.8)
    if not (0.0 < warn_ratio < 1.0):
        warn_ratio = 0.8
    spend_today = max(0.0, _float_value(daily_spend_usd, 0.0))
    spend_7d = max(0.0, _float_value(rolling_7d_spend_usd, 0.0))
    enabled = budget > 0.0
    threshold = budget * warn_ratio if enabled else 0.0
    utilization_ratio = (spend_today / budget) if enabled else 0.0
    hard_stop = bool(enforce_hard_stop) and enabled and spend_today >= budget
    state = "disabled"
    if enabled:
        if spend_today > budget:
            state = "exceeded"
        elif spend_today >= threshold:
            state = "warning"
        else:
            state = "ok"
    return {
        "enabled": enabled,
        "state": state,
        "daily_budget_usd": round(budget, 8),
        "daily_warning_threshold_usd": round(threshold, 8),
        "daily_spend_usd": round(spend_today, 8),
        "daily_remaining_usd": round(max(0.0, budget - spend_today) if enabled else 0.0, 8),
        "rolling_7d_spend_usd": round(spend_7d, 8),
        "warning_ratio": round(warn_ratio, 6),
        "utilization_ratio": round(utilization_ratio, 6),
        "utilization_percent": round(utilization_ratio * 100.0, 2),
        "enforce_hard_stop": bool(enforce_hard_stop),
        "hard_stop": hard_stop,
        "source_of_truth": str(source_of_truth).strip() or CANONICAL_SOURCE_ESTIMATED,
        "data_freshness": dict(data_freshness) if isinstance(data_freshness, dict) else {},
    }


def _current_swarm_budget_status(config: ManagerConfig, lane_items: list[dict[str, Any]]) -> dict[str, Any]:
    authoritative = _provider_authoritative_cost_snapshot(config, lane_items)
    authoritative_records_total = _int_value(authoritative.get("records_total", 0), 0)
    cost_source_payload = authoritative
    source_of_truth = str(authoritative.get("source_of_truth", CANONICAL_SOURCE_AUTHORITATIVE)).strip()
    if authoritative_records_total <= 0:
        timeseries = _response_metrics_timeseries_snapshot(config, lane_items)
        cost_source_payload = timeseries
        source_of_truth = str(timeseries.get("source_of_truth", CANONICAL_SOURCE_ESTIMATED)).strip()
        if not source_of_truth:
            source_of_truth = CANONICAL_SOURCE_ESTIMATED

    cost_windows = (
        cost_source_payload.get("cost_windows_usd", {})
        if isinstance(cost_source_payload.get("cost_windows_usd", {}), dict)
        else {}
    )
    data_freshness = (
        cost_source_payload.get("data_freshness", {})
        if isinstance(cost_source_payload.get("data_freshness", {}), dict)
        else {}
    )
    return _swarm_budget_status(
        daily_budget_usd=config.swarm_daily_budget_usd,
        warning_ratio=config.swarm_budget_warning_ratio,
        enforce_hard_stop=config.swarm_budget_enforce_hard_stop,
        daily_spend_usd=_float_value(cost_windows.get("today", 0.0), 0.0),
        rolling_7d_spend_usd=_float_value(cost_windows.get("last_7d", 0.0), 0.0),
        source_of_truth=source_of_truth,
        data_freshness=data_freshness,
    )


def _empty_response_metrics(error: str = "") -> dict[str, Any]:
    message = str(error).strip()
    return {
        "timestamp": _now_iso(),
        "summary_file": "",
        "timeseries_files": [],
        "authoritative_cost_files": [],
        "authoritative_cost_summary_file": "",
        "sources": [],
        "ok": False,
        "partial": True,
        "errors": [message] if message else [],
        "timeseries_errors": [],
        "authoritative_cost_errors": [],
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
        "estimated_tokens_total": 0,
        "tokens_input_total": 0,
        "tokens_output_total": 0,
        "tokens_avg": 0.0,
        "token_exact_count": 0,
        "token_exact_coverage": 0.0,
        "token_rate_per_minute": 0.0,
        "estimated_cost_per_million_tokens": 0.0,
        "routing_decisions_total": 0,
        "routing_routellm_count": 0,
        "routing_routellm_rate": 0.0,
        "routing_fallback_count": 0,
        "routing_fallback_rate": 0.0,
        "routing_router_error_count": 0,
        "routing_router_error_rate": 0.0,
        "routing_router_latency_sum": 0.0,
        "routing_router_latency_avg": 0.0,
        "routing_by_provider": {},
        "currency": "USD",
        "source_of_truth": CANONICAL_SOURCE_ESTIMATED,
        "authoritative_cost_available": False,
        "authoritative_cost_records_total": 0,
        "by_owner": {},
        "latest_metric": {},
        "cost_windows_usd": {"last_hour": 0.0, "today": 0.0, "last_7d": 0.0, "last_30d": 0.0},
        "cost_windows_tokens": {"last_hour": 0, "today": 0, "last_7d": 0, "last_30d": 0},
        "cost_windows_responses": {"last_hour": 0, "today": 0, "last_7d": 0, "last_30d": 0},
        "provider_cost_30d": {},
        "model_cost_30d": {},
        "cost_series_hourly_24h": [],
        "data_freshness": {
            "latest_event_timestamp": "",
            "age_sec": -1,
            "stale": True,
            "stale_threshold_sec": 900,
            "files_scanned": 0,
            "events_scanned": 0,
        },
        "authoritative_cost": {},
        "swarm_daily_budget": {
            "enabled": False,
            "state": "disabled",
            "daily_budget_usd": 0.0,
            "daily_warning_threshold_usd": 0.0,
            "daily_spend_usd": 0.0,
            "daily_remaining_usd": 0.0,
            "rolling_7d_spend_usd": 0.0,
            "warning_ratio": 0.8,
            "utilization_ratio": 0.0,
            "utilization_percent": 0.0,
            "enforce_hard_stop": False,
            "hard_stop": False,
            "source_of_truth": CANONICAL_SOURCE_ESTIMATED,
            "data_freshness": {},
        },
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
    paths = _dedupe_resolved_paths(candidate_paths)

    reports: list[dict[str, Any]] = []
    errors: list[str] = []
    recommendations: list[str] = []
    by_owner: dict[str, dict[str, Any]] = {}
    latest_metric: dict[str, Any] = {}
    latest_timestamp = ""
    latest_timestamp_parsed: dt.datetime | None = None
    latest_timestamp_is_valid = False

    responses_total = 0
    quality_sum = 0.0
    latency_sum = 0.0
    prompt_difficulty_sum = 0.0
    first_time_pass_count = 0
    acceptance_pass_count = 0
    exact_cost_count = 0
    cost_usd_total = 0.0
    tokens_total = 0
    estimated_tokens_total = 0
    tokens_input_total = 0
    tokens_output_total = 0
    token_exact_count = 0
    routing_decisions_total = 0
    routing_routellm_count = 0
    routing_fallback_count = 0
    routing_router_error_count = 0
    routing_router_latency_sum = 0.0
    routing_by_provider: dict[str, dict[str, Any]] = {}
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
        source_estimated_tokens_total = _int_value(summary.get("estimated_tokens_total", source_tokens_total), source_tokens_total)
        source_tokens_input_total = _int_value(summary.get("tokens_input_total", 0), 0)
        source_tokens_output_total = _int_value(summary.get("tokens_output_total", 0), 0)
        source_token_exact_count = _int_value(
            summary.get("token_exact_count", round(_float_value(summary.get("token_exact_coverage", 0.0), 0.0) * source_responses)),
            0,
        )
        source_routing_decisions = _int_value(summary.get("routing_decisions_total", source_responses), source_responses)
        source_routing_routellm = _int_value(
            summary.get(
                "routing_routellm_count",
                round(_float_value(summary.get("routing_routellm_rate", 0.0), 0.0) * source_routing_decisions),
            ),
            0,
        )
        source_routing_fallback = _int_value(
            summary.get(
                "routing_fallback_count",
                round(_float_value(summary.get("routing_fallback_rate", 0.0), 0.0) * source_routing_decisions),
            ),
            0,
        )
        source_routing_router_error = _int_value(
            summary.get(
                "routing_router_error_count",
                round(_float_value(summary.get("routing_router_error_rate", 0.0), 0.0) * source_routing_decisions),
            ),
            0,
        )
        source_routing_latency_sum = _float_value(
            summary.get(
                "routing_router_latency_sum",
                _float_value(summary.get("routing_router_latency_avg", 0.0), 0.0) * source_routing_decisions,
            ),
            0.0,
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
        estimated_tokens_total += source_estimated_tokens_total
        tokens_input_total += source_tokens_input_total
        tokens_output_total += source_tokens_output_total
        token_exact_count += source_token_exact_count
        routing_decisions_total += source_routing_decisions
        routing_routellm_count += source_routing_routellm
        routing_fallback_count += source_routing_fallback
        routing_router_error_count += source_routing_router_error
        routing_router_latency_sum += source_routing_latency_sum

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
                        "routing_routellm_count": 0,
                        "routing_fallback_count": 0,
                        "routing_router_error_count": 0,
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
                aggregate_owner["routing_routellm_count"] = _int_value(
                    aggregate_owner.get("routing_routellm_count", 0),
                    0,
                ) + _int_value(owner_payload.get("routing_routellm_count", 0), 0)
                aggregate_owner["routing_fallback_count"] = _int_value(
                    aggregate_owner.get("routing_fallback_count", 0),
                    0,
                ) + _int_value(owner_payload.get("routing_fallback_count", 0), 0)
                aggregate_owner["routing_router_error_count"] = _int_value(
                    aggregate_owner.get("routing_router_error_count", 0),
                    0,
                ) + _int_value(owner_payload.get("routing_router_error_count", 0), 0)
                by_owner[owner] = aggregate_owner

        provider_map = summary.get("routing_by_provider", {})
        if isinstance(provider_map, dict):
            for provider_name, provider_payload in provider_map.items():
                if not isinstance(provider_payload, dict):
                    continue
                provider = str(provider_name).strip() or "unknown"
                aggregate_provider = routing_by_provider.get(
                    provider,
                    {
                        "responses": 0,
                        "routellm_count": 0,
                        "fallback_count": 0,
                        "router_error_count": 0,
                        "cost_usd_total": 0.0,
                        "tokens_total": 0,
                    },
                )
                aggregate_provider["responses"] = _int_value(aggregate_provider.get("responses", 0), 0) + _int_value(
                    provider_payload.get("responses", 0),
                    0,
                )
                aggregate_provider["routellm_count"] = _int_value(
                    aggregate_provider.get("routellm_count", 0),
                    0,
                ) + _int_value(provider_payload.get("routellm_count", 0), 0)
                aggregate_provider["fallback_count"] = _int_value(
                    aggregate_provider.get("fallback_count", 0),
                    0,
                ) + _int_value(provider_payload.get("fallback_count", 0), 0)
                aggregate_provider["router_error_count"] = _int_value(
                    aggregate_provider.get("router_error_count", 0),
                    0,
                ) + _int_value(provider_payload.get("router_error_count", 0), 0)
                aggregate_provider["cost_usd_total"] = _float_value(
                    aggregate_provider.get("cost_usd_total", 0.0),
                    0.0,
                ) + _float_value(provider_payload.get("cost_usd_total", 0.0), 0.0)
                aggregate_provider["tokens_total"] = _int_value(
                    aggregate_provider.get("tokens_total", 0),
                    0,
                ) + _int_value(provider_payload.get("tokens_total", 0), 0)
                routing_by_provider[provider] = aggregate_provider

        latest = summary.get("latest_metric", {})
        if isinstance(latest, dict):
            ts = str(latest.get("timestamp", "")).strip()
            if ts:
                parsed = _parse_iso_timestamp(ts)
                should_replace = False
                if parsed is not None:
                    if (not latest_timestamp_is_valid) or latest_timestamp_parsed is None or parsed >= latest_timestamp_parsed:
                        should_replace = True
                elif not latest_timestamp_is_valid:
                    # When all timestamps are invalid, preserve source order by
                    # allowing later entries to replace earlier ones.
                    should_replace = True
                if should_replace:
                    latest_timestamp = ts
                    latest_timestamp_parsed = parsed
                    latest_timestamp_is_valid = parsed is not None
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
        owner_tokens_total = _int_value(owner_payload.get("tokens_total", 0), 0)
        owner_cost_total = _float_value(owner_payload.get("cost_usd_total", 0.0), 0.0)
        owner_payload["cost_per_million_tokens"] = round(
            ((owner_cost_total * 1_000_000.0) / owner_tokens_total) if owner_tokens_total > 0 else 0.0,
            6,
        )
        owner_payload["routing_routellm_rate"] = round(
            _int_value(owner_payload.get("routing_routellm_count", 0), 0) / owner_responses,
            6,
        )
        owner_payload["routing_fallback_rate"] = round(
            _int_value(owner_payload.get("routing_fallback_count", 0), 0) / owner_responses,
            6,
        )
        owner_payload["routing_router_error_rate"] = round(
            _int_value(owner_payload.get("routing_router_error_count", 0), 0) / owner_responses,
            6,
        )

    for provider_payload in routing_by_provider.values():
        provider_responses = max(1, _int_value(provider_payload.get("responses", 0), 0))
        provider_payload["routellm_rate"] = round(
            _int_value(provider_payload.get("routellm_count", 0), 0) / provider_responses,
            6,
        )
        provider_payload["fallback_rate"] = round(
            _int_value(provider_payload.get("fallback_count", 0), 0) / provider_responses,
            6,
        )
        provider_payload["router_error_rate"] = round(
            _int_value(provider_payload.get("router_error_count", 0), 0) / provider_responses,
            6,
        )
        provider_tokens_total = _int_value(provider_payload.get("tokens_total", 0), 0)
        provider_cost_total = _float_value(provider_payload.get("cost_usd_total", 0.0), 0.0)
        provider_payload["cost_per_million_tokens"] = round(
            ((provider_cost_total * 1_000_000.0) / provider_tokens_total) if provider_tokens_total > 0 else 0.0,
            6,
        )

    coverage = round(exact_cost_count / max(1, responses_total), 6)
    token_exact_coverage = round(token_exact_count / max(1, responses_total), 6)
    if estimated_tokens_total <= 0:
        estimated_tokens_total = tokens_total
    estimated_cost_per_million_tokens = (
        (cost_usd_total * 1_000_000.0) / estimated_tokens_total if estimated_tokens_total > 0 else 0.0
    )
    token_rate_per_minute = 0.0
    if latency_sum > 0.0:
        token_rate_per_minute = (float(tokens_total) / latency_sum) * 60.0
    timeseries = _response_metrics_timeseries_snapshot(config, lane_items)
    timeseries_errors = [
        str(item).strip()
        for item in timeseries.get("timeseries_errors", [])
        if str(item).strip()
    ]
    authoritative = _provider_authoritative_cost_snapshot(config, lane_items)
    authoritative_errors = [
        str(item).strip()
        for item in authoritative.get("errors", [])
        if str(item).strip()
    ]
    authoritative_records_total = _int_value(authoritative.get("records_total", 0), 0)
    authoritative_available = authoritative_records_total > 0
    cost_source_payload = authoritative if authoritative_available else timeseries
    source_of_truth = str(
        cost_source_payload.get(
            "source_of_truth",
            CANONICAL_SOURCE_AUTHORITATIVE if authoritative_available else CANONICAL_SOURCE_ESTIMATED,
        )
    ).strip() or (CANONICAL_SOURCE_AUTHORITATIVE if authoritative_available else CANONICAL_SOURCE_ESTIMATED)
    source_cost_windows = (
        cost_source_payload.get("cost_windows_usd", {})
        if isinstance(cost_source_payload.get("cost_windows_usd", {}), dict)
        else {}
    )
    source_freshness = (
        cost_source_payload.get("data_freshness", {})
        if isinstance(cost_source_payload.get("data_freshness", {}), dict)
        else {}
    )
    swarm_daily_budget = _swarm_budget_status(
        daily_budget_usd=config.swarm_daily_budget_usd,
        warning_ratio=config.swarm_budget_warning_ratio,
        enforce_hard_stop=config.swarm_budget_enforce_hard_stop,
        daily_spend_usd=_float_value(source_cost_windows.get("today", 0.0), 0.0),
        rolling_7d_spend_usd=_float_value(source_cost_windows.get("last_7d", 0.0), 0.0),
        source_of_truth=source_of_truth,
        data_freshness=source_freshness,
    )
    budget_state = str(swarm_daily_budget.get("state", "disabled")).strip().lower()
    if budget_state in {"warning", "exceeded"}:
        budget_note = (
            "Swarm daily budget nearing cap "
            if budget_state == "warning"
            else "Swarm daily budget exceeded "
        )
        budget_note += (
            f"(today=${_float_value(swarm_daily_budget.get('daily_spend_usd', 0.0), 0.0):.4f} "
            f"/ cap=${_float_value(swarm_daily_budget.get('daily_budget_usd', 0.0), 0.0):.4f})."
        )
        if budget_note not in recommendations:
            recommendations.append(budget_note)
    errors_with_timeseries = list(errors)
    errors_with_timeseries.extend(timeseries_errors)
    errors_with_timeseries.extend(f"authoritative_cost: {item}" for item in authoritative_errors)
    snapshot = {
        "timestamp": _now_iso(),
        "summary_file": str(config.metrics_summary_file),
        "timeseries_files": list(timeseries.get("timeseries_files", [])),
        "authoritative_cost_files": list(authoritative.get("records_files", [])),
        "authoritative_cost_summary_file": str(authoritative.get("summary_file", "")),
        "sources": reports,
        "ok": len(errors_with_timeseries) == 0,
        "partial": len(errors_with_timeseries) > 0,
        "errors": errors_with_timeseries,
        "timeseries_errors": timeseries_errors,
        "authoritative_cost_errors": authoritative_errors,
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
        "estimated_tokens_total": estimated_tokens_total,
        "tokens_input_total": tokens_input_total,
        "tokens_output_total": tokens_output_total,
        "tokens_avg": round(tokens_total / max(1, responses_total), 6),
        "token_exact_count": token_exact_count,
        "token_exact_coverage": token_exact_coverage,
        "token_rate_per_minute": round(token_rate_per_minute, 6),
        "estimated_cost_per_million_tokens": round(estimated_cost_per_million_tokens, 6),
        "routing_decisions_total": routing_decisions_total,
        "routing_routellm_count": routing_routellm_count,
        "routing_routellm_rate": round(routing_routellm_count / max(1, routing_decisions_total), 6),
        "routing_fallback_count": routing_fallback_count,
        "routing_fallback_rate": round(routing_fallback_count / max(1, routing_decisions_total), 6),
        "routing_router_error_count": routing_router_error_count,
        "routing_router_error_rate": round(routing_router_error_count / max(1, routing_decisions_total), 6),
        "routing_router_latency_sum": round(routing_router_latency_sum, 6),
        "routing_router_latency_avg": round(routing_router_latency_sum / max(1, routing_decisions_total), 6),
        "routing_by_provider": routing_by_provider,
        "currency": currency,
        "source_of_truth": source_of_truth,
        "authoritative_cost_available": authoritative_available,
        "authoritative_cost_records_total": authoritative_records_total,
        "by_owner": by_owner,
        "latest_metric": latest_metric,
        "cost_windows_usd": dict(cost_source_payload.get("cost_windows_usd", {})),
        "cost_windows_tokens": dict(cost_source_payload.get("cost_windows_tokens", {})),
        "cost_windows_responses": dict(cost_source_payload.get("cost_windows_responses", {})),
        "provider_cost_30d": dict(cost_source_payload.get("provider_cost_30d", {})),
        "model_cost_30d": dict(cost_source_payload.get("model_cost_30d", {})),
        "cost_series_hourly_24h": list(cost_source_payload.get("cost_series_hourly_24h", [])),
        "data_freshness": dict(cost_source_payload.get("data_freshness", {})),
        "authoritative_cost": authoritative,
        "swarm_daily_budget": swarm_daily_budget,
        "optimization_recommendations": recommendations,
    }
    snapshot["exciting_stat"] = _build_exciting_stat(snapshot)
    return snapshot


def _normalize_error_messages(raw: Any) -> list[str]:
    if isinstance(raw, list):
        values = raw
    elif raw in (None, "", []):
        values = []
    else:
        values = [raw]
    normalized: list[str] = []
    for item in values:
        message = str(item).strip()
        if message:
            normalized.append(message)
    return normalized


def _lane_conversation_rollup(conversation_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source_reports = conversation_payload.get("sources", [])
    if not isinstance(source_reports, list):
        source_reports = []
    events = conversation_payload.get("events", [])
    if not isinstance(events, list):
        events = []

    rollup_raw: dict[str, dict[str, Any]] = {}

    def _entry(lane_id: str) -> dict[str, Any]:
        return rollup_raw.setdefault(
            lane_id,
            {
                "lane_id": lane_id,
                "owner_hints": {},
                "source_count": 0,
                "source_ok": None,
                "source_event_count": 0,
                "source_error_count": 0,
                "missing_count": 0,
                "recoverable_missing_count": 0,
                "fallback_count": 0,
                "observed_event_count": 0,
                "latest_event": {},
            },
        )

    for item in source_reports:
        if not isinstance(item, dict):
            continue
        lane_key = str(item.get("lane_id", "")).strip()
        if not lane_key:
            continue
        current = _entry(lane_key)
        source_owner = str(item.get("owner", "")).strip() or "unknown"
        if source_owner != "unknown":
            owner_hints = current.get("owner_hints")
            if not isinstance(owner_hints, dict):
                owner_hints = {}
                current["owner_hints"] = owner_hints
            owner_hints[source_owner] = _int_value(owner_hints.get(source_owner, 0), 0) + 1
        current["source_count"] += 1
        current_ok = bool(item.get("ok", False))
        if current["source_ok"] is None:
            current["source_ok"] = current_ok
        else:
            current["source_ok"] = bool(current["source_ok"]) and current_ok
        current["source_event_count"] += max(0, _int_value(item.get("event_count", 0), 0))
        if str(item.get("error", "")).strip():
            current["source_error_count"] += 1
        if bool(item.get("missing", False)):
            current["missing_count"] += 1
        if bool(item.get("recoverable_missing", False)):
            current["recoverable_missing_count"] += 1
        if bool(item.get("fallback_used", False)):
            current["fallback_count"] += 1

    for item in events:
        if not isinstance(item, dict):
            continue
        lane_key = str(item.get("lane_id", "")).strip()
        if not lane_key:
            continue
        current = _entry(lane_key)
        current["observed_event_count"] += 1
        event_owner = str(item.get("owner", "")).strip() or "unknown"
        if event_owner != "unknown":
            owner_hints = current.get("owner_hints")
            if not isinstance(owner_hints, dict):
                owner_hints = {}
                current["owner_hints"] = owner_hints
            owner_hints[event_owner] = _int_value(owner_hints.get(event_owner, 0), 0) + 1
        candidate = {
            "timestamp": str(item.get("timestamp", "")).strip(),
            "owner": event_owner,
            "lane_id": lane_key,
            "task_id": str(item.get("task_id", "")).strip(),
            "event_type": str(item.get("event_type", "")).strip(),
            "content": str(item.get("content", "")).strip(),
            "source": str(item.get("source", "")).strip(),
            "source_kind": str(item.get("source_kind", "")).strip(),
        }
        existing = current.get("latest_event", {})
        existing_ts = _parse_iso_timestamp(existing.get("timestamp")) if isinstance(existing, dict) else None
        candidate_ts = _parse_iso_timestamp(candidate.get("timestamp"))
        if existing_ts is None and candidate_ts is None:
            # Preserve source sequence when timestamps are malformed.
            should_replace = True
        elif existing_ts is None:
            should_replace = True
        elif candidate_ts is None:
            should_replace = False
        else:
            should_replace = candidate_ts >= existing_ts
        if should_replace:
            current["latest_event"] = candidate

    rollup: dict[str, dict[str, Any]] = {}
    for lane_key, item in rollup_raw.items():
        source_ok = item.get("source_ok")
        source_state = "unreported"
        if source_ok is True:
            source_state = "ok"
        elif source_ok is False:
            source_state = "error"
        latest_event = item.get("latest_event", {}) if isinstance(item.get("latest_event", {}), dict) else {}
        owner = str(latest_event.get("owner", "")).strip() or "unknown"
        owner_hints = item.get("owner_hints", {})
        if owner == "unknown" and isinstance(owner_hints, dict):
            ordered_hints = sorted(
                (
                    (str(name).strip(), _int_value(count, 0))
                    for name, count in owner_hints.items()
                    if str(name).strip() and str(name).strip() != "unknown"
                ),
                key=lambda pair: (-pair[1], pair[0]),
            )
            if ordered_hints:
                owner = ordered_hints[0][0]
        rollup[lane_key] = {
            "lane_id": lane_key,
            "owner": owner,
            "source_count": _int_value(item.get("source_count", 0), 0),
            "source_ok": source_ok,
            "source_state": source_state,
            "event_count": max(
                _int_value(item.get("source_event_count", 0), 0),
                _int_value(item.get("observed_event_count", 0), 0),
            ),
            "source_error_count": _int_value(item.get("source_error_count", 0), 0),
            "missing_count": _int_value(item.get("missing_count", 0), 0),
            "recoverable_missing_count": _int_value(item.get("recoverable_missing_count", 0), 0),
            "fallback_count": _int_value(item.get("fallback_count", 0), 0),
            "latest_event": latest_event,
        }
    return rollup


def _lane_error_mentions_unknown_lane(error: str, lane_id: str) -> bool:
    message = str(error).strip().lower()
    lane = lane_id.strip().lower()
    if not message or not lane:
        return False
    if not message.startswith("unknown lane id "):
        return False
    return lane in message


def _lane_error_mentions_unavailable_lane(error: str, lane_id: str) -> bool:
    message = str(error).strip().lower()
    lane = lane_id.strip().lower()
    if not message or not lane:
        return False
    if not message.startswith("requested lane "):
        return False
    if "is unavailable because lane status sources failed." not in message:
        return False
    return lane in message


def _augment_lane_payload_with_conversation_rollup(
    lane_payload: dict[str, Any],
    conversation_payload: dict[str, Any],
) -> dict[str, Any]:
    rollup = _lane_conversation_rollup(conversation_payload)
    lane_items = lane_payload.get("lanes", [])
    if not isinstance(lane_items, list):
        lane_items = []

    enriched_lanes: list[dict[str, Any]] = []
    seen_lanes: set[str] = set()
    for lane in lane_items:
        if not isinstance(lane, dict):
            continue
        lane_id = str(lane.get("id", "")).strip()
        if lane_id:
            seen_lanes.add(lane_id)
        lane_rollup = rollup.get(lane_id, {})
        lane_copy = dict(lane)
        rollup_owner = str(lane_rollup.get("owner", "")).strip() or "unknown"
        if (str(lane_copy.get("owner", "")).strip() or "unknown") == "unknown" and rollup_owner != "unknown":
            lane_copy["owner"] = rollup_owner
        lane_copy["conversation_event_count"] = _int_value(lane_rollup.get("event_count", 0), 0)
        lane_copy["conversation_source_count"] = _int_value(lane_rollup.get("source_count", 0), 0)
        lane_copy["conversation_source_state"] = str(lane_rollup.get("source_state", "unreported"))
        source_ok = lane_rollup.get("source_ok")
        lane_copy["conversation_source_ok"] = source_ok if isinstance(source_ok, bool) else None
        lane_copy["conversation_source_error_count"] = _int_value(lane_rollup.get("source_error_count", 0), 0)
        lane_copy["conversation_source_missing_count"] = _int_value(lane_rollup.get("missing_count", 0), 0)
        lane_copy["conversation_source_recoverable_missing_count"] = _int_value(
            lane_rollup.get("recoverable_missing_count", 0),
            0,
        )
        lane_copy["conversation_source_fallback_count"] = _int_value(lane_rollup.get("fallback_count", 0), 0)
        lane_copy["latest_conversation_event"] = (
            lane_rollup.get("latest_event", {})
            if isinstance(lane_rollup.get("latest_event", {}), dict)
            else {}
        )
        enriched_lanes.append(lane_copy)

    recovered_lanes: list[str] = []
    lane_source_degraded = (
        not bool(lane_payload.get("ok", True))
        or bool(lane_payload.get("partial", False))
        or bool(lane_payload.get("errors", []))
    )
    if lane_source_degraded:
        synthesize_lane_ids = sorted(lane_id for lane_id in rollup if lane_id not in seen_lanes)
        for lane_id in synthesize_lane_ids:
            lane_rollup = rollup.get(lane_id, {})
            latest_event = lane_rollup.get("latest_event", {})
            if not isinstance(latest_event, dict):
                latest_event = {}
            owner = str(latest_event.get("owner", "")).strip() or ""
            if not owner or owner == "unknown":
                owner = str(lane_rollup.get("owner", "unknown")).strip() or "unknown"
            enriched_lanes.append(
                {
                    "id": lane_id,
                    "owner": owner,
                    "running": False,
                    "pid": None,
                    "health": "unknown",
                    "heartbeat_age_sec": -1,
                    "state_counts": {},
                    "build_current": False,
                    "error": "lane status missing; derived from conversation logs",
                    "conversation_lane_fallback": True,
                    "scaling_mode": "static",
                    "scaling_group": "",
                    "scaling_rank": 1,
                    "scaling_decision_file": "",
                    "scaling_min_marginal_npv_usd": 0.0,
                    "scaling_daily_budget_usd": 0.0,
                    "scaling_max_parallel_agents": 1,
                    "scaling_max_subagents_per_agent": 1,
                    "scaling_event_counts": {"scale_up": 0, "scale_down": 0, "scale_hold": 0},
                    "scale_up_events": 0,
                    "scale_down_events": 0,
                    "scale_hold_events": 0,
                    "latest_scale_event": {},
                    "conversation_event_count": _int_value(lane_rollup.get("event_count", 0), 0),
                    "conversation_source_count": _int_value(lane_rollup.get("source_count", 0), 0),
                    "conversation_source_state": str(lane_rollup.get("source_state", "unreported")),
                    "conversation_source_ok": (
                        lane_rollup.get("source_ok")
                        if isinstance(lane_rollup.get("source_ok"), bool)
                        else None
                    ),
                    "conversation_source_error_count": _int_value(lane_rollup.get("source_error_count", 0), 0),
                    "conversation_source_missing_count": _int_value(lane_rollup.get("missing_count", 0), 0),
                    "conversation_source_recoverable_missing_count": _int_value(
                        lane_rollup.get("recoverable_missing_count", 0),
                        0,
                    ),
                    "conversation_source_fallback_count": _int_value(lane_rollup.get("fallback_count", 0), 0),
                    "latest_conversation_event": latest_event,
                }
            )
            recovered_lanes.append(lane_id)

    conversation_errors = _normalize_error_messages(conversation_payload.get("errors", []))
    combined_lane_errors = _normalize_error_messages(lane_payload.get("errors", []))
    if recovered_lanes:
        combined_lane_errors = [
            message
            for message in combined_lane_errors
            if not any(
                _lane_error_mentions_unknown_lane(message, lane_id)
                or _lane_error_mentions_unavailable_lane(message, lane_id)
                for lane_id in recovered_lanes
            )
        ]
        for lane_id in recovered_lanes:
            warning = f"Lane status missing for {lane_id!r}; using conversation-derived fallback."
            if warning not in combined_lane_errors:
                combined_lane_errors.append(warning)

    health_counts, owner_counts = _lane_health_owner_counts(enriched_lanes)
    scaling_event_counts = {"scale_up": 0, "scale_down": 0, "scale_hold": 0}
    for lane in enriched_lanes:
        counts = lane.get("scaling_event_counts", {})
        if not isinstance(counts, dict):
            continue
        for key in scaling_event_counts:
            scaling_event_counts[key] += _int_value(counts.get(key, 0), 0)
    running_count = sum(1 for lane in enriched_lanes if bool(lane.get("running", False)))
    lane_partial = bool(lane_payload.get("partial", False))
    conversation_partial = bool(conversation_payload.get("partial", False))
    partial = lane_partial or conversation_partial or bool(combined_lane_errors) or bool(recovered_lanes)
    ok = bool(lane_payload.get("ok", not combined_lane_errors)) and not partial

    result = dict(lane_payload)
    result["lanes"] = enriched_lanes
    result["running_count"] = running_count
    result["total_count"] = len(enriched_lanes)
    result["health_counts"] = health_counts
    result["owner_counts"] = owner_counts
    result["scaling_event_counts"] = scaling_event_counts
    result["errors"] = combined_lane_errors
    result["partial"] = partial
    result["ok"] = ok
    result["recovered_lanes"] = recovered_lanes
    result["recovered_lane_count"] = len(recovered_lanes)
    result["conversation_by_lane"] = rollup
    result["conversation_partial"] = conversation_partial
    result["conversation_ok"] = bool(conversation_payload.get("ok", False))
    result["conversation_errors"] = conversation_errors
    return result


def monitor_snapshot(config: ManagerConfig) -> dict[str, Any]:
    status = status_snapshot(config)
    diagnostics: dict[str, Any] = {"ok": True, "errors": [], "sources": {}}

    def _error_text(raw: Any) -> str:
        return "; ".join(_normalize_error_messages(raw))

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
        "partial": True,
        "errors": [],
    }
    try:
        lanes = lane_status_snapshot(config)
    except Exception as err:
        try:
            lanes = lane_status_fallback_snapshot(config, error=str(err))
        except Exception as fallback_err:
            lanes["errors"] = [str(err), f"lane_status_fallback: {fallback_err}"]
            lanes["error"] = str(err)
            lanes["partial"] = True
    _mark_source("lanes", bool(lanes.get("ok", False)), _error_text(lanes.get("errors", [])))
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
        message = str(err)
        conv["errors"] = [message]
        conv["partial"] = True
        conv["ok"] = False
        conv["sources"] = [
            {
                "path": str(config.conversation_log_file),
                "resolved_path": str(config.conversation_log_file),
                "kind": "primary",
                "resolved_kind": "primary",
                "lane_id": "",
                "owner": "",
                "ok": False,
                "missing": not config.conversation_log_file.exists(),
                "recoverable_missing": False,
                "fallback_used": False,
                "error": message,
                "event_count": 0,
            }
        ]
    conversation_errors = _normalize_error_messages(conv.get("errors", []))
    _mark_source("conversations", bool(conv.get("ok", False)), "; ".join(conversation_errors))
    conv_events = conv.get("events", [])
    if not isinstance(conv_events, list):
        conv_events = []
    normalized_conv_events = [item for item in conv_events if isinstance(item, dict)]
    normalized_conv_events = sorted(normalized_conv_events, key=_conversation_event_sort_key)
    recent_conversations = normalized_conv_events[-20:]
    conv_sources = conv.get("sources", [])
    if not isinstance(conv_sources, list):
        conv_sources = []
    normalized_conv_sources = [item for item in conv_sources if isinstance(item, dict)]
    source_error_count = sum(1 for item in normalized_conv_sources if str(item.get("error", "")).strip())
    source_missing_count = sum(1 for item in normalized_conv_sources if bool(item.get("missing", False)))
    source_recoverable_missing_count = sum(1 for item in normalized_conv_sources if bool(item.get("recoverable_missing", False)))
    source_fallback_count = sum(1 for item in normalized_conv_sources if bool(item.get("fallback_used", False)))
    conversation_total_events = _int_value(conv.get("total_events", len(normalized_conv_events)), len(normalized_conv_events))
    if conversation_total_events < 0:
        conversation_total_events = len(normalized_conv_events)
    conversation_owner_counts = conv.get("owner_counts", {})
    if not isinstance(conversation_owner_counts, dict):
        conversation_owner_counts = {}
    conversation_partial = bool(conv.get("partial", False)) or bool(conversation_errors)
    conversation_ok = bool(conv.get("ok", False)) and not conversation_partial
    task_done_24h = _task_done_events_last_24h(normalized_conv_sources)
    _mark_source("completed_last_24h", bool(task_done_24h.get("ok", False)), _error_text(task_done_24h.get("errors", [])))
    progress["completed_last_24h"] = _int_value(task_done_24h.get("completed_events", 0), 0)
    progress["completed_last_24h_unique_tasks"] = _int_value(task_done_24h.get("unique_task_count", 0), 0)
    progress["completed_last_24h_by_owner"] = (
        dict(task_done_24h.get("by_owner", {})) if isinstance(task_done_24h.get("by_owner", {}), dict) else {}
    )
    progress["completed_last_24h_window_start"] = str(task_done_24h.get("window_start", ""))
    progress["completed_last_24h_window_end"] = str(task_done_24h.get("window_end", ""))
    progress["completed_last_24h_sources_scanned"] = _int_value(task_done_24h.get("files_scanned", 0), 0)
    progress["completed_last_24h_errors"] = list(task_done_24h.get("errors", [])) if isinstance(
        task_done_24h.get("errors", []), list
    ) else []
    lane_items = lanes.get("lanes", []) if isinstance(lanes.get("lanes", []), list) else []
    try:
        response_metrics = _response_metrics_snapshot(config, lane_items)
    except Exception as err:
        response_metrics = _empty_response_metrics(str(err))
    _mark_source("response_metrics", bool(response_metrics.get("ok", False)), _error_text(response_metrics.get("errors", [])))
    lanes = _augment_lane_payload_with_conversation_rollup(lanes, conv)
    runtime_lane_items = lanes.get("lanes", []) if isinstance(lanes.get("lanes", []), list) else []
    runtime_lane_items = [item for item in runtime_lane_items if isinstance(item, dict)]
    lane_operational_states = {"ok", "paused", "idle"}
    operational_lanes = [
        lane
        for lane in runtime_lane_items
        if str(lane.get("health", "")).strip().lower() in lane_operational_states
    ]
    degraded_lanes = [
        lane
        for lane in runtime_lane_items
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
    lane_running_count = max(0, _int_value(lanes.get("running_count", 0), 0))
    lane_scaling_counts = lanes.get("scaling_event_counts", {}) if isinstance(lanes.get("scaling_event_counts", {}), dict) else {}
    parallel_capacity = lanes.get("parallel_capacity", {}) if isinstance(lanes.get("parallel_capacity", {}), dict) else {}
    local_model_fleet = lanes.get("local_model_fleet", {}) if isinstance(lanes.get("local_model_fleet", {}), dict) else {}
    parallel_groups = parallel_capacity.get("groups", []) if isinstance(parallel_capacity.get("groups", []), list) else []
    normalized_parallel_groups = [item for item in parallel_groups if isinstance(item, dict)]
    parallel_groups_at_limit = sum(1 for item in normalized_parallel_groups if bool(item.get("at_limit", False)))
    parallel_groups_over_limit = sum(1 for item in normalized_parallel_groups if bool(item.get("over_limit", False)))

    snapshot = {
        "timestamp": _now_iso(),
        "status": status,
        "runtime": {
            "primary_runner_running": bool(status.get("runner_running", False)),
            "lane_agents_running": lane_running_count > 0,
            "effective_agents_running": bool(status.get("runner_running", False)) or lane_running_count > 0,
            "lane_operational_count": len(operational_lanes),
            "lane_degraded_count": len(degraded_lanes),
            "lane_health_counts": lanes.get("health_counts", {}),
            "lane_owner_health": lanes.get("owner_counts", {}),
            "lane_scale_up_events": _int_value(lane_scaling_counts.get("scale_up", 0), 0),
            "lane_scale_down_events": _int_value(lane_scaling_counts.get("scale_down", 0), 0),
            "lane_scale_hold_events": _int_value(lane_scaling_counts.get("scale_hold", 0), 0),
            "parallel_capacity_group_count": len(normalized_parallel_groups),
            "parallel_capacity_groups_at_limit": parallel_groups_at_limit,
            "parallel_capacity_groups_over_limit": parallel_groups_over_limit,
            "local_model_fleet_ok": bool(local_model_fleet.get("ok", False)),
            "local_model_endpoint_total": _int_value(
                local_model_fleet.get("summary", {}).get("endpoint_total", 0)
                if isinstance(local_model_fleet.get("summary", {}), dict)
                else 0,
                0,
            ),
            "local_model_endpoint_healthy": _int_value(
                local_model_fleet.get("summary", {}).get("endpoint_healthy", 0)
                if isinstance(local_model_fleet.get("summary", {}), dict)
                else 0,
                0,
            ),
        },
        "progress": progress,
        "lanes": lanes,
        "parallel_capacity": parallel_capacity,
        "local_model_fleet": local_model_fleet,
        "response_metrics": response_metrics,
        "conversations": {
            "ok": conversation_ok,
            "total_events": conversation_total_events,
            "owner_counts": conversation_owner_counts,
            "latest": (recent_conversations[-1] if recent_conversations else {}),
            "recent_events": recent_conversations,
            "partial": conversation_partial,
            "errors": conversation_errors,
            "sources": normalized_conv_sources,
            "source_error_count": source_error_count,
            "source_missing_count": source_missing_count,
            "source_recoverable_missing_count": source_recoverable_missing_count,
            "source_fallback_count": source_fallback_count,
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
    scaling_counts = lanes.get("scaling_event_counts", {}) if isinstance(lanes.get("scaling_event_counts", {}), dict) else {}
    parallel_capacity = snapshot.get("parallel_capacity", {}) if isinstance(snapshot.get("parallel_capacity", {}), dict) else {}
    parallel_groups = parallel_capacity.get("groups", []) if isinstance(parallel_capacity.get("groups", []), list) else []
    normalized_parallel_groups = [item for item in parallel_groups if isinstance(item, dict)]
    parallel_parts: list[str] = []
    for item in normalized_parallel_groups:
        parallel_parts.append(
            f"{item.get('provider', 'unknown')}:{item.get('model', 'default')}"
            f"={item.get('running_count', 0)}/{item.get('effective_limit', 1)}"
            f"{'!' if bool(item.get('over_limit', False)) else ''}"
        )
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
        f"[{_format_local_timestamp(snapshot.get('timestamp', ''))}] supervisor={status.get('supervisor_running', False)} "
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
            f"scaling_events: up={_int_value(scaling_counts.get('scale_up', 0), 0)} "
            f"down={_int_value(scaling_counts.get('scale_down', 0), 0)} "
            f"hold={_int_value(scaling_counts.get('scale_hold', 0), 0)}"
        ),
        f"parallel_capacity: {' | '.join(parallel_parts) if parallel_parts else 'none'}",
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
            f"cost_total=${response_metrics.get('cost_usd_total', 0.0)} "
            f"routed={response_metrics.get('routing_routellm_count', 0)}/"
            f"{response_metrics.get('routing_decisions_total', 0)} "
            f"fallback={response_metrics.get('routing_fallback_count', 0)}"
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


def _process_watchdog_paths(config: ManagerConfig, env: dict[str, str]) -> tuple[Path, Path]:
    state_file = _path_from_env(
        env,
        "ORXAQ_AUTONOMY_PROCESS_WATCHDOG_STATE_FILE",
        config.artifacts_dir / "process_watchdog_state.json",
    )
    history_file = _path_from_env(
        env,
        "ORXAQ_AUTONOMY_PROCESS_WATCHDOG_HISTORY_FILE",
        config.artifacts_dir / "process_watchdog_history.ndjson",
    )
    return state_file, history_file


def _watchdog_bucket(state: dict[str, Any], process_id: str) -> dict[str, Any]:
    processes = state.get("processes", {})
    if not isinstance(processes, dict):
        processes = {}
        state["processes"] = processes
    bucket = processes.get(process_id, {})
    if not isinstance(bucket, dict):
        bucket = {}
    bucket["checks_total"] = _int_value(bucket.get("checks_total", 0), 0)
    bucket["restart_successes"] = _int_value(bucket.get("restart_successes", 0), 0)
    bucket["restart_failures"] = _int_value(bucket.get("restart_failures", 0), 0)
    bucket.setdefault("last_status", "")
    bucket.setdefault("last_detail", "")
    bucket.setdefault("last_pid", None)
    bucket.setdefault("last_checked_at", "")
    bucket.setdefault("last_restart_at", "")
    processes[process_id] = bucket
    return bucket


def _state_all_tasks_done(path: Path) -> bool:
    payload = _read_json_file(path)
    if not payload:
        return False
    statuses: list[str] = []
    for item in payload.values():
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).strip().lower()
        if not status:
            continue
        statuses.append(status)
    return bool(statuses) and all(status == "done" for status in statuses)


def _watchdog_restart_allowed(bucket: dict[str, Any], *, now_utc: dt.datetime, cooldown_sec: int) -> bool:
    last_restart = _parse_iso_timestamp(bucket.get("last_restart_at", ""))
    if last_restart is None:
        return True
    return (now_utc - last_restart).total_seconds() >= max(0, cooldown_sec)


def _record_watchdog_outcome(
    *,
    bucket: dict[str, Any],
    process_id: str,
    pid: int | None,
    status: str,
    detail: str,
    now_iso: str,
) -> dict[str, Any]:
    bucket["checks_total"] = _int_value(bucket.get("checks_total", 0), 0) + 1
    bucket["last_status"] = status
    bucket["last_detail"] = detail
    bucket["last_pid"] = pid
    bucket["last_checked_at"] = now_iso
    return {
        "id": process_id,
        "pid": pid,
        "status": status,
        "detail": detail,
    }


def _wait_for_running_pid(path: Path, *, grace_sec: int) -> int | None:
    deadline = time.monotonic() + max(0, grace_sec)
    while True:
        pid = _read_pid(path)
        if _pid_running(pid):
            return pid
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def _restart_supervisor_for_watchdog(config: ManagerConfig) -> None:
    supervisor_pid = _read_pid(config.supervisor_pid_file)
    if _pid_running(supervisor_pid):
        _terminate_pid(supervisor_pid)
    config.supervisor_pid_file.unlink(missing_ok=True)
    config.runner_pid_file.unlink(missing_ok=True)
    start_background(config)


def process_watchdog_pass(config: ManagerConfig) -> dict[str, Any]:
    env = _manager_env(config)
    state_file, history_file = _process_watchdog_paths(config, env)
    restart_cooldown_sec = _int_from_env(env, "ORXAQ_AUTONOMY_PROCESS_RESTART_COOLDOWN_SEC", 30, min_value=0)
    startup_grace_sec = _int_from_env(env, "ORXAQ_AUTONOMY_PROCESS_STARTUP_GRACE_SEC", 8, min_value=0)
    now_utc = _now_utc()
    now_iso = now_utc.isoformat()
    heartbeat_age = _heartbeat_age_sec(config)

    state = _read_json_file(state_file)
    state["checks_total"] = _int_value(state.get("checks_total", 0), 0) + 1
    if not isinstance(state.get("processes"), dict):
        state["processes"] = {}

    results: list[dict[str, Any]] = []

    supervisor_bucket = _watchdog_bucket(state, "supervisor")
    supervisor_pid = _read_pid(config.supervisor_pid_file)
    supervisor_running = _pid_running(supervisor_pid)
    supervisor_status = "healthy"
    supervisor_detail = "supervisor running"
    supervisor_restarted_this_pass = False

    if not supervisor_running:
        if not _watchdog_restart_allowed(
            supervisor_bucket,
            now_utc=now_utc,
            cooldown_sec=restart_cooldown_sec,
        ):
            supervisor_status = "down_cooldown"
            supervisor_detail = "supervisor restart skipped due to cooldown"
        else:
            supervisor_bucket["last_restart_at"] = now_iso
            try:
                spawned_supervisor_pid = start_background(config)
            except Exception as err:
                supervisor_status = "restart_failed"
                supervisor_detail = f"supervisor start failed: {err}"
                supervisor_bucket["restart_failures"] = _int_value(supervisor_bucket.get("restart_failures", 0), 0) + 1
            else:
                refreshed_supervisor_pid = _wait_for_running_pid(
                    config.supervisor_pid_file,
                    grace_sec=startup_grace_sec,
                )
                if refreshed_supervisor_pid is not None:
                    supervisor_pid = refreshed_supervisor_pid
                    supervisor_running = True
                    supervisor_status = "restarted"
                    supervisor_detail = f"supervisor restarted pid={refreshed_supervisor_pid}"
                    supervisor_bucket["restart_successes"] = _int_value(
                        supervisor_bucket.get("restart_successes", 0),
                        0,
                    ) + 1
                    supervisor_restarted_this_pass = True
                elif _pid_running(spawned_supervisor_pid):
                    supervisor_pid = spawned_supervisor_pid
                    supervisor_running = True
                    supervisor_status = "restarted"
                    supervisor_detail = (
                        f"supervisor started pid={spawned_supervisor_pid}; waiting for pid file"
                    )
                    supervisor_bucket["restart_successes"] = _int_value(
                        supervisor_bucket.get("restart_successes", 0),
                        0,
                    ) + 1
                    supervisor_restarted_this_pass = True
                else:
                    supervisor_status = "restart_failed"
                    supervisor_detail = (
                        f"supervisor did not restart within {startup_grace_sec}s"
                    )
                    supervisor_bucket["restart_failures"] = _int_value(
                        supervisor_bucket.get("restart_failures", 0),
                        0,
                    ) + 1

    results.append(
        _record_watchdog_outcome(
            bucket=supervisor_bucket,
            process_id="supervisor",
            pid=supervisor_pid,
            status=supervisor_status,
            detail=supervisor_detail,
            now_iso=now_iso,
        )
    )

    runner_bucket = _watchdog_bucket(state, "runner")
    runner_pid = _read_pid(config.runner_pid_file)
    runner_running = _pid_running(runner_pid)
    runner_stale = runner_running and heartbeat_age != -1 and heartbeat_age > config.heartbeat_stale_sec
    heartbeat_payload = _read_json_file(config.heartbeat_file)
    heartbeat_phase = str(heartbeat_payload.get("phase", "")).strip().lower()
    tasks_all_done = _state_all_tasks_done(config.state_file)
    runner_idle_completed = (
        not runner_running
        and (heartbeat_phase in RUNNER_IDLE_HEARTBEAT_PHASES or tasks_all_done)
    )
    runner_status = "healthy"
    runner_detail = "runner running"
    restart_reason = ""

    if not supervisor_running:
        runner_status = "down_no_restart"
        runner_detail = "runner restart unavailable while supervisor is down"
    elif runner_running and not runner_stale:
        if heartbeat_age == -1:
            runner_detail = "runner running; heartbeat not available yet"
        else:
            runner_detail = f"runner running; heartbeat_age_sec={heartbeat_age}"
    elif runner_idle_completed:
        if heartbeat_phase in RUNNER_IDLE_HEARTBEAT_PHASES:
            runner_detail = f"runner idle; last heartbeat phase={heartbeat_phase}"
        else:
            runner_detail = "runner idle; all tasks are marked done"
    else:
        if not _watchdog_restart_allowed(
            runner_bucket,
            now_utc=now_utc,
            cooldown_sec=restart_cooldown_sec,
        ):
            runner_status = "down_cooldown"
            runner_detail = "runner restart skipped due to cooldown"
        elif supervisor_restarted_this_pass and not runner_running and not runner_stale:
            refreshed_runner_pid = _wait_for_running_pid(
                config.runner_pid_file,
                grace_sec=startup_grace_sec,
            )
            if refreshed_runner_pid is not None:
                runner_pid = refreshed_runner_pid
                runner_status = "restarted"
                runner_detail = f"runner restarted pid={runner_pid} (supervisor_restarted)"
                runner_bucket["restart_successes"] = _int_value(
                    runner_bucket.get("restart_successes", 0),
                    0,
                ) + 1
            else:
                runner_status = "down_no_restart"
                runner_detail = (
                    f"runner not yet running after supervisor restart; waited {startup_grace_sec}s"
                )
        else:
            runner_bucket["last_restart_at"] = now_iso
            try:
                if runner_stale and runner_pid:
                    _terminate_pid(runner_pid)
                    restart_reason = "stale_heartbeat"
                else:
                    _restart_supervisor_for_watchdog(config)
                    restart_reason = "runner_missing"
            except Exception as err:
                runner_status = "restart_failed"
                runner_detail = f"runner restart failed: {err}"
                runner_bucket["restart_failures"] = _int_value(runner_bucket.get("restart_failures", 0), 0) + 1
            else:
                refreshed_runner_pid = _wait_for_running_pid(
                    config.runner_pid_file,
                    grace_sec=startup_grace_sec,
                )
                if refreshed_runner_pid is not None:
                    runner_pid = refreshed_runner_pid
                    runner_status = "restarted"
                    runner_detail = f"runner restarted pid={runner_pid} ({restart_reason})"
                    runner_bucket["restart_successes"] = _int_value(
                        runner_bucket.get("restart_successes", 0),
                        0,
                    ) + 1
                else:
                    runner_status = "restart_failed"
                    runner_detail = (
                        f"runner did not restart within {startup_grace_sec}s ({restart_reason})"
                    )
                    runner_bucket["restart_failures"] = _int_value(
                        runner_bucket.get("restart_failures", 0),
                        0,
                    ) + 1

    results.append(
        _record_watchdog_outcome(
            bucket=runner_bucket,
            process_id="runner",
            pid=runner_pid,
            status=runner_status,
            detail=runner_detail,
            now_iso=now_iso,
        )
    )

    state["updated_at"] = now_iso
    state["last_pass_ok"] = all(item.get("status") in PROCESS_WATCHDOG_HEALTHY_STATUSES for item in results)
    _write_json_file(state_file, state)

    for result in results:
        _append_ndjson_record(
            history_file,
            {
                "timestamp": now_iso,
                "heartbeat_age_sec": heartbeat_age,
                "process_id": result.get("id"),
                "pid": result.get("pid"),
                "status": result.get("status"),
                "detail": result.get("detail"),
            },
        )

    return {
        "timestamp": now_iso,
        "heartbeat_age_sec": heartbeat_age,
        "heartbeat_stale_threshold_sec": config.heartbeat_stale_sec,
        "ok": bool(state.get("last_pass_ok", False)),
        "processes": results,
        "state_file": str(state_file),
        "history_file": str(history_file),
        "restart_cooldown_sec": restart_cooldown_sec,
        "startup_grace_sec": startup_grace_sec,
    }


def full_autonomy_snapshot(config: ManagerConfig, *, require_clean: bool = True) -> dict[str, Any]:
    env = _manager_env(config)
    report_file = _path_from_env(
        env,
        "ORXAQ_AUTONOMY_FULL_AUTONOMY_REPORT_FILE",
        config.artifacts_dir / "full_autonomy_report.json",
    )
    preflight_payload = preflight(config, require_clean=require_clean)
    watchdog_payload = process_watchdog_pass(config)
    status = status_snapshot(config)
    health = health_snapshot(config)
    ok = (
        bool(preflight_payload.get("clean", True))
        and bool(watchdog_payload.get("ok", False))
        and bool(status.get("supervisor_running", False))
        and bool(status.get("runner_running", False))
        and not bool(health.get("heartbeat_stale", False))
    )
    payload = {
        "timestamp": _now_iso(),
        "ok": ok,
        "preflight": preflight_payload,
        "watchdog": watchdog_payload,
        "status": status,
        "health": health,
    }
    _write_json_file(report_file, payload)
    payload["full_autonomy_report_file"] = str(report_file)
    return payload


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


def _resolve_lane_path(root: Path, raw: str, default: Path, *, artifacts_dir: Path) -> Path:
    value = raw.strip()
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()

    # Preserve backwards compatibility for lane configs that still reference the
    # default "artifacts/autonomy/..." paths while honoring ORXAQ_AUTONOMY_ARTIFACTS_DIR.
    normalized = value.replace("\\", "/").lstrip("./")
    legacy_prefix = "artifacts/autonomy"
    if normalized == legacy_prefix or normalized.startswith(f"{legacy_prefix}/"):
        suffix = normalized[len(legacy_prefix) :].lstrip("/")
        rebased = artifacts_dir / suffix if suffix else artifacts_dir
        return rebased.resolve()

    return (root / path).resolve()


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
    artifacts_dir = _resolve_lane_path(
        config.root_dir,
        str(item.get("artifacts_dir", "")),
        runtime_dir,
        artifacts_dir=config.artifacts_dir,
    )
    state_file = _resolve_lane_path(
        config.root_dir,
        str(item.get("state_file", "")),
        artifacts_dir / "state.json",
        artifacts_dir=config.artifacts_dir,
    )
    heartbeat_file = _resolve_lane_path(
        config.root_dir,
        str(item.get("heartbeat_file", "")),
        artifacts_dir / "heartbeat.json",
        artifacts_dir=config.artifacts_dir,
    )
    lock_file = _resolve_lane_path(
        config.root_dir,
        str(item.get("lock_file", "")),
        artifacts_dir / "runner.lock",
        artifacts_dir=config.artifacts_dir,
    )
    conversation_log_file = _resolve_lane_path(
        config.root_dir,
        str(item.get("conversation_log_file", "")),
        artifacts_dir / "conversations.ndjson",
        artifacts_dir=config.artifacts_dir,
    )
    metrics_file = _resolve_lane_path(
        config.root_dir,
        str(item.get("metrics_file", "")),
        artifacts_dir / "response_metrics.ndjson",
        artifacts_dir=config.artifacts_dir,
    )
    metrics_summary_file = _resolve_lane_path(
        config.root_dir,
        str(item.get("metrics_summary_file", "")),
        artifacts_dir / "response_metrics_summary.json",
        artifacts_dir=config.artifacts_dir,
    )
    pricing_file = _resolve_path(
        config.root_dir,
        str(item.get("pricing_file", "")),
        config.pricing_file,
    )
    routellm_policy_file = _resolve_path(
        config.root_dir,
        str(item.get("routellm_policy_file", "")),
        config.routellm_policy_file,
    )
    route_timeout_raw = item.get("routellm_timeout_sec", config.routellm_timeout_sec)
    try:
        routellm_timeout_sec = max(1, int(route_timeout_raw))
    except (TypeError, ValueError):
        routellm_timeout_sec = max(1, int(config.routellm_timeout_sec))
    routellm_enabled = bool(item.get("routellm_enabled", config.routellm_enabled))
    routellm_url = str(item.get("routellm_url", config.routellm_url)).strip()
    codex_cmd = str(item.get("codex_cmd", config.codex_cmd)).strip() or config.codex_cmd
    gemini_cmd = str(item.get("gemini_cmd", config.gemini_cmd)).strip() or config.gemini_cmd
    claude_cmd = str(item.get("claude_cmd", config.claude_cmd)).strip() or config.claude_cmd
    codex_model_raw = item.get("codex_model", config.codex_model if config.codex_model is not None else "")
    gemini_model_raw = item.get("gemini_model", config.gemini_model if config.gemini_model is not None else "")
    claude_model_raw = item.get("claude_model", config.claude_model if config.claude_model is not None else "")
    codex_model = str(codex_model_raw).strip() or None
    gemini_model = str(gemini_model_raw).strip() or None
    claude_model = str(claude_model_raw).strip() or None
    lane_env_raw = item.get("env", {})
    lane_env: dict[str, str] = {}
    if isinstance(lane_env_raw, dict):
        for key, value in lane_env_raw.items():
            env_key = str(key).strip()
            if not env_key:
                continue
            lane_env[env_key] = str(value).strip()
    gemini_fallback_raw = item.get("gemini_fallback_models", config.gemini_fallback_models)
    if isinstance(gemini_fallback_raw, list):
        gemini_fallback_models = [str(candidate).strip() for candidate in gemini_fallback_raw if str(candidate).strip()]
    elif gemini_fallback_raw in (None, ""):
        gemini_fallback_models = []
    else:
        gemini_fallback_models = [
            candidate.strip()
            for candidate in re.split(r"[;,]", str(gemini_fallback_raw))
            if candidate.strip()
        ]
    if not gemini_fallback_models:
        gemini_fallback_models = list(config.gemini_fallback_models)
    mcp_raw = str(item.get("mcp_context_file", "")).strip()
    mcp_default = config.mcp_context_file or (config.root_dir / "config" / "mcp_context.example.json")
    if mcp_raw or config.mcp_context_file is not None:
        mcp_context_file: Path | None = _resolve_path(config.root_dir, mcp_raw, mcp_default)
    else:
        mcp_context_file = None
    scaling_mode_raw = str(item.get("scaling_mode", "static")).strip().lower()
    scaling_mode = scaling_mode_raw if scaling_mode_raw in {"static", "npv"} else "static"
    execution_profile = _normalize_execution_profile(
        item.get("execution_profile", config.execution_profile),
        default=config.execution_profile,
    )
    scaling_group = str(item.get("scaling_group", "")).strip()
    scaling_rank_raw = item.get("scaling_rank", 1)
    try:
        scaling_rank = max(1, int(scaling_rank_raw))
    except (TypeError, ValueError):
        scaling_rank = 1
    scaling_decision_file = _resolve_path(
        config.root_dir,
        str(item.get("scaling_decision_file", "")),
        config.scaling_decision_file,
    )

    def _lane_float(name: str, default: float) -> float:
        raw = item.get(name, default)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(default)

    def _lane_int(name: str, default: int) -> int:
        raw = item.get(name, default)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(default)

    def _lane_bool(name: str, default: bool) -> bool:
        raw = item.get(name, default)
        if isinstance(raw, bool):
            return raw
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    scaling_min_marginal_npv_usd = max(
        0.0,
        _lane_float("scaling_min_marginal_npv_usd", config.scaling_min_marginal_npv_usd),
    )
    scaling_daily_budget_usd = max(
        0.0,
        _lane_float("scaling_daily_budget_usd", config.scaling_daily_budget_usd),
    )
    scaling_max_parallel_agents = max(
        1,
        _lane_int("scaling_max_parallel_agents", config.scaling_max_parallel_agents),
    )
    scaling_max_subagents_per_agent = max(
        1,
        _lane_int("scaling_max_subagents_per_agent", config.scaling_max_subagents_per_agent),
    )
    max_cycles = max(1, _lane_int("max_cycles", config.max_cycles))
    max_attempts = max(1, _lane_int("max_attempts", config.max_attempts))
    continuous = _lane_bool("continuous", False)
    continuous_recycle_delay_sec = max(10, _lane_int("continuous_recycle_delay_sec", 90))
    if execution_profile == "extra_high":
        continuous = True
        max_cycles = max(max_cycles, EXTRA_HIGH_MIN_MAX_CYCLES)
        lane_env.setdefault("ORXAQ_TASK_QUEUE_PERSISTENT_MODE", "1")
        lane_env.setdefault("ORXAQ_AUTONOMY_EXECUTION_PROFILE", "extra_high")
        lane_env.setdefault("ORXAQ_AUTONOMY_ASSUME_TRUE_FULL_AUTONOMY", "1")
    isolated_worktree = _lane_bool("isolated_worktree", True)
    worktree_root = _resolve_lane_path(
        config.root_dir,
        str(item.get("worktree_root", "")),
        runtime_dir / "worktrees",
        artifacts_dir=config.artifacts_dir,
    )
    worktree_base_ref = str(item.get("worktree_base_ref", "origin/main")).strip() or "origin/main"

    return {
        "id": lane_id,
        "enabled": bool(item.get("enabled", True)),
        "owner": owner,
        "description": str(item.get("description", "")).strip(),
        "impl_repo": _resolve_path(config.root_dir, str(item.get("impl_repo", "")), config.impl_repo),
        "test_repo": _resolve_path(config.root_dir, str(item.get("test_repo", "")), config.test_repo),
        "tasks_file": _resolve_path(config.root_dir, str(item.get("tasks_file", "")), config.tasks_file),
        "task_queue_file": _resolve_lane_path(
            config.root_dir,
            str(item.get("task_queue_file", "")),
            runtime_dir / "task_queue.ndjson",
            artifacts_dir=config.artifacts_dir,
        ),
        "task_queue_state_file": _resolve_lane_path(
            config.root_dir,
            str(item.get("task_queue_state_file", "")),
            runtime_dir / "task_queue_claimed.json",
            artifacts_dir=config.artifacts_dir,
        ),
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
        "handoff_dir": _resolve_lane_path(
            config.root_dir,
            str(item.get("handoff_dir", "")),
            config.artifacts_dir / "handoffs",
            artifacts_dir=config.artifacts_dir,
        ),
        "artifacts_dir": artifacts_dir,
        "heartbeat_file": heartbeat_file,
        "lock_file": lock_file,
        "conversation_log_file": conversation_log_file,
        "metrics_file": metrics_file,
        "metrics_summary_file": metrics_summary_file,
        "pricing_file": pricing_file,
        "routellm_policy_file": routellm_policy_file,
        "routellm_enabled": routellm_enabled,
        "routellm_url": routellm_url,
        "routellm_timeout_sec": routellm_timeout_sec,
        "codex_cmd": codex_cmd,
        "gemini_cmd": gemini_cmd,
        "claude_cmd": claude_cmd,
        "codex_model": codex_model,
        "gemini_model": gemini_model,
        "claude_model": claude_model,
        "env": lane_env,
        "gemini_fallback_models": gemini_fallback_models,
        "owner_filter": [owner],
        "validate_commands": [
            str(cmd).strip() for cmd in item.get("validate_commands", config.validate_commands) if str(cmd).strip()
        ],
        "exclusive_paths": [str(path).strip() for path in item.get("exclusive_paths", []) if str(path).strip()],
        "scaling_mode": scaling_mode,
        "execution_profile": execution_profile,
        "scaling_group": scaling_group,
        "scaling_rank": scaling_rank,
        "scaling_decision_file": scaling_decision_file,
        "scaling_min_marginal_npv_usd": scaling_min_marginal_npv_usd,
        "scaling_daily_budget_usd": scaling_daily_budget_usd,
        "scaling_max_parallel_agents": scaling_max_parallel_agents,
        "scaling_max_subagents_per_agent": scaling_max_subagents_per_agent,
        "max_cycles": max_cycles,
        "max_attempts": max_attempts,
        "continuous": continuous,
        "continuous_recycle_delay_sec": continuous_recycle_delay_sec,
        "isolated_worktree": isolated_worktree,
        "worktree_root": worktree_root,
        "worktree_base_ref": worktree_base_ref,
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


def _resolve_requested_lane_id(lanes: list[dict[str, Any]], requested_lane: str) -> str | None:
    lane_filter = requested_lane.strip()
    if not lane_filter:
        return None
    known_ids: list[str] = []
    for lane in lanes:
        lane_id = str(lane.get("id", "")).strip()
        if lane_id:
            known_ids.append(lane_id)
    if lane_filter in known_ids:
        return lane_filter
    folded_matches = sorted({lane_id for lane_id in known_ids if lane_id.lower() == lane_filter.lower()})
    if len(folded_matches) == 1:
        return folded_matches[0]
    if len(folded_matches) > 1:
        joined = ", ".join(repr(item) for item in folded_matches)
        raise RuntimeError(f"Lane id {lane_filter!r} is ambiguous. Matching ids: {joined}.")
    return None


def _lane_load_error_entries(errors: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for raw in errors:
        message = str(raw).strip()
        if not message:
            continue
        lane_label = "lane_config"
        if ":" in message:
            prefix = message.split(":", 1)[0].strip()
            if prefix:
                lane_label = prefix
        owner = "unknown"
        owner_match = re.search(r"unsupported\s+lane\s+owner\s+['\"]([^'\"]+)['\"]", message, flags=re.IGNORECASE)
        if owner_match:
            owner = owner_match.group(1).strip() or "unknown"
        out.append({"id": lane_label, "owner": owner, "error": message, "source": "lane_config"})
    return out


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


def _lane_scaling_event_summary(events_path: Path, *, limit: int = 400) -> dict[str, Any]:
    entries = _tail_ndjson(events_path, max(1, limit))
    counts = {"scale_up": 0, "scale_down": 0, "scale_hold": 0}
    latest_scale_event: dict[str, Any] = {}
    for entry in entries:
        event_type = str(entry.get("event_type", "")).strip().lower()
        if event_type in {"scale_up", "scaled_up"}:
            counts["scale_up"] += 1
            latest_scale_event = entry
        elif event_type in {"scale_down", "scaled_down"}:
            counts["scale_down"] += 1
            latest_scale_event = entry
        elif event_type in {"scale_hold", "scale_blocked"}:
            counts["scale_hold"] += 1
            latest_scale_event = entry
    return {
        "counts": counts,
        "latest_scale_event": latest_scale_event,
    }


def _coerce_bool(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


_PARALLEL_CAPACITY_SIGNAL_TERMS = (
    "rate limit",
    "429",
    "too many requests",
    "resource exhausted",
    "over quota",
    "quota exceeded",
    "provider capacity limit",
    "service capacity",
    "capacity exceeded",
    "temporarily unavailable",
    "service unavailable",
    "throttle",
    "concurrency",
)


def _parallel_owner_model(lane: dict[str, Any]) -> tuple[str, str]:
    owner = str(lane.get("owner", "unknown")).strip().lower() or "unknown"
    model = ""
    if owner == "codex":
        model = str(lane.get("codex_model", "") or "").strip()
    elif owner == "gemini":
        model = str(lane.get("gemini_model", "") or "").strip()
    elif owner == "claude":
        model = str(lane.get("claude_model", "") or "").strip()
    if not model:
        model = "default"
    return owner, model


def _normalize_endpoint_key(raw: str) -> str:
    text = str(raw).strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return ""
    port = parsed.port
    if port is None:
        port = 443 if str(parsed.scheme or "").strip().lower() == "https" else 80
    return f"{host}:{port}"


def _lane_endpoint_key(lane: dict[str, Any]) -> str:
    env = lane.get("env", {}) if isinstance(lane.get("env", {}), dict) else {}
    raw_urls: list[str] = []
    base_urls = str(env.get("ORXAQ_LOCAL_OPENAI_BASE_URLS", "")).strip()
    if base_urls:
        raw_urls.extend(item.strip() for item in re.split(r"[,\s;]+", base_urls) if item.strip())
    base_url = str(env.get("ORXAQ_LOCAL_OPENAI_BASE_URL", "")).strip()
    if base_url:
        raw_urls.append(base_url)
    for raw in raw_urls:
        key = _normalize_endpoint_key(raw)
        if key:
            return key
    return ""


def _parallel_key(owner: str, model: str, endpoint_key: str = "") -> str:
    normalized_owner = owner.strip().lower() or "unknown"
    normalized_model = model.strip() or "default"
    normalized_endpoint = endpoint_key.strip().lower()
    if normalized_endpoint:
        return f"{normalized_owner}::{normalized_model}::{normalized_endpoint}"
    return f"{normalized_owner}::{normalized_model}"


def _split_parallel_key(key: str) -> tuple[str, str, str]:
    raw = key.strip()
    if not raw:
        return "unknown", "default", ""
    parts = [part.strip() for part in raw.split("::")]
    if len(parts) >= 3:
        return parts[0].lower() or "unknown", parts[1] or "default", parts[2].lower()
    if len(parts) == 2:
        return parts[0].lower() or "unknown", parts[1] or "default", ""
    return raw.lower(), "default", ""


def _parallel_identity(lane: dict[str, Any]) -> dict[str, str]:
    owner, model = _parallel_owner_model(lane)
    endpoint_key = _lane_endpoint_key(lane)
    key = _parallel_key(owner, model, endpoint_key)
    return {"owner": owner, "model": model, "endpoint_key": endpoint_key, "key": key}


def _contains_parallel_capacity_signal(text: str) -> str:
    normalized = text.strip().lower()
    if not normalized:
        return ""
    for term in _PARALLEL_CAPACITY_SIGNAL_TERMS:
        if term in normalized:
            return term
    return ""


def _lane_parallel_capacity_signal(lane_status: dict[str, Any]) -> dict[str, Any]:
    lane_id = str(lane_status.get("id", "")).strip()
    candidates: list[str] = []
    latest_log = str(lane_status.get("latest_log_line", "")).strip()
    if latest_log:
        candidates.append(latest_log)
    lane_error = str(lane_status.get("error", "")).strip()
    if lane_error:
        candidates.append(lane_error)
    last_event = lane_status.get("last_event", {})
    if isinstance(last_event, dict) and last_event:
        try:
            candidates.append(json.dumps(last_event, sort_keys=True))
        except Exception:
            candidates.append(str(last_event))
    for candidate in candidates:
        term = _contains_parallel_capacity_signal(candidate)
        if term:
            return {
                "lane_id": lane_id,
                "matched_term": term,
                "sample": candidate[-240:],
            }
    return {}


def _local_model_endpoint_capacity_map(config: ManagerConfig) -> dict[str, dict[str, Any]]:
    status_file = (config.artifacts_dir / "local_models" / "fleet_status.json").resolve()
    payload = _read_json_file(status_file)
    if not payload:
        return {}

    probe_payload = payload.get("probe", {}) if isinstance(payload.get("probe", {}), dict) else {}
    capability_payload = payload.get("capability_scan", {}) if isinstance(payload.get("capability_scan", {}), dict) else {}
    capability_summary = (
        capability_payload.get("summary", {}).get("by_endpoint", {})
        if isinstance(capability_payload.get("summary", {}), dict)
        else {}
    )
    by_endpoint_name = capability_summary if isinstance(capability_summary, dict) else {}
    mapping: dict[str, dict[str, Any]] = {}

    for row in probe_payload.get("endpoints", []):
        if not isinstance(row, dict):
            continue
        base_url = str(row.get("base_url", "")).strip()
        endpoint_key = _normalize_endpoint_key(base_url)
        if not endpoint_key:
            continue
        endpoint_id = str(row.get("id", "")).strip()
        configured_max_parallel = max(1, _int_value(row.get("max_parallel", 1), 1))
        capability_row = by_endpoint_name.get(endpoint_id, {}) if endpoint_id else {}
        recommended_parallel = max(1, _int_value(capability_row.get("recommended_parallel", configured_max_parallel), configured_max_parallel))
        context_tokens = max(0, _int_value(capability_row.get("max_context_tokens_success", 0), 0))
        mapping[endpoint_key] = {
            "endpoint_id": endpoint_id,
            "endpoint_key": endpoint_key,
            "configured_max_parallel": configured_max_parallel,
            "recommended_parallel": recommended_parallel,
            "effective_limit": max(1, min(configured_max_parallel, recommended_parallel)),
            "max_context_tokens_success": context_tokens,
        }
    return mapping


def _apply_local_model_capacity_env(
    config: ManagerConfig,
    lane: dict[str, Any],
    *,
    endpoint_capacity: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    env = dict(lane.get("env", {}) if isinstance(lane.get("env", {}), dict) else {})
    capacity_map = endpoint_capacity if endpoint_capacity is not None else _local_model_endpoint_capacity_map(config)
    if not capacity_map:
        return env

    endpoint_key = _lane_endpoint_key({"env": env})
    endpoint_entry = capacity_map.get(endpoint_key, {}) if endpoint_key else {}
    endpoint_context = max(0, _int_value(endpoint_entry.get("max_context_tokens_success", 0), 0))
    if endpoint_context > 0:
        raw_fraction = str(env.get("ORXAQ_LOCAL_OPENAI_CONTEXT_FRACTION", "0.95")).strip()
        try:
            fraction = float(raw_fraction)
        except (TypeError, ValueError):
            fraction = 0.95
        fraction = min(max(fraction, 0.10), 1.0)
        suggested_tokens = max(64, int(endpoint_context * fraction))
        existing_tokens = _int_value(env.get("ORXAQ_LOCAL_OPENAI_MAX_TOKENS", 0), 0)
        env["ORXAQ_LOCAL_OPENAI_MAX_TOKENS"] = str(max(suggested_tokens, existing_tokens))
        env.setdefault("ORXAQ_LOCAL_OPENAI_DYNAMIC_MAX_TOKENS", "1")
        env.setdefault("ORXAQ_LOCAL_OPENAI_CONTEXT_FRACTION", "0.95")

    overrides: list[str] = []
    for key in sorted(capacity_map):
        context_tokens = max(0, _int_value(capacity_map[key].get("max_context_tokens_success", 0), 0))
        if context_tokens > 0:
            overrides.append(f"{key}={context_tokens}")
    if overrides and not str(env.get("ORXAQ_LOCAL_OPENAI_MAX_TOKENS_BY_ENDPOINT", "")).strip():
        env["ORXAQ_LOCAL_OPENAI_MAX_TOKENS_BY_ENDPOINT"] = ",".join(overrides)

    env.setdefault(
        "ORXAQ_LOCAL_MODEL_FLEET_STATUS_FILE",
        str((config.artifacts_dir / "local_models" / "fleet_status.json").resolve()),
    )
    return env


def _read_parallel_capacity_state(config: ManagerConfig) -> dict[str, Any]:
    raw = _read_json_file(config.parallel_capacity_state_file)
    keys = raw.get("keys", {}) if isinstance(raw.get("keys", {}), dict) else {}
    return {"timestamp": str(raw.get("timestamp", "")).strip(), "keys": keys}


def _write_parallel_capacity_state(config: ManagerConfig, payload: dict[str, Any]) -> None:
    _write_json_file(config.parallel_capacity_state_file, payload)


def _append_parallel_capacity_event(config: ManagerConfig, payload: dict[str, Any]) -> None:
    path = config.parallel_capacity_log_file
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": _now_iso(),
        "event_type": "parallel_capacity_plan",
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _parallel_capacity_plan(
    config: ManagerConfig,
    lanes: list[dict[str, Any]],
    *,
    scaling_plan: dict[str, Any],
    status_by_id: dict[str, dict[str, Any]],
    operation: str,
    requested_lane: str,
) -> dict[str, Any]:
    now_iso = _now_iso()
    endpoint_capacity = _local_model_endpoint_capacity_map(config)
    scaling_by_lane = scaling_plan.get("by_lane", {}) if isinstance(scaling_plan.get("by_lane", {}), dict) else {}
    lane_order = {
        str(item.get("id", "")).strip(): idx
        for idx, item in enumerate(lanes)
        if str(item.get("id", "")).strip()
    }

    groups: dict[str, dict[str, Any]] = {}
    lane_details: dict[str, dict[str, Any]] = {}
    for lane in lanes:
        lane_id = str(lane.get("id", "")).strip()
        if not lane_id:
            continue
        identity = _parallel_identity(lane)
        key = identity["key"]
        lane_scaling = scaling_by_lane.get(lane_id, {})
        scaling_allowed = bool(lane_scaling.get("allowed", True))
        enabled = bool(lane.get("enabled", False))
        lane_status = status_by_id.get(lane_id, {})
        running = bool(lane_status.get("running", False))
        signal = _lane_parallel_capacity_signal(lane_status)

        group = groups.setdefault(
            key,
            {
                "provider": identity["owner"],
                "model": identity["model"],
                "endpoint_key": identity.get("endpoint_key", ""),
                "enabled_lane_count": 0,
                "scaling_eligible_lane_count": 0,
                "running_count": 0,
                "running_lane_ids": [],
                "signal_count": 0,
                "signals": [],
                "lane_ids": [],
            },
        )
        if enabled:
            group["enabled_lane_count"] += 1
            if scaling_allowed:
                group["scaling_eligible_lane_count"] += 1
        if running:
            group["running_count"] += 1
            group["running_lane_ids"].append(lane_id)
        if signal:
            group["signal_count"] += 1
            group["signals"].append(signal)
        group["lane_ids"].append(lane_id)

        lane_details[lane_id] = {
            "provider": identity["owner"],
            "model": identity["model"],
            "endpoint_key": identity.get("endpoint_key", ""),
            "parallel_key": key,
            "enabled": enabled,
            "scaling_allowed": scaling_allowed,
            "running": running,
            "priority": (
                _int_value(lane_scaling.get("slot", 1), 1),
                _int_value(lane_scaling.get("rank", lane.get("scaling_rank", 1)), 1),
                _int_value(lane_order.get(lane_id, 0), 0),
                lane_id,
            ),
        }

    state_payload = _read_parallel_capacity_state(config)
    previous_keys = state_payload.get("keys", {}) if isinstance(state_payload.get("keys", {}), dict) else {}
    next_keys: dict[str, dict[str, Any]] = {}
    key_summaries: dict[str, dict[str, Any]] = {}
    carry_forward_keys = {
        key
        for key, row in previous_keys.items()
        if isinstance(row, dict) and _int_value(row.get("running_count", 0), 0) > 0
    }
    all_keys = sorted(set(groups) | carry_forward_keys)

    for key in all_keys:
        group = groups.get(key, {})
        previous = previous_keys.get(key, {})
        owner, model, endpoint_key = _split_parallel_key(key)
        enabled_count = _int_value(group.get("enabled_lane_count", previous.get("enabled_lane_count", 0)), 0)
        eligible_count = _int_value(group.get("scaling_eligible_lane_count", previous.get("scaling_eligible_lane_count", 0)), 0)
        running_count = _int_value(group.get("running_count", previous.get("running_count", 0)), 0)
        signal_count = _int_value(group.get("signal_count", 0), 0)
        signals = group.get("signals", [])
        configured_limit = max(1, min(config.parallel_capacity_max_limit, eligible_count if eligible_count > 0 else max(1, enabled_count)))
        endpoint_limit = 0
        endpoint_limit_source = ""
        if endpoint_key:
            endpoint_entry = endpoint_capacity.get(endpoint_key, {})
            if endpoint_entry:
                endpoint_limit = max(1, _int_value(endpoint_entry.get("effective_limit", configured_limit), configured_limit))
                endpoint_limit_source = str(endpoint_entry.get("endpoint_id", "")).strip() or endpoint_key
        if endpoint_limit > 0:
            configured_limit = max(1, min(configured_limit, endpoint_limit))
        initial_limit = max(1, min(configured_limit, config.parallel_capacity_default_limit))
        current_limit = max(1, _int_value(previous.get("limit", initial_limit), initial_limit))
        current_limit = min(current_limit, configured_limit)
        stable_cycles = max(0, _int_value(previous.get("stable_cycles", 0), 0))
        signal_total = max(0, _int_value(previous.get("signal_total", 0), 0))
        decision = "hold"
        decision_reason = "stable"
        last_signal_at = str(previous.get("last_signal_at", "")).strip()
        last_recovery_at = str(previous.get("last_recovery_at", "")).strip()
        seen_capacity_signal = signal_total > 0 or bool(last_signal_at)

        if signal_count > 0:
            target_limit = max(1, running_count - 1) if running_count > 1 else 1
            target_limit = max(1, min(current_limit, target_limit))
            if target_limit < current_limit:
                decision = "decrease"
                decision_reason = "capacity_signal"
            else:
                decision_reason = "capacity_signal_hold"
            current_limit = target_limit
            stable_cycles = 0
            signal_total += signal_count
            last_signal_at = now_iso
            seen_capacity_signal = True
        elif running_count > current_limit and not seen_capacity_signal and configured_limit > current_limit:
            # Avoid permanent under-capacity when the manager restarts into an already
            # healthy lane set with no observed provider saturation signals.
            current_limit = configured_limit
            stable_cycles = 0
            decision = "increase"
            decision_reason = "bootstrap_configured_limit"
            last_recovery_at = now_iso
        elif running_count <= current_limit:
            stable_cycles += 1
            if stable_cycles >= config.parallel_capacity_recovery_cycles and current_limit < configured_limit:
                current_limit += 1
                stable_cycles = 0
                decision = "increase"
                decision_reason = "recovery_window"
                last_recovery_at = now_iso
        else:
            stable_cycles = 0
            decision_reason = "running_above_limit"

        effective_limit = max(1, min(current_limit, configured_limit))
        next_keys[key] = {
            "provider": owner,
            "model": model,
            "endpoint_key": endpoint_key,
            "limit": effective_limit,
            "configured_limit": configured_limit,
            "enabled_lane_count": enabled_count,
            "scaling_eligible_lane_count": eligible_count,
            "running_count": running_count,
            "signal_total": signal_total,
            "signal_count_last_cycle": signal_count,
            "stable_cycles": stable_cycles,
            "last_signal_at": last_signal_at,
            "last_recovery_at": last_recovery_at,
            "endpoint_limit": endpoint_limit,
            "endpoint_limit_source": endpoint_limit_source,
            "updated_at": now_iso,
        }
        key_summaries[key] = {
            "provider": owner,
            "model": model,
            "endpoint_key": endpoint_key,
            "parallel_key": key,
            "effective_limit": effective_limit,
            "configured_limit": configured_limit,
            "enabled_lane_count": enabled_count,
            "scaling_eligible_lane_count": eligible_count,
            "running_count": running_count,
            "overflow_count": max(0, running_count - effective_limit),
            "signal_count": signal_count,
            "signal_total": signal_total,
            "signals": signals,
            "decision": decision,
            "decision_reason": decision_reason,
            "last_signal_at": last_signal_at,
            "last_recovery_at": last_recovery_at,
            "endpoint_limit": endpoint_limit,
            "endpoint_limit_source": endpoint_limit_source,
        }

    _write_parallel_capacity_state(
        config,
        {
            "timestamp": now_iso,
            "keys": next_keys,
        },
    )
    _append_parallel_capacity_event(
        config,
        {
            "operation": operation,
            "requested_lane": requested_lane or "all",
            "state_file": str(config.parallel_capacity_state_file),
            "groups": [key_summaries[key] for key in sorted(key_summaries)],
        },
    )

    for lane_id, detail in lane_details.items():
        key = detail["parallel_key"]
        group = key_summaries.get(key, {})
        detail["effective_limit"] = _int_value(group.get("effective_limit", config.parallel_capacity_default_limit), config.parallel_capacity_default_limit)
        detail["running_count"] = _int_value(group.get("running_count", 0), 0)

    return {
        "timestamp": now_iso,
        "operation": operation,
        "requested_lane": requested_lane or "all",
        "state_file": str(config.parallel_capacity_state_file),
        "log_file": str(config.parallel_capacity_log_file),
        "groups": {key: key_summaries[key] for key in sorted(key_summaries)},
        "by_lane": lane_details,
    }


def _parallel_capacity_snapshot(config: ManagerConfig, lane_rows: list[dict[str, Any]]) -> dict[str, Any]:
    state_payload = _read_parallel_capacity_state(config)
    stored = state_payload.get("keys", {}) if isinstance(state_payload.get("keys", {}), dict) else {}
    aggregates: dict[str, dict[str, Any]] = {}
    for lane in lane_rows:
        if not isinstance(lane, dict):
            continue
        identity = _parallel_identity(lane)
        key = identity["key"]
        entry = aggregates.setdefault(
            key,
            {
                "provider": identity["owner"],
                "model": identity["model"],
                "endpoint_key": identity.get("endpoint_key", ""),
                "parallel_key": key,
                "lane_count": 0,
                "running_count": 0,
            },
        )
        entry["lane_count"] += 1
        if bool(lane.get("running", False)):
            entry["running_count"] += 1
    for key, stored_entry in stored.items():
        provider, model, endpoint_key = _split_parallel_key(key)
        entry = aggregates.setdefault(
            key,
            {
                "provider": provider,
                "model": model,
                "endpoint_key": endpoint_key,
                "parallel_key": key,
                "lane_count": 0,
                "running_count": 0,
            },
        )
        configured_limit = max(1, _int_value(stored_entry.get("configured_limit", entry["lane_count"] or config.parallel_capacity_default_limit), 1))
        effective_limit = max(1, _int_value(stored_entry.get("limit", min(config.parallel_capacity_default_limit, configured_limit)), 1))
        entry["configured_limit"] = configured_limit
        entry["effective_limit"] = min(configured_limit, effective_limit)
        entry["signal_total"] = max(0, _int_value(stored_entry.get("signal_total", 0), 0))
        entry["last_signal_at"] = str(stored_entry.get("last_signal_at", "")).strip()
        entry["last_recovery_at"] = str(stored_entry.get("last_recovery_at", "")).strip()
    for key, entry in aggregates.items():
        if "configured_limit" not in entry:
            configured_limit = max(1, entry["lane_count"])
            entry["configured_limit"] = configured_limit
            entry["effective_limit"] = max(1, min(configured_limit, config.parallel_capacity_default_limit))
            entry["signal_total"] = 0
            entry["last_signal_at"] = ""
            entry["last_recovery_at"] = ""
        entry["at_limit"] = entry["running_count"] >= entry["effective_limit"]
        entry["over_limit"] = entry["running_count"] > entry["effective_limit"]
    groups = [aggregates[key] for key in sorted(aggregates)]
    return {
        "timestamp": _now_iso(),
        "state_file": str(config.parallel_capacity_state_file),
        "log_file": str(config.parallel_capacity_log_file),
        "groups": groups,
        "by_key": {item["parallel_key"]: item for item in groups},
        "ok": True,
    }


def _evaluate_lane_scaling_plan(
    config: ManagerConfig,
    lanes: list[dict[str, Any]],
    *,
    requested_lane: str | None = None,
) -> dict[str, Any]:
    by_lane: dict[str, dict[str, Any]] = {}
    group_payloads: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    npv_groups: dict[str, list[dict[str, Any]]] = {}

    for lane in lanes:
        lane_id = str(lane.get("id", "")).strip()
        if not lane_id:
            continue
        lane_mode = str(lane.get("scaling_mode", "static")).strip().lower()
        if lane_mode not in {"static", "npv"}:
            lane_mode = "static"
        lane_group = str(lane.get("scaling_group", "")).strip() or f"{lane.get('owner', 'unknown')}:default"
        lane_rank = max(1, _int_value(lane.get("scaling_rank", 1), 1))
        by_lane[lane_id] = {
            "lane_id": lane_id,
            "mode": lane_mode,
            "group": lane_group,
            "rank": lane_rank,
            "slot": 1,
            "allowed": True,
            "allowed_parallel_lanes": 1,
            "reason": "static",
            "reasons": [],
            "decision": {},
        }
        if lane_mode != "npv":
            continue
        if not bool(lane.get("enabled", False)) and lane_id != (requested_lane or ""):
            continue
        npv_groups.setdefault(lane_group, []).append(lane)

    for group_id, group_lanes in npv_groups.items():
        ordered = sorted(
            group_lanes,
            key=lambda item: (
                max(1, _int_value(item.get("scaling_rank", 1), 1)),
                str(item.get("id", "")).strip(),
            ),
        )
        if not ordered:
            continue
        decision_paths = sorted(
            {
                str(Path(item.get("scaling_decision_file", config.scaling_decision_file)).resolve())
                for item in ordered
            }
        )
        decision_file = Path(decision_paths[0])
        if len(decision_paths) > 1:
            errors.append(
                f"scaling_group={group_id!r} has conflicting decision files; using {decision_file}."
            )
        min_npv_threshold = max(
            0.0,
            max(_float_value(item.get("scaling_min_marginal_npv_usd", 0.0), 0.0) for item in ordered),
        )
        budget_caps = [
            max(0.0, _float_value(item.get("scaling_daily_budget_usd", 0.0), 0.0))
            for item in ordered
            if _float_value(item.get("scaling_daily_budget_usd", 0.0), 0.0) > 0.0
        ]
        configured_budget_cap = min(budget_caps) if budget_caps else 0.0
        configured_max_parallel = min(
            max(1, _int_value(item.get("scaling_max_parallel_agents", 1), 1))
            for item in ordered
        )
        configured_max_subagents = min(
            max(1, _int_value(item.get("scaling_max_subagents_per_agent", 1), 1))
            for item in ordered
        )
        decision_payload = {
            "group": group_id,
            "decision_file": str(decision_file),
            "marginal_npv_usd": 0.0,
            "requested_parallel_agents": 1,
            "requested_subagents_per_agent": 1,
            "projected_daily_spend_usd": 0.0,
            "configured_min_marginal_npv_usd": min_npv_threshold,
            "configured_daily_budget_usd": configured_budget_cap,
            "configured_max_parallel_agents": configured_max_parallel,
            "configured_max_subagents_per_agent": configured_max_subagents,
            "constraint_daily_budget_usd": 0.0,
            "constraint_max_parallel_agents": configured_max_parallel,
            "constraint_max_subagents_per_agent": configured_max_subagents,
            "decision": "hold",
        }
        reasons: list[str] = []

        if not bool(config.scaling_enabled):
            reasons.append("scaling_disabled")
        else:
            raw_decision = _read_json_file(decision_file) if decision_file.exists() else {}
            if not decision_file.exists():
                reasons.append("decision_file_missing")
            elif not isinstance(raw_decision, dict) or not raw_decision:
                reasons.append("decision_file_invalid")
            else:
                decision_name = str(raw_decision.get("decision", "")).strip().lower() or "hold"
                should_scale = _coerce_bool(raw_decision.get("should_scale"), decision_name == "scale_up")
                raw_reasons = [
                    str(item).strip()
                    for item in raw_decision.get("reasons", [])
                    if str(item).strip()
                ] if isinstance(raw_decision.get("reasons", []), list) else []
                capacity = raw_decision.get("capacity", {})
                if not isinstance(capacity, dict):
                    capacity = {}
                constraints = raw_decision.get("constraints", {})
                if not isinstance(constraints, dict):
                    constraints = {}
                requested_parallel = max(
                    1,
                    _int_value(
                        capacity.get("requested_parallel_agents", raw_decision.get("requested_parallel_agents", 1)),
                        1,
                    ),
                )
                requested_subagents = max(
                    1,
                    _int_value(
                        capacity.get(
                            "requested_subagents_per_agent",
                            raw_decision.get("requested_subagents_per_agent", 1),
                        ),
                        1,
                    ),
                )
                marginal_npv_usd = _float_value(raw_decision.get("marginal_npv_usd", 0.0), 0.0)
                projected_daily_spend_usd = max(
                    0.0,
                    _float_value(
                        constraints.get(
                            "projected_daily_spend_usd",
                            raw_decision.get("projected_daily_spend_usd", 0.0),
                        ),
                        0.0,
                    ),
                )
                constraint_daily_budget = max(
                    0.0,
                    _float_value(
                        constraints.get("daily_budget_usd", raw_decision.get("daily_budget_usd", 0.0)),
                        0.0,
                    ),
                )
                constraint_max_parallel = max(
                    1,
                    _int_value(
                        constraints.get("max_parallel_agents", raw_decision.get("max_parallel_agents", configured_max_parallel)),
                        configured_max_parallel,
                    ),
                )
                constraint_max_subagents = max(
                    1,
                    _int_value(
                        constraints.get(
                            "max_subagents_per_agent",
                            raw_decision.get("max_subagents_per_agent", configured_max_subagents),
                        ),
                        configured_max_subagents,
                    ),
                )
                decision_payload.update(
                    {
                        "decision": decision_name,
                        "marginal_npv_usd": marginal_npv_usd,
                        "requested_parallel_agents": requested_parallel,
                        "requested_subagents_per_agent": requested_subagents,
                        "projected_daily_spend_usd": projected_daily_spend_usd,
                        "constraint_daily_budget_usd": constraint_daily_budget,
                        "constraint_max_parallel_agents": constraint_max_parallel,
                        "constraint_max_subagents_per_agent": constraint_max_subagents,
                    }
                )

                effective_budget_cap = configured_budget_cap if configured_budget_cap > 0.0 else constraint_daily_budget
                effective_max_parallel = min(configured_max_parallel, constraint_max_parallel, len(ordered))
                effective_max_subagents = min(configured_max_subagents, constraint_max_subagents)

                if not should_scale:
                    reasons.append("decision_not_approved")
                if marginal_npv_usd <= min_npv_threshold:
                    reasons.append("marginal_npv_below_threshold")
                if requested_parallel > effective_max_parallel:
                    reasons.append("max_parallel_agents_exceeded")
                if requested_subagents > effective_max_subagents:
                    reasons.append("max_subagents_per_agent_exceeded")
                if effective_budget_cap > 0.0 and projected_daily_spend_usd > effective_budget_cap:
                    reasons.append("daily_budget_exceeded")
                if _coerce_bool(raw_decision.get("stop_loss_triggered"), False):
                    reasons.append("stop_loss_triggered")
                for unhealthy_reason in ("router_unhealthy", "reliability_unhealthy", "quality_unhealthy"):
                    if unhealthy_reason in raw_reasons and unhealthy_reason not in reasons:
                        reasons.append(unhealthy_reason)

                if not reasons:
                    decision_payload["allowed_parallel_lanes"] = max(1, min(requested_parallel, effective_max_parallel))
                else:
                    decision_payload["allowed_parallel_lanes"] = 1

        allowed_parallel_lanes = max(1, _int_value(decision_payload.get("allowed_parallel_lanes", 1), 1))
        reason = reasons[0] if reasons else "approved"
        for slot, lane in enumerate(ordered, start=1):
            lane_id = str(lane.get("id", "")).strip()
            if not lane_id:
                continue
            allowed = slot <= allowed_parallel_lanes
            lane_plan = by_lane.get(lane_id, {})
            lane_plan.update(
                {
                    "mode": "npv",
                    "group": group_id,
                    "rank": max(1, _int_value(lane.get("scaling_rank", 1), 1)),
                    "slot": slot,
                    "allowed": allowed,
                    "allowed_parallel_lanes": allowed_parallel_lanes,
                    "reason": reason if not allowed else ("approved" if allowed_parallel_lanes > 1 else reason),
                    "reasons": list(reasons),
                    "decision": dict(decision_payload),
                }
            )
            by_lane[lane_id] = lane_plan
        group_payloads[group_id] = {
            "group": group_id,
            "mode": "npv",
            "decision_file": str(decision_file),
            "lane_count": len(ordered),
            "allowed_parallel_lanes": allowed_parallel_lanes,
            "reason": reason,
            "reasons": list(reasons),
            "decision": dict(decision_payload),
        }

    return {
        "enabled": bool(config.scaling_enabled),
        "groups": group_payloads,
        "by_lane": by_lane,
        "errors": errors,
        "ok": len(errors) == 0,
    }


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


def _lane_intentionally_scaled_down(last_event: list[dict[str, Any]], *, running: bool) -> bool:
    if running or not last_event:
        return False
    latest = last_event[-1] if isinstance(last_event, list) else {}
    if not isinstance(latest, dict):
        return False
    event_type = str(latest.get("event_type", "")).strip().lower()
    payload = latest.get("payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    reason = str(payload_dict.get("reason", "")).strip().lower()
    if event_type == "scale_down":
        return True
    if event_type == "stopped":
        return reason.startswith("scale_down") or reason == "mesh_scale_down"
    return False


def _lane_health_owner_counts(snapshots: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
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
    return health_counts, owner_counts


def _lane_unavailable_snapshot(
    config: ManagerConfig,
    lane: dict[str, Any],
    *,
    error: str,
    health: str = "error",
    source: str = "lane_runtime",
) -> dict[str, Any]:
    lane_id = str(lane.get("id", "lane")).strip() or "lane"
    runtime_dir = (config.lanes_runtime_dir / lane_id).resolve()
    log_path = _lane_log_file(config, lane_id)
    pid_path = _lane_pid_file(config, lane_id)
    meta_path = _lane_meta_file(config, lane_id)
    events_path = _lane_events_file(config, lane_id)
    pause_path = _lane_pause_file(config, lane_id)
    heartbeat_file = Path(lane.get("heartbeat_file", runtime_dir / "heartbeat.json"))
    scaling_summary = _lane_scaling_event_summary(events_path)
    identity = _parallel_identity(lane)
    return {
        "id": lane_id,
        "enabled": bool(lane.get("enabled", False)),
        "owner": str(lane.get("owner", "unknown")).strip() or "unknown",
        "description": str(lane.get("description", "")).strip(),
        "running": False,
        "pid": None,
        "tasks_file": str(lane.get("tasks_file", "")),
        "task_queue_file": str(lane.get("task_queue_file", "")),
        "task_queue_state_file": str(lane.get("task_queue_state_file", "")),
        "dependency_state_file": str(lane.get("dependency_state_file", "")),
        "handoff_dir": str(lane.get("handoff_dir", "")),
        "objective_file": str(lane.get("objective_file", "")),
        "impl_repo": str(lane.get("impl_repo", "")),
        "test_repo": str(lane.get("test_repo", "")),
        "metrics_file": str(lane.get("metrics_file", "")),
        "metrics_summary_file": str(lane.get("metrics_summary_file", "")),
        "pricing_file": str(lane.get("pricing_file", "")),
        "routellm_policy_file": str(lane.get("routellm_policy_file", "")),
        "routellm_enabled": bool(lane.get("routellm_enabled", False)),
        "routellm_url": str(lane.get("routellm_url", "")),
        "routellm_timeout_sec": _int_value(lane.get("routellm_timeout_sec", 0), 0),
        "codex_cmd": str(lane.get("codex_cmd", "")),
        "gemini_cmd": str(lane.get("gemini_cmd", "")),
        "claude_cmd": str(lane.get("claude_cmd", "")),
        "codex_model": str(lane.get("codex_model", "") or ""),
        "gemini_model": str(lane.get("gemini_model", "") or ""),
        "claude_model": str(lane.get("claude_model", "") or ""),
        "parallel_provider": identity["owner"],
        "parallel_model": identity["model"],
        "parallel_key": identity["key"],
        "gemini_fallback_models": [str(model) for model in lane.get("gemini_fallback_models", []) if str(model)],
        "exclusive_paths": [str(path) for path in lane.get("exclusive_paths", []) if str(path)],
        "execution_profile": _normalize_execution_profile(
            lane.get("execution_profile", config.execution_profile),
            default=config.execution_profile,
        ),
        "scaling_mode": str(lane.get("scaling_mode", "static")).strip() or "static",
        "scaling_group": str(lane.get("scaling_group", "")).strip(),
        "scaling_rank": max(1, _int_value(lane.get("scaling_rank", 1), 1)),
        "scaling_decision_file": str(lane.get("scaling_decision_file", "")),
        "scaling_min_marginal_npv_usd": _float_value(lane.get("scaling_min_marginal_npv_usd", 0.0), 0.0),
        "scaling_daily_budget_usd": _float_value(lane.get("scaling_daily_budget_usd", 0.0), 0.0),
        "scaling_max_parallel_agents": max(1, _int_value(lane.get("scaling_max_parallel_agents", 1), 1)),
        "scaling_max_subagents_per_agent": max(1, _int_value(lane.get("scaling_max_subagents_per_agent", 1), 1)),
        "scaling_event_counts": scaling_summary.get("counts", {}),
        "scale_up_events": _int_value(scaling_summary.get("counts", {}).get("scale_up", 0), 0),
        "scale_down_events": _int_value(scaling_summary.get("counts", {}).get("scale_down", 0), 0),
        "scale_hold_events": _int_value(scaling_summary.get("counts", {}).get("scale_hold", 0), 0),
        "latest_scale_event": (
            scaling_summary.get("latest_scale_event", {})
            if isinstance(scaling_summary.get("latest_scale_event", {}), dict)
            else {}
        ),
        "latest_log_line": "",
        "log_file": str(log_path),
        "pid_file": str(pid_path),
        "meta_file": str(meta_path),
        "events_file": str(events_path),
        "pause_file": str(pause_path),
        "heartbeat_file": str(heartbeat_file),
        "heartbeat_age_sec": -1,
        "heartbeat_stale": False,
        "paused": pause_path.exists(),
        "build_id": "",
        "expected_build_id": _lane_build_id(config, lane),
        "build_current": False,
        "state_counts": {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 1},
        "task_total": 0,
        "state_entries": 0,
        "missing_state_entries": 0,
        "extra_state_entries": 0,
        "last_event": {},
        "health": str(health).strip() or "error",
        "error": str(error).strip() or "lane status unavailable",
        "source": source,
        "meta": {},
    }


def lane_status_fallback_snapshot(config: ManagerConfig, *, error: str) -> dict[str, Any]:
    lanes, load_errors = _load_lane_specs_resilient(config)
    message = str(error).strip() or "lane status unavailable"
    snapshots = [
        _lane_unavailable_snapshot(
            config,
            lane,
            error=message,
            health="unknown",
            source="lane_status_fallback",
        )
        for lane in lanes
    ]
    errors: list[str] = [message]

    for entry in _lane_load_error_entries(load_errors):
        lane_id = str(entry.get("id", "lane_config")).strip() or "lane_config"
        lane_owner = str(entry.get("owner", "unknown")).strip() or "unknown"
        runtime_dir = (config.lanes_runtime_dir / lane_id).resolve()
        lane_error = str(entry.get("error", "lane configuration error")).strip() or "lane configuration error"
        snapshots.append(
            {
                "id": lane_id,
                "enabled": False,
                "owner": lane_owner,
                "description": "lane configuration load error",
                "running": False,
                "pid": None,
                "tasks_file": "",
                "task_queue_file": "",
                "task_queue_state_file": "",
                "dependency_state_file": "",
                "handoff_dir": "",
                "objective_file": "",
                "impl_repo": "",
                "test_repo": "",
                "metrics_file": "",
                "metrics_summary_file": "",
                "pricing_file": "",
                "routellm_policy_file": "",
                "routellm_enabled": False,
                "routellm_url": "",
                "routellm_timeout_sec": 0,
                "codex_cmd": "",
                "gemini_cmd": "",
                "claude_cmd": "",
                "codex_model": "",
                "gemini_model": "",
                "claude_model": "",
                "execution_profile": _normalize_execution_profile(config.execution_profile),
                "gemini_fallback_models": [],
                "exclusive_paths": [],
                "scaling_mode": "static",
                "scaling_group": "",
                "scaling_rank": 1,
                "scaling_decision_file": "",
                "scaling_min_marginal_npv_usd": 0.0,
                "scaling_daily_budget_usd": 0.0,
                "scaling_max_parallel_agents": 1,
                "scaling_max_subagents_per_agent": 1,
                "scaling_event_counts": {"scale_up": 0, "scale_down": 0, "scale_hold": 0},
                "scale_up_events": 0,
                "scale_down_events": 0,
                "scale_hold_events": 0,
                "latest_scale_event": {},
                "latest_log_line": "",
                "log_file": str(runtime_dir / "runner.log"),
                "pid_file": str(runtime_dir / "lane.pid"),
                "meta_file": str(runtime_dir / "lane.json"),
                "events_file": str(runtime_dir / "events.ndjson"),
                "pause_file": str(runtime_dir / "paused.flag"),
                "heartbeat_file": str(runtime_dir / "heartbeat.json"),
                "heartbeat_age_sec": -1,
                "heartbeat_stale": False,
                "paused": False,
                "build_id": "",
                "expected_build_id": "",
                "build_current": False,
                "state_counts": {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 1},
                "task_total": 0,
                "state_entries": 0,
                "missing_state_entries": 0,
                "extra_state_entries": 0,
                "last_event": {},
                "health": "error",
                "error": lane_error,
                "source": "lane_config",
                "meta": {},
            }
        )
        errors.append(lane_error)

    health_counts, owner_counts = _lane_health_owner_counts(snapshots)
    scaling_event_counts = {"scale_up": 0, "scale_down": 0, "scale_hold": 0}
    for lane in snapshots:
        scaling_counts = lane.get("scaling_event_counts", {})
        if not isinstance(scaling_counts, dict):
            continue
        for key in scaling_event_counts:
            scaling_event_counts[key] += _int_value(scaling_counts.get(key, 0), 0)
    parallel_capacity = _parallel_capacity_snapshot(config, snapshots)
    local_model_fleet = _local_model_fleet_snapshot(config)
    return {
        "timestamp": _now_iso(),
        "lanes_file": str(config.lanes_file),
        "running_count": 0,
        "total_count": len(snapshots),
        "lanes": snapshots,
        "health_counts": health_counts,
        "owner_counts": owner_counts,
        "scaling_event_counts": scaling_event_counts,
        "parallel_capacity": parallel_capacity,
        "local_model_fleet": local_model_fleet,
        "partial": True,
        "ok": False,
        "errors": errors,
    }


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
            paused = (not bool(lane.get("enabled", False))) or pause_path.exists()
            state_progress = _read_lane_state_progress(Path(lane["state_file"]), Path(lane["tasks_file"]))
            state_counts = state_progress["counts"]
            last_event = _tail_ndjson(events_path, 1)
            if _lane_intentionally_scaled_down(last_event, running=running):
                paused = True
            scaling_summary = _lane_scaling_event_summary(events_path)
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
                    "task_queue_file": str(lane["task_queue_file"]),
                    "task_queue_state_file": str(lane["task_queue_state_file"]),
                    "dependency_state_file": str(lane["dependency_state_file"]),
                    "handoff_dir": str(lane["handoff_dir"]),
                    "objective_file": str(lane["objective_file"]),
                    "impl_repo": str(lane["impl_repo"]),
                    "test_repo": str(lane["test_repo"]),
                    "runtime_impl_repo": str(meta.get("runtime_impl_repo", "")).strip() or str(lane["impl_repo"]),
                    "runtime_test_repo": str(meta.get("runtime_test_repo", "")).strip() or str(lane["test_repo"]),
                    "isolated_worktree": bool(lane.get("isolated_worktree", True)),
                    "worktree_root": str(lane.get("worktree_root", lane["runtime_dir"] / "worktrees")),
                    "worktree_base_ref": str(lane.get("worktree_base_ref", "origin/main")),
                    "continuous": bool(lane.get("continuous", False)),
                    "max_cycles": _int_value(lane.get("max_cycles", config.max_cycles), config.max_cycles),
                    "max_attempts": _int_value(lane.get("max_attempts", config.max_attempts), config.max_attempts),
                    "metrics_file": str(lane["metrics_file"]),
                    "metrics_summary_file": str(lane["metrics_summary_file"]),
                    "pricing_file": str(lane["pricing_file"]),
                    "routellm_policy_file": str(lane["routellm_policy_file"]),
                    "routellm_enabled": bool(lane["routellm_enabled"]),
                    "routellm_url": str(lane["routellm_url"]),
                    "routellm_timeout_sec": int(lane["routellm_timeout_sec"]),
                    "codex_cmd": str(lane["codex_cmd"]),
                    "gemini_cmd": str(lane["gemini_cmd"]),
                    "claude_cmd": str(lane["claude_cmd"]),
                    "codex_model": str(lane["codex_model"] or ""),
                    "gemini_model": str(lane["gemini_model"] or ""),
                    "claude_model": str(lane["claude_model"] or ""),
                    "execution_profile": _normalize_execution_profile(
                        lane.get("execution_profile", config.execution_profile),
                        default=config.execution_profile,
                    ),
                    "parallel_provider": _parallel_identity(lane)["owner"],
                    "parallel_model": _parallel_identity(lane)["model"],
                    "parallel_key": _parallel_identity(lane)["key"],
                    "gemini_fallback_models": [str(model) for model in lane["gemini_fallback_models"]],
                    "exclusive_paths": lane["exclusive_paths"],
                    "scaling_mode": str(lane.get("scaling_mode", "static")).strip() or "static",
                    "scaling_group": str(lane.get("scaling_group", "")).strip(),
                    "scaling_rank": max(1, _int_value(lane.get("scaling_rank", 1), 1)),
                    "scaling_decision_file": str(lane.get("scaling_decision_file", "")),
                    "scaling_min_marginal_npv_usd": _float_value(
                        lane.get("scaling_min_marginal_npv_usd", 0.0),
                        0.0,
                    ),
                    "scaling_daily_budget_usd": _float_value(lane.get("scaling_daily_budget_usd", 0.0), 0.0),
                    "scaling_max_parallel_agents": max(
                        1,
                        _int_value(lane.get("scaling_max_parallel_agents", 1), 1),
                    ),
                    "scaling_max_subagents_per_agent": max(
                        1,
                        _int_value(lane.get("scaling_max_subagents_per_agent", 1), 1),
                    ),
                    "scaling_event_counts": scaling_summary.get("counts", {}),
                    "scale_up_events": _int_value(scaling_summary.get("counts", {}).get("scale_up", 0), 0),
                    "scale_down_events": _int_value(scaling_summary.get("counts", {}).get("scale_down", 0), 0),
                    "scale_hold_events": _int_value(scaling_summary.get("counts", {}).get("scale_hold", 0), 0),
                    "latest_scale_event": (
                        scaling_summary.get("latest_scale_event", {})
                        if isinstance(scaling_summary.get("latest_scale_event", {}), dict)
                        else {}
                    ),
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
                    "task_queue_file": str(lane["task_queue_file"]),
                    "task_queue_state_file": str(lane["task_queue_state_file"]),
                    "dependency_state_file": str(lane["dependency_state_file"]),
                    "handoff_dir": str(lane["handoff_dir"]),
                    "objective_file": str(lane["objective_file"]),
                    "impl_repo": str(lane["impl_repo"]),
                    "test_repo": str(lane["test_repo"]),
                    "runtime_impl_repo": str(lane.get("impl_repo", "")),
                    "runtime_test_repo": str(lane.get("test_repo", "")),
                    "isolated_worktree": bool(lane.get("isolated_worktree", True)),
                    "worktree_root": str(lane.get("worktree_root", lane.get("runtime_dir", config.lanes_runtime_dir / lane_id / "worktrees"))),
                    "worktree_base_ref": str(lane.get("worktree_base_ref", "origin/main")),
                    "continuous": bool(lane.get("continuous", False)),
                    "max_cycles": _int_value(lane.get("max_cycles", config.max_cycles), config.max_cycles),
                    "max_attempts": _int_value(lane.get("max_attempts", config.max_attempts), config.max_attempts),
                    "metrics_file": str(lane["metrics_file"]),
                    "metrics_summary_file": str(lane["metrics_summary_file"]),
                    "pricing_file": str(lane["pricing_file"]),
                    "routellm_policy_file": str(lane["routellm_policy_file"]),
                    "routellm_enabled": bool(lane["routellm_enabled"]),
                    "routellm_url": str(lane["routellm_url"]),
                    "routellm_timeout_sec": int(lane["routellm_timeout_sec"]),
                    "codex_cmd": str(lane["codex_cmd"]),
                    "gemini_cmd": str(lane["gemini_cmd"]),
                    "claude_cmd": str(lane["claude_cmd"]),
                    "codex_model": str(lane["codex_model"] or ""),
                    "gemini_model": str(lane["gemini_model"] or ""),
                    "claude_model": str(lane["claude_model"] or ""),
                    "execution_profile": _normalize_execution_profile(
                        lane.get("execution_profile", config.execution_profile),
                        default=config.execution_profile,
                    ),
                    "parallel_provider": _parallel_identity(lane)["owner"],
                    "parallel_model": _parallel_identity(lane)["model"],
                    "parallel_key": _parallel_identity(lane)["key"],
                    "gemini_fallback_models": [str(model) for model in lane["gemini_fallback_models"]],
                    "exclusive_paths": lane["exclusive_paths"],
                    "scaling_mode": str(lane.get("scaling_mode", "static")).strip() or "static",
                    "scaling_group": str(lane.get("scaling_group", "")).strip(),
                    "scaling_rank": max(1, _int_value(lane.get("scaling_rank", 1), 1)),
                    "scaling_decision_file": str(lane.get("scaling_decision_file", "")),
                    "scaling_min_marginal_npv_usd": _float_value(
                        lane.get("scaling_min_marginal_npv_usd", 0.0),
                        0.0,
                    ),
                    "scaling_daily_budget_usd": _float_value(lane.get("scaling_daily_budget_usd", 0.0), 0.0),
                    "scaling_max_parallel_agents": max(
                        1,
                        _int_value(lane.get("scaling_max_parallel_agents", 1), 1),
                    ),
                    "scaling_max_subagents_per_agent": max(
                        1,
                        _int_value(lane.get("scaling_max_subagents_per_agent", 1), 1),
                    ),
                    "scaling_event_counts": {"scale_up": 0, "scale_down": 0, "scale_hold": 0},
                    "scale_up_events": 0,
                    "scale_down_events": 0,
                    "scale_hold_events": 0,
                    "latest_scale_event": {},
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
    for entry in _lane_load_error_entries(load_errors):
        lane_id = str(entry.get("id", "lane_config")).strip() or "lane_config"
        lane_owner = str(entry.get("owner", "unknown")).strip() or "unknown"
        message = str(entry.get("error", "lane configuration error")).strip() or "lane configuration error"
        runtime_dir = (config.lanes_runtime_dir / lane_id).resolve()
        snapshots.append(
            {
                "id": lane_id,
                "enabled": False,
                "owner": lane_owner,
                "description": "lane configuration load error",
                "running": False,
                "pid": None,
                "tasks_file": "",
                "task_queue_file": "",
                "task_queue_state_file": "",
                "dependency_state_file": "",
                "handoff_dir": "",
                "objective_file": "",
                "impl_repo": "",
                "test_repo": "",
                "metrics_file": "",
                "metrics_summary_file": "",
                "pricing_file": "",
                "routellm_policy_file": "",
                "routellm_enabled": False,
                "routellm_url": "",
                "routellm_timeout_sec": 0,
                "codex_cmd": "",
                "gemini_cmd": "",
                "claude_cmd": "",
                "codex_model": "",
                "gemini_model": "",
                "claude_model": "",
                "execution_profile": _normalize_execution_profile(config.execution_profile),
                "parallel_provider": str(lane_owner).strip().lower() or "unknown",
                "parallel_model": "default",
                "parallel_key": _parallel_key(str(lane_owner).strip().lower() or "unknown", "default"),
                "gemini_fallback_models": [],
                "exclusive_paths": [],
                "scaling_mode": "static",
                "scaling_group": "",
                "scaling_rank": 1,
                "scaling_decision_file": "",
                "scaling_min_marginal_npv_usd": 0.0,
                "scaling_daily_budget_usd": 0.0,
                "scaling_max_parallel_agents": 1,
                "scaling_max_subagents_per_agent": 1,
                "scaling_event_counts": {"scale_up": 0, "scale_down": 0, "scale_hold": 0},
                "scale_up_events": 0,
                "scale_down_events": 0,
                "scale_hold_events": 0,
                "latest_scale_event": {},
                "latest_log_line": "",
                "log_file": str(runtime_dir / "runner.log"),
                "pid_file": str(runtime_dir / "lane.pid"),
                "meta_file": str(runtime_dir / "lane.json"),
                "events_file": str(runtime_dir / "events.ndjson"),
                "pause_file": str(runtime_dir / "paused.flag"),
                "heartbeat_file": str(runtime_dir / "heartbeat.json"),
                "heartbeat_age_sec": -1,
                "heartbeat_stale": False,
                "paused": False,
                "build_id": "",
                "expected_build_id": "",
                "build_current": False,
                "state_counts": {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 1},
                "task_total": 0,
                "state_entries": 0,
                "missing_state_entries": 0,
                "extra_state_entries": 0,
                "last_event": {},
                "health": "error",
                "error": message,
                "source": "lane_config",
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
    scaling_event_counts = {"scale_up": 0, "scale_down": 0, "scale_hold": 0}
    for lane in snapshots:
        scaling_counts = lane.get("scaling_event_counts", {})
        if not isinstance(scaling_counts, dict):
            continue
        for key in scaling_event_counts:
            scaling_event_counts[key] += _int_value(scaling_counts.get(key, 0), 0)
    parallel_capacity = _parallel_capacity_snapshot(config, snapshots)
    local_model_fleet = _local_model_fleet_snapshot(config)
    return {
        "timestamp": _now_iso(),
        "lanes_file": str(config.lanes_file),
        "running_count": sum(1 for lane in snapshots if lane["running"]),
        "total_count": len(snapshots),
        "lanes": snapshots,
        "health_counts": health_counts,
        "owner_counts": owner_counts,
        "scaling_event_counts": scaling_event_counts,
        "parallel_capacity": parallel_capacity,
        "local_model_fleet": local_model_fleet,
        "partial": len(errors) > 0,
        "ok": len(errors) == 0,
        "errors": errors,
    }


def _lane_command_for_owner(lane: dict[str, Any], owner: str) -> str:
    if owner == "codex":
        return str(lane.get("codex_cmd", "")).strip() or "codex"
    if owner == "gemini":
        return str(lane.get("gemini_cmd", "")).strip() or "gemini"
    if owner == "claude":
        return str(lane.get("claude_cmd", "")).strip() or "claude"
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
    impl_repo = Path(lane.get("runtime_impl_repo", lane["impl_repo"]))
    test_repo = Path(lane.get("runtime_test_repo", lane["test_repo"]))
    lane_max_cycles = max(1, _int_value(lane.get("max_cycles", config.max_cycles), config.max_cycles))
    lane_max_attempts = max(1, _int_value(lane.get("max_attempts", config.max_attempts), config.max_attempts))
    lane_continuous = bool(lane.get("continuous", False))
    lane_recycle_delay = max(
        10,
        _int_value(lane.get("continuous_recycle_delay_sec", 90), 90),
    )
    lane_execution_profile = _normalize_execution_profile(
        lane.get("execution_profile", config.execution_profile),
        default=config.execution_profile,
    )
    if lane_execution_profile == "extra_high":
        lane_continuous = True
        lane_max_cycles = max(lane_max_cycles, EXTRA_HIGH_MIN_MAX_CYCLES)
    cmd = [
        sys.executable,
        "-m",
        "orxaq_autonomy.runner",
        "--impl-repo",
        str(impl_repo),
        "--test-repo",
        str(test_repo),
        "--tasks-file",
        str(lane["tasks_file"]),
        "--state-file",
        str(lane["state_file"]),
        "--task-queue-file",
        str(lane["task_queue_file"]),
        "--task-queue-state-file",
        str(lane["task_queue_state_file"]),
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
        "--routellm-policy-file",
        str(lane["routellm_policy_file"]),
        "--routellm-timeout-sec",
        str(lane["routellm_timeout_sec"]),
        "--max-cycles",
        str(lane_max_cycles),
        "--max-attempts",
        str(lane_max_attempts),
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
        str(lane["codex_cmd"]),
        "--gemini-cmd",
        str(lane["gemini_cmd"]),
        "--claude-cmd",
        str(lane["claude_cmd"]),
        "--owner-filter",
        lane["owner"],
        "--execution-profile",
        lane_execution_profile,
    ]
    if lane_continuous:
        cmd.extend(
            [
                "--continuous",
                "--continuous-recycle-delay-sec",
                str(lane_recycle_delay),
            ]
        )
    cmd.append("--routellm-enabled" if lane["routellm_enabled"] else "--no-routellm-enabled")
    if str(lane["routellm_url"]).strip():
        cmd.extend(["--routellm-url", str(lane["routellm_url"]).strip()])
    if lane["mcp_context_file"] is not None:
        cmd.extend(["--mcp-context-file", str(lane["mcp_context_file"])])
    if lane["codex_model"]:
        cmd.extend(["--codex-model", str(lane["codex_model"])])
    if lane["gemini_model"]:
        cmd.extend(["--gemini-model", str(lane["gemini_model"])])
    for model in lane["gemini_fallback_models"]:
        cmd.extend(["--gemini-fallback-model", model])
    if lane["claude_model"]:
        cmd.extend(["--claude-model", str(lane["claude_model"])])
    cmd.extend(
        [
            "--auto-push-interval-sec",
            str(config.auto_push_interval_sec),
            ("--auto-push-guard" if config.auto_push_guard else "--no-auto-push-guard"),
        ]
    )

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
    lanes, _ = _load_lane_specs_resilient(config)
    lane = next((item for item in lanes if item["id"] == lane_id), None)
    if lane is None:
        raise RuntimeError(f"Unknown lane id {lane_id!r}. Update {config.lanes_file}.")
    _append_lane_event(config, lane_id, "start_requested", {"owner": lane["owner"]})

    cmd_name = _lane_command_for_owner(lane, lane["owner"])
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

    lane_runtime = dict(lane)
    try:
        runtime_payload = _prepare_lane_runtime_repos(config=config, lane=lane_runtime)
        lane_runtime.update(runtime_payload)
    except Exception as err:
        _append_lane_event(
            config,
            lane_id,
            "start_failed",
            {"reason": "worktree_prepare_failed", "error": str(err)},
        )
        raise RuntimeError(f"Lane {lane_id}: unable to prepare isolated runtime repositories: {err}") from err

    for repo in {
        Path(lane_runtime["runtime_impl_repo"]).resolve(),
        Path(lane_runtime["runtime_test_repo"]).resolve(),
    }:
        ok, message = _repo_basic_check(repo)
        if not ok:
            _append_lane_event(config, lane_id, "start_failed", {"reason": message, "repo": str(repo)})
            raise RuntimeError(f"Lane {lane_id}: runtime repository check failed for {repo}: {message}")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    lane_runtime_env = _apply_local_model_capacity_env(config, lane_runtime)
    lane_runtime["env"] = dict(lane_runtime_env)
    cmd = _build_lane_runner_cmd(config, lane_runtime)
    kwargs: dict[str, Any] = {
        "cwd": str(config.root_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
        "env": _runtime_env(
            {**_load_env_file(config.env_file), **lane_runtime_env},
            root_dir=config.root_dir,
        ),
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
            "task_queue_file": str(lane["task_queue_file"]),
            "task_queue_state_file": str(lane["task_queue_state_file"]),
            "dependency_state_file": str(lane["dependency_state_file"]),
            "handoff_dir": str(lane["handoff_dir"]),
            "objective_file": str(lane["objective_file"]),
            "impl_repo": str(lane["impl_repo"]),
            "test_repo": str(lane["test_repo"]),
            "runtime_impl_repo": str(lane_runtime.get("runtime_impl_repo", lane["impl_repo"])),
            "runtime_test_repo": str(lane_runtime.get("runtime_test_repo", lane["test_repo"])),
            "isolated_worktree": bool(lane.get("isolated_worktree", True)),
            "worktree_root": str(lane.get("worktree_root", lane["runtime_dir"] / "worktrees")),
            "worktree_base_ref": str(lane.get("worktree_base_ref", "origin/main")),
            "worktree_branches": dict(lane_runtime.get("worktree_branches", {})),
            "continuous": bool(lane.get("continuous", False)),
            "continuous_recycle_delay_sec": _int_value(lane.get("continuous_recycle_delay_sec", 90), 90),
            "max_cycles": _int_value(lane.get("max_cycles", config.max_cycles), config.max_cycles),
            "max_attempts": _int_value(lane.get("max_attempts", config.max_attempts), config.max_attempts),
            "conversation_log_file": str(lane["conversation_log_file"]),
            "metrics_file": str(lane["metrics_file"]),
            "metrics_summary_file": str(lane["metrics_summary_file"]),
            "pricing_file": str(lane["pricing_file"]),
            "routellm_policy_file": str(lane["routellm_policy_file"]),
            "routellm_enabled": bool(lane["routellm_enabled"]),
            "routellm_url": str(lane["routellm_url"]),
            "routellm_timeout_sec": int(lane["routellm_timeout_sec"]),
            "codex_cmd": str(lane["codex_cmd"]),
            "gemini_cmd": str(lane["gemini_cmd"]),
            "claude_cmd": str(lane["claude_cmd"]),
            "codex_model": str(lane["codex_model"] or ""),
            "gemini_model": str(lane["gemini_model"] or ""),
            "claude_model": str(lane["claude_model"] or ""),
            "execution_profile": _normalize_execution_profile(
                lane.get("execution_profile", config.execution_profile),
                default=config.execution_profile,
            ),
            "env": dict(lane_runtime.get("env", {})),
            "gemini_fallback_models": [str(model) for model in lane["gemini_fallback_models"]],
            "build_id": _lane_build_id(config, lane),
            "exclusive_paths": lane["exclusive_paths"],
            "scaling_mode": str(lane.get("scaling_mode", "static")),
            "scaling_group": str(lane.get("scaling_group", "")),
            "scaling_rank": max(1, _int_value(lane.get("scaling_rank", 1), 1)),
            "scaling_decision_file": str(lane.get("scaling_decision_file", "")),
            "scaling_min_marginal_npv_usd": _float_value(lane.get("scaling_min_marginal_npv_usd", 0.0), 0.0),
            "scaling_daily_budget_usd": _float_value(lane.get("scaling_daily_budget_usd", 0.0), 0.0),
            "scaling_max_parallel_agents": max(1, _int_value(lane.get("scaling_max_parallel_agents", 1), 1)),
            "scaling_max_subagents_per_agent": max(
                1,
                _int_value(lane.get("scaling_max_subagents_per_agent", 1), 1),
            ),
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
            "task_queue_file": str(lane["task_queue_file"]),
            "task_queue_state_file": str(lane["task_queue_state_file"]),
            "runtime_impl_repo": str(lane_runtime.get("runtime_impl_repo", lane["impl_repo"])),
            "runtime_test_repo": str(lane_runtime.get("runtime_test_repo", lane["test_repo"])),
            "continuous": bool(lane.get("continuous", False)),
            "execution_profile": _normalize_execution_profile(
                lane.get("execution_profile", config.execution_profile),
                default=config.execution_profile,
            ),
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


def stop_lane_background(
    config: ManagerConfig,
    lane_id: str,
    *,
    reason: str = "manual",
    pause: bool = True,
) -> dict[str, Any]:
    pid_path = _lane_pid_file(config, lane_id)
    pid = _read_pid(pid_path)
    lanes, _ = _load_lane_specs_resilient(config)
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
    if reason == "manual" and pause:
        _lane_pause_file(config, lane_id).write_text("manual\n", encoding="utf-8")
    else:
        _lane_pause_file(config, lane_id).unlink(missing_ok=True)
    _append_lane_event(config, lane_id, "stopped", {"pid": pid, "reason": reason})
    status = lane_status_snapshot(config)
    return next((item for item in status["lanes"] if item["id"] == lane_id), {"id": lane_id, "running": False})


def ensure_lanes_background(config: ManagerConfig, lane_id: str | None = None) -> dict[str, Any]:
    requested_lane = lane_id.strip() if isinstance(lane_id, str) and lane_id.strip() else None
    lanes, load_errors = _load_lane_specs_resilient(config)
    resolved_lane = _resolve_requested_lane_id(lanes, requested_lane or "") if requested_lane is not None else None
    selected = [lane for lane in lanes if lane["enabled"]] if resolved_lane is None else [lane for lane in lanes if lane["id"] == resolved_lane]
    if requested_lane is not None and resolved_lane is None:
        raise RuntimeError(f"Unknown lane id {requested_lane!r}. Update {config.lanes_file}.")
    swarm_budget = _current_swarm_budget_status(config, selected if selected else lanes)
    swarm_budget_hard_stop = bool(swarm_budget.get("hard_stop", False))
    scaling_plan = _evaluate_lane_scaling_plan(config, lanes, requested_lane=resolved_lane)
    ensured: list[dict[str, Any]] = []
    started: list[dict[str, Any]] = []
    restarted: list[dict[str, Any]] = []
    scaled_up: list[dict[str, Any]] = []
    scaled_down: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    config_failures = _lane_load_error_entries(load_errors) if requested_lane is None else []
    failed: list[dict[str, Any]] = list(config_failures)

    snapshot = lane_status_snapshot(config)
    by_id = {lane["id"]: lane for lane in snapshot.get("lanes", [])}
    lane_map = {str(lane.get("id", "")).strip(): lane for lane in lanes if str(lane.get("id", "")).strip()}
    mesh_command_effects = _consume_mesh_scaling_commands(
        config,
        lane_map=lane_map,
        status_by_id=by_id,
        requested_lane=resolved_lane or "",
    )
    parallel_plan = _parallel_capacity_plan(
        config,
        lanes,
        scaling_plan=scaling_plan,
        status_by_id=by_id,
        operation="ensure",
        requested_lane=resolved_lane or "",
    )
    parallel_groups = parallel_plan.get("groups", {})
    parallel_by_lane = parallel_plan.get("by_lane", {})
    running_by_parallel_key = {
        key: _int_value(payload.get("running_count", 0), 0)
        for key, payload in parallel_groups.items()
        if isinstance(payload, dict)
    }
    scaling_by_lane = scaling_plan.get("by_lane", {}) if isinstance(scaling_plan.get("by_lane", {}), dict) else {}
    mesh_scaling_decision = _latest_mesh_scaling_decision(config, resolved_lane or "all_enabled")
    mesh_action = str(mesh_scaling_decision.get("action", "")).strip().lower()
    mesh_reason = str(mesh_scaling_decision.get("reason", "")).strip() or "mesh_decision"
    mesh_target_delta = _int_value(mesh_scaling_decision.get("target_delta", 0), 0)
    mesh_parallel_limit_boost = 1 if mesh_action == "scale_up" and mesh_target_delta > 0 else 0
    if mesh_action in {"scale_down", "scale_up"}:
        for lane in selected:
            lane_id = str(lane.get("id", "")).strip()
            if not lane_id:
                continue
            lane_scaling = scaling_by_lane.get(lane_id, {})
            if not isinstance(lane_scaling, dict):
                lane_scaling = {}
            lane_scaling = dict(lane_scaling)
            lane_scaling["mode"] = "mesh"
            lane_scaling["reason"] = f"mesh_decision_{mesh_action}:{mesh_reason}"
            lane_scaling["decision"] = {
                "action": mesh_action,
                "reason": mesh_reason,
                "target_delta": mesh_target_delta,
                "event_id": str(mesh_scaling_decision.get("event_id", "")).strip(),
                "age_sec": _int_value(mesh_scaling_decision.get("age_sec", -1), -1),
            }
            if mesh_action == "scale_down":
                lane_scaling["allowed"] = False
                lane_scaling["allowed_parallel_lanes"] = 0
            else:
                lane_scaling["allowed"] = True
                lane_scaling["allowed_parallel_lanes"] = max(
                    1,
                    _int_value(lane_scaling.get("allowed_parallel_lanes", 1), 1) + mesh_parallel_limit_boost,
                )
            scaling_by_lane[lane_id] = lane_scaling
    mesh_scaling_decision = _latest_mesh_scaling_decision(config, resolved_lane or "all_enabled")
    mesh_action = str(mesh_scaling_decision.get("action", "")).strip().lower()
    mesh_reason = str(mesh_scaling_decision.get("reason", "")).strip() or "mesh_decision"
    mesh_target_delta = _int_value(mesh_scaling_decision.get("target_delta", 0), 0)
    mesh_parallel_limit_boost = 1 if mesh_action == "scale_up" and mesh_target_delta > 0 else 0
    if mesh_action in {"scale_down", "scale_up"}:
        for lane in selected:
            lane_id = str(lane.get("id", "")).strip()
            if not lane_id:
                continue
            lane_scaling = scaling_by_lane.get(lane_id, {})
            if not isinstance(lane_scaling, dict):
                lane_scaling = {}
            lane_scaling = dict(lane_scaling)
            lane_scaling["mode"] = "mesh"
            lane_scaling["reason"] = f"mesh_decision_{mesh_action}:{mesh_reason}"
            lane_scaling["decision"] = {
                "action": mesh_action,
                "reason": mesh_reason,
                "target_delta": mesh_target_delta,
                "event_id": str(mesh_scaling_decision.get("event_id", "")).strip(),
                "age_sec": _int_value(mesh_scaling_decision.get("age_sec", -1), -1),
            }
            if mesh_action == "scale_down":
                lane_scaling["allowed"] = False
                lane_scaling["allowed_parallel_lanes"] = 0
            else:
                lane_scaling["allowed"] = True
                lane_scaling["allowed_parallel_lanes"] = max(
                    1,
                    _int_value(lane_scaling.get("allowed_parallel_lanes", 1), 1) + mesh_parallel_limit_boost,
                )
            scaling_by_lane[lane_id] = lane_scaling
    desired_running_by_parallel_key: dict[str, set[str]] = {}

    def _parallel_lane_context(lane_payload: dict[str, Any]) -> dict[str, Any]:
        lane_identity = _parallel_identity(lane_payload)
        lane_key = lane_identity["key"]
        lane_plan = parallel_by_lane.get(str(lane_payload.get("id", "")).strip(), {})
        if isinstance(lane_plan, dict) and str(lane_plan.get("parallel_key", "")).strip():
            lane_key = str(lane_plan.get("parallel_key")).strip()
            lane_identity["owner"], lane_identity["model"], lane_identity["endpoint_key"] = _split_parallel_key(lane_key)
        group = parallel_groups.get(lane_key, {}) if isinstance(parallel_groups.get(lane_key, {}), dict) else {}
        limit = max(
            1,
            _int_value(
                group.get("effective_limit", config.parallel_capacity_default_limit),
                config.parallel_capacity_default_limit,
            ),
        )
        limit += mesh_parallel_limit_boost
        return {
            "parallel_key": lane_key,
            "parallel_provider": lane_identity["owner"],
            "parallel_model": lane_identity["model"],
            "parallel_endpoint": lane_identity.get("endpoint_key", ""),
            "parallel_limit": limit,
        }

    if resolved_lane is None:
        lane_ids_by_parallel_key: dict[str, list[str]] = {}
        for lane in selected:
            lane_id = str(lane.get("id", "")).strip()
            if not lane_id:
                continue
            lane_scaling = scaling_by_lane.get(lane_id, {})
            if not bool(lane_scaling.get("allowed", True)):
                continue
            lane_context = _parallel_lane_context(lane)
            lane_ids_by_parallel_key.setdefault(lane_context["parallel_key"], []).append(lane_id)
        rotation_tick = int(dt.datetime.now(dt.timezone.utc).timestamp() // 60)
        for parallel_key, lane_ids in lane_ids_by_parallel_key.items():
            if not lane_ids:
                continue
            group = parallel_groups.get(parallel_key, {}) if isinstance(parallel_groups.get(parallel_key, {}), dict) else {}
            limit = max(
                1,
                _int_value(
                    group.get("effective_limit", config.parallel_capacity_default_limit),
                    config.parallel_capacity_default_limit,
                ),
            )
            prioritized_all = sorted(
                lane_ids,
                key=lambda lane_name: (
                    _int_value(scaling_by_lane.get(lane_name, {}).get("slot", 1), 1),
                    _int_value(scaling_by_lane.get(lane_name, {}).get("rank", 1), 1),
                    lane_name,
                ),
            )
            if len(prioritized_all) <= limit:
                desired_running_by_parallel_key[parallel_key] = set(prioritized_all)
                continue
            offset = rotation_tick % len(prioritized_all)
            rotated = prioritized_all[offset:] + prioritized_all[:offset]
            desired_running_by_parallel_key[parallel_key] = set(rotated[:limit])

        running_by_key: dict[str, list[str]] = {}
        for lane in selected:
            current_lane_id = lane["id"]
            current = by_id.get(current_lane_id, {})
            if not bool(current.get("running", False)):
                continue
            lane_scaling = scaling_by_lane.get(current_lane_id, {})
            if not bool(lane_scaling.get("allowed", True)):
                continue
            lane_context = _parallel_lane_context(lane)
            running_by_key.setdefault(lane_context["parallel_key"], []).append(current_lane_id)
        for parallel_key, running_ids in running_by_key.items():
            group = parallel_groups.get(parallel_key, {}) if isinstance(parallel_groups.get(parallel_key, {}), dict) else {}
            limit = max(
                1,
                _int_value(
                    group.get("effective_limit", config.parallel_capacity_default_limit),
                    config.parallel_capacity_default_limit,
                ),
            )
            desired_running = desired_running_by_parallel_key.get(parallel_key, set())
            running_set = set(running_ids)
            desired_missing = [lane_name for lane_name in desired_running if lane_name not in running_set]
            if len(running_ids) <= limit and not desired_missing:
                continue
            prioritized = sorted(
                running_ids,
                key=lambda lane_name: (
                    _int_value(scaling_by_lane.get(lane_name, {}).get("slot", 1), 1),
                    _int_value(scaling_by_lane.get(lane_name, {}).get("rank", 1), 1),
                    lane_name,
                ),
            )
            if desired_running:
                overflow = [lane_name for lane_name in prioritized if lane_name not in desired_running]
                required_stops = max(0, len(prioritized) - limit)
                # Only preempt incumbents for rotation when the group is saturated.
                if len(prioritized) >= limit:
                    required_stops = max(required_stops, len(desired_missing))
                if len(overflow) < required_stops:
                    overflow.extend(
                        lane_name
                        for lane_name in prioritized
                        if lane_name in desired_running and lane_name not in overflow
                    )
                overflow = overflow[:required_stops]
            else:
                overflow = prioritized[limit:]
            for overflow_lane_id in overflow:
                lane_spec = next((item for item in selected if item["id"] == overflow_lane_id), None)
                if lane_spec is None:
                    continue
                lane_context = _parallel_lane_context(lane_spec)
                scale_down_reason = (
                    "rotation_rebalance"
                    if desired_running and len(prioritized) <= limit
                    else "provider_model_parallel_limit"
                )
                try:
                    stop_lane_background(
                        config,
                        overflow_lane_id,
                        reason="scale_down_parallel_limit",
                        pause=False,
                    )
                    running_by_parallel_key[parallel_key] = max(0, running_by_parallel_key.get(parallel_key, 0) - 1)
                    if isinstance(by_id.get(overflow_lane_id, {}), dict):
                        by_id[overflow_lane_id]["running"] = False
                    scaled_down.append(
                        {
                            "id": overflow_lane_id,
                            "status": "scaled_down",
                            "reason": scale_down_reason,
                            "parallel_key": parallel_key,
                            "provider": lane_context["parallel_provider"],
                            "model": lane_context["parallel_model"],
                            "parallel_limit": lane_context["parallel_limit"],
                            "running_count": len(prioritized),
                        }
                    )
                    _append_lane_event(
                        config,
                        overflow_lane_id,
                        "scale_down",
                        {
                            "reason": scale_down_reason,
                            "parallel_key": parallel_key,
                            "provider": lane_context["parallel_provider"],
                            "model": lane_context["parallel_model"],
                            "parallel_limit": lane_context["parallel_limit"],
                            "running_count": len(prioritized),
                        },
                    )
                except Exception as err:
                    failed.append(
                        {
                            "id": overflow_lane_id,
                            "error": str(err),
                            "source": "lane_runtime",
                        }
                    )

    for lane in selected:
        if not lane["enabled"]:
            skipped.append({"id": lane["id"], "reason": "disabled"})
            continue
        current_lane_id = lane["id"]
        pause_file = _lane_pause_file(config, current_lane_id)
        current = by_id.get(current_lane_id, {})
        running = bool(current.get("running", False))
        stale = bool(current.get("heartbeat_stale", False))
        build_current = bool(current.get("build_current", False))
        lane_context = _parallel_lane_context(lane)
        parallel_key = lane_context["parallel_key"]
        parallel_limit = lane_context["parallel_limit"]
        current_parallel_running = max(0, _int_value(running_by_parallel_key.get(parallel_key, 0), 0))
        lane_scaling = scaling_plan.get("by_lane", {}).get(current_lane_id, {})
        lane_allowed = bool(lane_scaling.get("allowed", True))
        lane_scale_reason = str(lane_scaling.get("reason", "npv_gate_hold")).strip() or "npv_gate_hold"
        scale_payload = {
            "group": str(lane_scaling.get("group", "")).strip(),
            "slot": _int_value(lane_scaling.get("slot", 1), 1),
            "allowed_parallel_lanes": _int_value(lane_scaling.get("allowed_parallel_lanes", 1), 1),
            "reason": lane_scale_reason,
            "reasons": lane_scaling.get("reasons", []),
            "decision": lane_scaling.get("decision", {}),
        }
        if not lane_allowed:
            if running:
                stop_lane_background(
                    config,
                    current_lane_id,
                    reason="scale_down_npv_gate",
                    pause=False,
                )
                running_by_parallel_key[parallel_key] = max(0, current_parallel_running - 1)
                scaled_down.append(
                    {
                        "id": current_lane_id,
                        "status": "scaled_down",
                        "reason": lane_scale_reason,
                        "group": scale_payload["group"],
                    }
                )
                _append_lane_event(config, current_lane_id, "scale_down", scale_payload)
            else:
                skipped.append(
                    {
                        "id": current_lane_id,
                        "reason": lane_scale_reason,
                        "group": scale_payload["group"],
                    }
                )
            continue
        if pause_file.exists():
            skipped.append({"id": current_lane_id, "reason": "manually_paused"})
            continue
        if not running and not bool(lane.get("continuous", False)):
            state_counts = current.get("state_counts", {}) if isinstance(current.get("state_counts", {}), dict) else {}
            done_count = _int_value(state_counts.get("done", 0), 0)
            pending_count = _int_value(state_counts.get("pending", 0), 0)
            in_progress_count = _int_value(state_counts.get("in_progress", 0), 0)
            blocked_count = _int_value(state_counts.get("blocked", 0), 0)
            task_total = _int_value(current.get("task_total", 0), 0)
            if (
                task_total > 0
                and done_count >= task_total
                and pending_count == 0
                and in_progress_count == 0
                and blocked_count == 0
            ):
                skipped.append({"id": current_lane_id, "reason": "completed_non_continuous"})
                continue
            if (
                task_total > 0
                and pending_count == 0
                and in_progress_count == 0
                and blocked_count > 0
                and (done_count + blocked_count) >= task_total
            ):
                skipped.append({"id": current_lane_id, "reason": "blocked_terminal_non_continuous"})
                continue
        if running and not stale and build_current:
            ensured.append({"id": current_lane_id, "status": "running"})
            continue
        if (
            running
            and (stale or not build_current)
            and swarm_budget_hard_stop
        ):
            hold_payload = {
                "reason": "swarm_daily_budget_cap_restart_hold",
                "parallel_key": parallel_key,
                "provider": lane_context["parallel_provider"],
                "model": lane_context["parallel_model"],
                "daily_spend_usd": _float_value(swarm_budget.get("daily_spend_usd", 0.0), 0.0),
                "daily_budget_usd": _float_value(swarm_budget.get("daily_budget_usd", 0.0), 0.0),
            }
            skipped.append({"id": current_lane_id, **hold_payload})
            _append_lane_event(config, current_lane_id, "scale_hold", hold_payload)
            continue
        desired_running = desired_running_by_parallel_key.get(parallel_key, set())
        if (
            resolved_lane is None
            and not running
            and desired_running
            and current_lane_id not in desired_running
        ):
            skipped.append(
                {
                    "id": current_lane_id,
                    "reason": "rotation_hold",
                    "parallel_key": parallel_key,
                    "provider": lane_context["parallel_provider"],
                    "model": lane_context["parallel_model"],
                    "parallel_limit": parallel_limit,
                    "running_count": current_parallel_running,
                }
            )
            continue
        if not running and current_parallel_running >= parallel_limit:
            skip_payload = {
                "id": current_lane_id,
                "reason": "provider_model_parallel_limit",
                "parallel_key": parallel_key,
                "provider": lane_context["parallel_provider"],
                "model": lane_context["parallel_model"],
                "parallel_limit": parallel_limit,
                "running_count": current_parallel_running,
            }
            skipped.append(skip_payload)
            _append_lane_event(
                config,
                current_lane_id,
                "scale_hold",
                {
                    "reason": "provider_model_parallel_limit",
                    "parallel_key": parallel_key,
                    "provider": lane_context["parallel_provider"],
                    "model": lane_context["parallel_model"],
                    "parallel_limit": parallel_limit,
                    "running_count": current_parallel_running,
                },
            )
            continue
        if not running and swarm_budget_hard_stop:
            skip_payload = {
                "id": current_lane_id,
                "reason": "swarm_daily_budget_cap",
                "parallel_key": parallel_key,
                "provider": lane_context["parallel_provider"],
                "model": lane_context["parallel_model"],
                "parallel_limit": parallel_limit,
                "running_count": current_parallel_running,
                "daily_spend_usd": _float_value(swarm_budget.get("daily_spend_usd", 0.0), 0.0),
                "daily_budget_usd": _float_value(swarm_budget.get("daily_budget_usd", 0.0), 0.0),
            }
            skipped.append(skip_payload)
            _append_lane_event(
                config,
                current_lane_id,
                "scale_hold",
                {
                    "reason": "swarm_daily_budget_cap",
                    "parallel_key": parallel_key,
                    "provider": lane_context["parallel_provider"],
                    "model": lane_context["parallel_model"],
                    "parallel_limit": parallel_limit,
                    "running_count": current_parallel_running,
                    "daily_spend_usd": _float_value(swarm_budget.get("daily_spend_usd", 0.0), 0.0),
                    "daily_budget_usd": _float_value(swarm_budget.get("daily_budget_usd", 0.0), 0.0),
                },
            )
            continue
        try:
            if running and (stale or not build_current):
                reason = "stale_heartbeat" if stale else "build_update"
                stop_lane_background(config, current_lane_id, reason=reason)
                running_by_parallel_key[parallel_key] = max(0, current_parallel_running - 1)
                started_lane = start_lane_background(config, current_lane_id)
                running_by_parallel_key[parallel_key] = running_by_parallel_key.get(parallel_key, 0) + 1
                restarted.append({"id": current_lane_id, "status": "restarted", "pid": started_lane.get("pid")})
                _append_lane_event(config, current_lane_id, "auto_restarted", {"reason": reason})
            else:
                started_lane = start_lane_background(config, current_lane_id)
                running_by_parallel_key[parallel_key] = running_by_parallel_key.get(parallel_key, 0) + 1
                started.append({"id": current_lane_id, "status": "started", "pid": started_lane.get("pid")})
                _append_lane_event(config, current_lane_id, "auto_started", {"reason": "not_running"})
                if (
                    str(lane_scaling.get("mode", "static")).strip().lower() == "npv"
                    and _int_value(lane_scaling.get("slot", 1), 1) > 1
                    and _int_value(lane_scaling.get("allowed_parallel_lanes", 1), 1) > 1
                ):
                    scaled_up.append(
                        {
                            "id": current_lane_id,
                            "status": "scaled_up",
                            "group": str(lane_scaling.get("group", "")).strip(),
                            "slot": _int_value(lane_scaling.get("slot", 1), 1),
                        }
                    )
                    _append_lane_event(config, current_lane_id, "scale_up", scale_payload)
        except Exception as err:
            failed.append({"id": current_lane_id, "error": str(err), "source": "lane_runtime"})
            _append_lane_event(config, current_lane_id, "ensure_failed", {"error": str(err)})

    result = {
        "timestamp": _now_iso(),
        "requested_lane": resolved_lane or "all_enabled",
        "ensured_count": len(ensured),
        "started_count": len(started),
        "restarted_count": len(restarted),
        "scaled_up_count": len(scaled_up),
        "scaled_down_count": len(scaled_down),
        "skipped_count": len(skipped),
        "config_error_count": len(config_failures),
        "config_errors": [item["error"] for item in config_failures],
        "failed_count": len(failed),
        "scaling": scaling_plan,
        "parallel_capacity": parallel_plan,
        "ensured": ensured,
        "started": started,
        "restarted": restarted,
        "scaled_up": scaled_up,
        "scaled_down": scaled_down,
        "skipped": skipped,
        "failed": failed,
        "ok": len(failed) == 0,
        "mesh_scaling_decision": mesh_scaling_decision,
        "mesh_command_effects": mesh_command_effects,
        "swarm_daily_budget": swarm_budget,
    }
    _emit_mesh_event(
        config,
        topic="scheduling",
        event_type="lanes.ensure.summary",
        payload={
            "requested_lane": result["requested_lane"],
            "ensured_count": result["ensured_count"],
            "started_count": result["started_count"],
            "restarted_count": result["restarted_count"],
            "scaled_up_count": result["scaled_up_count"],
            "scaled_down_count": result["scaled_down_count"],
            "skipped_count": result["skipped_count"],
            "failed_count": result["failed_count"],
            "parallel_groups_at_limit": sum(
                1
                for payload in (
                    result.get("parallel_capacity", {}).get("groups", {}).values()
                    if isinstance(result.get("parallel_capacity", {}).get("groups", {}), dict)
                    else []
                )
                if isinstance(payload, dict)
                and _int_value(payload.get("running_count", 0), 0) >= _int_value(payload.get("effective_limit", 1), 1)
            ),
            "ok": result["ok"],
        },
    )
    for item in started:
        _emit_mesh_event(
            config,
            topic="scheduling",
            event_type="lane.started",
            payload={"lane_id": str(item.get("id", "")).strip(), "status": str(item.get("status", "started")).strip()},
        )
    for item in restarted:
        _emit_mesh_event(
            config,
            topic="scheduling",
            event_type="lane.restarted",
            payload={
                "lane_id": str(item.get("id", "")).strip(),
                "status": str(item.get("status", "restarted")).strip(),
            },
        )
    for item in failed:
        _emit_mesh_event(
            config,
            topic="monitoring",
            event_type="lane.ensure_failed",
            payload={"lane_id": str(item.get("id", "")).strip(), "error": str(item.get("error", "")).strip()},
        )
    _dispatch_mesh_events(config, max_events=64)
    return result


def start_lanes_background(config: ManagerConfig, lane_id: str | None = None) -> dict[str, Any]:
    requested_lane = lane_id.strip() if isinstance(lane_id, str) and lane_id.strip() else None
    lanes, load_errors = _load_lane_specs_resilient(config)
    resolved_lane = _resolve_requested_lane_id(lanes, requested_lane or "") if requested_lane is not None else None
    selected = [lane for lane in lanes if lane["enabled"]] if resolved_lane is None else [lane for lane in lanes if lane["id"] == resolved_lane]
    if requested_lane is not None and resolved_lane is None:
        raise RuntimeError(f"Unknown lane id {requested_lane!r}. Update {config.lanes_file}.")
    swarm_budget = _current_swarm_budget_status(config, selected if selected else lanes)
    swarm_budget_hard_stop = bool(swarm_budget.get("hard_stop", False))
    scaling_plan = _evaluate_lane_scaling_plan(config, lanes, requested_lane=resolved_lane)
    snapshot = lane_status_snapshot(config)
    by_id = {lane["id"]: lane for lane in snapshot.get("lanes", [])}
    lane_map = {str(lane.get("id", "")).strip(): lane for lane in lanes if str(lane.get("id", "")).strip()}
    mesh_command_effects = _consume_mesh_scaling_commands(
        config,
        lane_map=lane_map,
        status_by_id=by_id,
        requested_lane=resolved_lane or "",
    )
    parallel_plan = _parallel_capacity_plan(
        config,
        lanes,
        scaling_plan=scaling_plan,
        status_by_id=by_id,
        operation="start",
        requested_lane=resolved_lane or "",
    )
    parallel_groups = parallel_plan.get("groups", {})
    parallel_by_lane = parallel_plan.get("by_lane", {})
    running_by_parallel_key = {
        key: _int_value(payload.get("running_count", 0), 0)
        for key, payload in parallel_groups.items()
        if isinstance(payload, dict)
    }
    scaling_by_lane = scaling_plan.get("by_lane", {}) if isinstance(scaling_plan.get("by_lane", {}), dict) else {}
    mesh_scaling_decision = _latest_mesh_scaling_decision(config, resolved_lane or "all_enabled")
    mesh_action = str(mesh_scaling_decision.get("action", "")).strip().lower()
    mesh_reason = str(mesh_scaling_decision.get("reason", "")).strip() or "mesh_decision"
    mesh_target_delta = _int_value(mesh_scaling_decision.get("target_delta", 0), 0)
    mesh_parallel_limit_boost = 1 if mesh_action == "scale_up" and mesh_target_delta > 0 else 0
    if mesh_action in {"scale_down", "scale_up"}:
        for lane in selected:
            lane_id = str(lane.get("id", "")).strip()
            if not lane_id:
                continue
            lane_scaling = scaling_by_lane.get(lane_id, {})
            if not isinstance(lane_scaling, dict):
                lane_scaling = {}
            lane_scaling = dict(lane_scaling)
            lane_scaling["mode"] = "mesh"
            lane_scaling["reason"] = f"mesh_decision_{mesh_action}:{mesh_reason}"
            lane_scaling["decision"] = {
                "action": mesh_action,
                "reason": mesh_reason,
                "target_delta": mesh_target_delta,
                "event_id": str(mesh_scaling_decision.get("event_id", "")).strip(),
                "age_sec": _int_value(mesh_scaling_decision.get("age_sec", -1), -1),
            }
            if mesh_action == "scale_down":
                lane_scaling["allowed"] = False
                lane_scaling["allowed_parallel_lanes"] = 0
            else:
                lane_scaling["allowed"] = True
                lane_scaling["allowed_parallel_lanes"] = max(
                    1,
                    _int_value(lane_scaling.get("allowed_parallel_lanes", 1), 1) + mesh_parallel_limit_boost,
                )
            scaling_by_lane[lane_id] = lane_scaling
    desired_running_by_parallel_key: dict[str, set[str]] = {}
    started: list[dict[str, Any]] = []
    scaled_up: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    config_failures = _lane_load_error_entries(load_errors) if requested_lane is None else []
    failed: list[dict[str, Any]] = list(config_failures)

    def _parallel_lane_context(lane_payload: dict[str, Any]) -> dict[str, Any]:
        lane_identity = _parallel_identity(lane_payload)
        lane_key = lane_identity["key"]
        lane_plan = parallel_by_lane.get(str(lane_payload.get("id", "")).strip(), {})
        if isinstance(lane_plan, dict) and str(lane_plan.get("parallel_key", "")).strip():
            lane_key = str(lane_plan.get("parallel_key")).strip()
            lane_identity["owner"], lane_identity["model"], lane_identity["endpoint_key"] = _split_parallel_key(lane_key)
        group = parallel_groups.get(lane_key, {}) if isinstance(parallel_groups.get(lane_key, {}), dict) else {}
        limit = max(
            1,
            _int_value(
                group.get("effective_limit", config.parallel_capacity_default_limit),
                config.parallel_capacity_default_limit,
            ),
        )
        limit += mesh_parallel_limit_boost
        return {
            "parallel_key": lane_key,
            "parallel_provider": lane_identity["owner"],
            "parallel_model": lane_identity["model"],
            "parallel_endpoint": lane_identity.get("endpoint_key", ""),
            "parallel_limit": limit,
        }

    if resolved_lane is None:
        lane_ids_by_parallel_key: dict[str, list[str]] = {}
        for lane in selected:
            lane_id = str(lane.get("id", "")).strip()
            if not lane_id:
                continue
            lane_scaling = scaling_by_lane.get(lane_id, {})
            if not bool(lane_scaling.get("allowed", True)):
                continue
            lane_context = _parallel_lane_context(lane)
            lane_ids_by_parallel_key.setdefault(lane_context["parallel_key"], []).append(lane_id)
        rotation_tick = int(dt.datetime.now(dt.timezone.utc).timestamp() // 60)
        for parallel_key, lane_ids in lane_ids_by_parallel_key.items():
            if not lane_ids:
                continue
            group = parallel_groups.get(parallel_key, {}) if isinstance(parallel_groups.get(parallel_key, {}), dict) else {}
            limit = max(
                1,
                _int_value(
                    group.get("effective_limit", config.parallel_capacity_default_limit),
                    config.parallel_capacity_default_limit,
                ),
            )
            prioritized_all = sorted(
                lane_ids,
                key=lambda lane_name: (
                    _int_value(scaling_by_lane.get(lane_name, {}).get("slot", 1), 1),
                    _int_value(scaling_by_lane.get(lane_name, {}).get("rank", 1), 1),
                    lane_name,
                ),
            )
            if len(prioritized_all) <= limit:
                desired_running_by_parallel_key[parallel_key] = set(prioritized_all)
                continue
            offset = rotation_tick % len(prioritized_all)
            rotated = prioritized_all[offset:] + prioritized_all[:offset]
            desired_running_by_parallel_key[parallel_key] = set(rotated[:limit])

    start_order = list(selected)
    if resolved_lane is None:
        start_order.sort(
            key=lambda lane: (
                0
                if str(lane.get("id", "")).strip()
                in desired_running_by_parallel_key.get(
                    _parallel_lane_context(lane)["parallel_key"],
                    set(),
                )
                else 1,
                _int_value(
                    scaling_by_lane.get(str(lane.get("id", "")).strip(), {}).get("slot", 1),
                    1,
                ),
                _int_value(
                    scaling_by_lane.get(str(lane.get("id", "")).strip(), {}).get("rank", 1),
                    1,
                ),
                str(lane.get("id", "")).strip(),
            )
        )

    for lane in start_order:
        lane_context = _parallel_lane_context(lane)
        parallel_key = lane_context["parallel_key"]
        parallel_limit = lane_context["parallel_limit"]
        current = by_id.get(str(lane.get("id", "")).strip(), {})
        lane_running = bool(current.get("running", False))
        current_parallel_running = max(0, _int_value(running_by_parallel_key.get(parallel_key, 0), 0))
        lane_scaling = scaling_by_lane.get(str(lane.get("id", "")).strip(), {})
        if not bool(lane_scaling.get("allowed", True)):
            skipped.append(
                {
                    "id": str(lane.get("id", "")).strip(),
                    "owner": str(lane.get("owner", "unknown")).strip() or "unknown",
                    "reason": str(lane_scaling.get("reason", "npv_gate_hold")).strip() or "npv_gate_hold",
                    "group": str(lane_scaling.get("group", "")).strip(),
                }
            )
            continue
        if not lane_running and current_parallel_running >= parallel_limit:
            skipped.append(
                {
                    "id": str(lane.get("id", "")).strip(),
                    "owner": str(lane.get("owner", "unknown")).strip() or "unknown",
                    "reason": "provider_model_parallel_limit",
                    "provider": lane_context["parallel_provider"],
                    "model": lane_context["parallel_model"],
                    "parallel_key": parallel_key,
                    "parallel_limit": parallel_limit,
                    "running_count": current_parallel_running,
                }
            )
            _append_lane_event(
                config,
                str(lane.get("id", "")).strip(),
                "scale_hold",
                {
                    "reason": "provider_model_parallel_limit",
                    "provider": lane_context["parallel_provider"],
                    "model": lane_context["parallel_model"],
                    "parallel_key": parallel_key,
                    "parallel_limit": parallel_limit,
                    "running_count": current_parallel_running,
                },
            )
            continue
        if not lane_running and swarm_budget_hard_stop:
            skipped.append(
                {
                    "id": str(lane.get("id", "")).strip(),
                    "owner": str(lane.get("owner", "unknown")).strip() or "unknown",
                    "reason": "swarm_daily_budget_cap",
                    "provider": lane_context["parallel_provider"],
                    "model": lane_context["parallel_model"],
                    "parallel_key": parallel_key,
                    "parallel_limit": parallel_limit,
                    "running_count": current_parallel_running,
                    "daily_spend_usd": _float_value(swarm_budget.get("daily_spend_usd", 0.0), 0.0),
                    "daily_budget_usd": _float_value(swarm_budget.get("daily_budget_usd", 0.0), 0.0),
                }
            )
            _append_lane_event(
                config,
                str(lane.get("id", "")).strip(),
                "scale_hold",
                {
                    "reason": "swarm_daily_budget_cap",
                    "provider": lane_context["parallel_provider"],
                    "model": lane_context["parallel_model"],
                    "parallel_key": parallel_key,
                    "parallel_limit": parallel_limit,
                    "running_count": current_parallel_running,
                    "daily_spend_usd": _float_value(swarm_budget.get("daily_spend_usd", 0.0), 0.0),
                    "daily_budget_usd": _float_value(swarm_budget.get("daily_budget_usd", 0.0), 0.0),
                },
            )
            continue
        try:
            lane_payload = start_lane_background(config, lane["id"])
            started.append(lane_payload)
            if not lane_running:
                running_by_parallel_key[parallel_key] = current_parallel_running + 1
            if (
                str(lane_scaling.get("mode", "static")).strip().lower() == "npv"
                and _int_value(lane_scaling.get("slot", 1), 1) > 1
                and _int_value(lane_scaling.get("allowed_parallel_lanes", 1), 1) > 1
            ):
                scale_payload = {
                    "group": str(lane_scaling.get("group", "")).strip(),
                    "slot": _int_value(lane_scaling.get("slot", 1), 1),
                    "allowed_parallel_lanes": _int_value(lane_scaling.get("allowed_parallel_lanes", 1), 1),
                    "reason": str(lane_scaling.get("reason", "approved")).strip() or "approved",
                    "reasons": lane_scaling.get("reasons", []),
                    "decision": lane_scaling.get("decision", {}),
                }
                _append_lane_event(config, lane["id"], "scale_up", scale_payload)
                scaled_up.append(
                    {
                        "id": lane["id"],
                        "status": "scaled_up",
                        "group": scale_payload["group"],
                        "slot": scale_payload["slot"],
                    }
                )
        except Exception as err:
            failed.append({"id": lane["id"], "owner": lane["owner"], "error": str(err), "source": "lane_runtime"})
    result = {
        "timestamp": _now_iso(),
        "requested_lane": resolved_lane or "all_enabled",
        "started_count": len(started),
        "scaled_up_count": len(scaled_up),
        "skipped_count": len(skipped),
        "started": started,
        "scaled_up": scaled_up,
        "skipped": skipped,
        "config_error_count": len(config_failures),
        "config_errors": [item["error"] for item in config_failures],
        "failed_count": len(failed),
        "scaling": scaling_plan,
        "parallel_capacity": parallel_plan,
        "failed": failed,
        "ok": len(failed) == 0,
        "mesh_scaling_decision": mesh_scaling_decision,
        "mesh_command_effects": mesh_command_effects,
        "swarm_daily_budget": swarm_budget,
    }
    _emit_mesh_event(
        config,
        topic="scheduling",
        event_type="lanes.start.summary",
        payload={
            "requested_lane": result["requested_lane"],
            "started_count": result["started_count"],
            "scaled_up_count": result["scaled_up_count"],
            "skipped_count": result["skipped_count"],
            "failed_count": result["failed_count"],
            "parallel_groups_at_limit": sum(
                1
                for payload in (
                    result.get("parallel_capacity", {}).get("groups", {}).values()
                    if isinstance(result.get("parallel_capacity", {}).get("groups", {}), dict)
                    else []
                )
                if isinstance(payload, dict)
                and _int_value(payload.get("running_count", 0), 0) >= _int_value(payload.get("effective_limit", 1), 1)
            ),
            "ok": result["ok"],
        },
    )
    for item in started:
        _emit_mesh_event(
            config,
            topic="scheduling",
            event_type="lane.start_requested",
            payload={"lane_id": str(item.get("id", "")).strip(), "running": bool(item.get("running", False))},
        )
    for item in failed:
        _emit_mesh_event(
            config,
            topic="monitoring",
            event_type="lane.start_failed",
            payload={"lane_id": str(item.get("id", "")).strip(), "error": str(item.get("error", "")).strip()},
        )
    _dispatch_mesh_events(config, max_events=64)
    return result


def stop_lanes_background(config: ManagerConfig, lane_id: str | None = None) -> dict[str, Any]:
    requested_lane = lane_id.strip() if isinstance(lane_id, str) and lane_id.strip() else None
    lanes, load_errors = _load_lane_specs_resilient(config)
    by_lane_id = {str(lane["id"]): lane for lane in lanes}
    resolved_lane = _resolve_requested_lane_id(lanes, requested_lane or "") if requested_lane is not None else None
    if requested_lane is not None and resolved_lane is None:
        raise RuntimeError(f"Unknown lane id {requested_lane!r}. Update {config.lanes_file}.")

    selected_ids = [resolved_lane] if resolved_lane is not None else [str(lane["id"]) for lane in lanes]
    config_failures = _lane_load_error_entries(load_errors) if requested_lane is None else []
    skipped: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = list(config_failures)
    status = lane_status_snapshot(config)
    status_by_id = {
        str(item.get("id", "")).strip(): item
        for item in status.get("lanes", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    for current_lane_id in selected_ids:
        lane = by_lane_id.get(current_lane_id, {})
        lane_enabled = bool(lane.get("enabled", False))
        lane_running = bool(status_by_id.get(current_lane_id, {}).get("running", False))
        if not lane_enabled and not lane_running:
            skipped.append({"id": current_lane_id, "reason": "disabled"})
            continue
        try:
            stopped.append(
                stop_lane_background(
                    config,
                    current_lane_id,
                    reason="manual",
                    pause=lane_enabled,
                )
            )
        except Exception as err:
            failed.append(
                {
                    "id": current_lane_id,
                    "owner": str(lane.get("owner", "unknown")).strip() or "unknown",
                    "error": str(err),
                    "source": "lane_runtime",
                }
            )
    result = {
        "timestamp": _now_iso(),
        "requested_lane": resolved_lane or "all",
        "stopped_count": len(stopped),
        "stopped": stopped,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "config_error_count": len(config_failures),
        "config_errors": [item["error"] for item in config_failures],
        "failed_count": len(failed),
        "failed": failed,
        "ok": len(failed) == 0,
    }
    _emit_mesh_event(
        config,
        topic="scheduling",
        event_type="lanes.stop.summary",
        payload={
            "requested_lane": result["requested_lane"],
            "stopped_count": result["stopped_count"],
            "skipped_count": result["skipped_count"],
            "failed_count": result["failed_count"],
            "ok": result["ok"],
        },
    )
    for item in stopped:
        _emit_mesh_event(
            config,
            topic="scheduling",
            event_type="lane.stopped",
            payload={"lane_id": str(item.get("id", "")).strip(), "running": bool(item.get("running", False))},
        )
    for item in failed:
        _emit_mesh_event(
            config,
            topic="monitoring",
            event_type="lane.stop_failed",
            payload={"lane_id": str(item.get("id", "")).strip(), "error": str(item.get("error", "")).strip()},
        )
    _dispatch_mesh_events(config, max_events=64)
    return result


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
    lane_owner_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    event = dict(item)
    event["source"] = str(source_path)
    event["source_kind"] = source_kind

    normalized_lane = str(event.get("lane_id", "")).strip()
    if lane_id and not normalized_lane:
        event["lane_id"] = lane_id
        normalized_lane = lane_id

    normalized_owner = str(event.get("owner", "")).strip()
    if owner and not normalized_owner:
        event["owner"] = owner
        normalized_owner = owner
    if not normalized_owner and normalized_lane and lane_owner_map:
        inferred_owner = str(lane_owner_map.get(normalized_lane, "")).strip()
        if inferred_owner:
            event["owner"] = inferred_owner
            normalized_owner = inferred_owner
    if not normalized_owner:
        event["owner"] = "unknown"

    if source_kind == "lane_events" and not str(event.get("content", "")).strip():
        event["content"] = _conversation_content_from_lane_event(event)

    return event


def _conversation_event_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    raw = str(item.get("timestamp", "")).strip()
    parsed = _parse_iso_timestamp(raw)
    if parsed is None:
        # Keep invalid timestamps ordered by source sequence (stable sort).
        return (0, float("-inf"), "")
    return (1, parsed.timestamp(), "")


def _task_done_events_last_24h(source_reports: list[dict[str, Any]]) -> dict[str, Any]:
    now_utc = _now_utc()
    window_start = now_utc - dt.timedelta(hours=24)
    by_owner: dict[str, int] = {}
    unique_tasks: set[tuple[str, str, str]] = set()
    seen_events: set[tuple[str, str, str, str, str, str]] = set()
    errors: list[str] = []
    files_scanned = 0
    completed_events = 0

    for source in source_reports:
        if not isinstance(source, dict):
            continue
        resolved_raw = str(source.get("resolved_path", "")).strip() or str(source.get("path", "")).strip()
        if not resolved_raw:
            continue
        source_path = Path(resolved_raw)
        if not source_path.exists() or not source_path.is_file():
            continue
        files_scanned += 1
        source_owner = str(source.get("owner", "")).strip() or "unknown"
        source_lane_id = str(source.get("lane_id", "")).strip()
        try:
            with source_path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if str(payload.get("event_type", "")).strip().lower() != "task_done":
                        continue
                    parsed_ts = _parse_iso_timestamp(payload.get("timestamp"))
                    if parsed_ts is None or parsed_ts < window_start or parsed_ts > now_utc:
                        continue
                    owner = str(payload.get("owner", "")).strip() or source_owner
                    lane_id = str(payload.get("lane_id", "")).strip() or source_lane_id
                    task_id = str(payload.get("task_id", "")).strip()
                    cycle = str(payload.get("cycle", "")).strip()
                    event_key = (
                        str(source_path),
                        parsed_ts.isoformat(),
                        owner,
                        lane_id,
                        task_id,
                        cycle,
                    )
                    if event_key in seen_events:
                        continue
                    seen_events.add(event_key)
                    completed_events += 1
                    by_owner[owner] = by_owner.get(owner, 0) + 1
                    if task_id:
                        unique_tasks.add((owner, lane_id, task_id))
        except Exception as err:
            errors.append(f"{source_path}: {err}")

    return {
        "window_start": window_start.isoformat(),
        "window_end": now_utc.isoformat(),
        "completed_events": completed_events,
        "unique_task_count": len(unique_tasks),
        "by_owner": by_owner,
        "files_scanned": files_scanned,
        "errors": errors,
        "ok": len(errors) == 0,
    }


def conversations_snapshot(config: ManagerConfig, lines: int = 200, include_lanes: bool = True) -> dict[str, Any]:
    line_limit = max(1, min(2000, int(lines)))
    lane_owner_map: dict[str, str] = {}
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
    lane_error_sources: list[dict[str, Any]] = []
    if include_lanes:
        lanes, lane_errors = _load_lane_specs_resilient(config)
        errors.extend(f"lane_specs: {err}" for err in lane_errors)
        for lane_error in _lane_load_error_entries(lane_errors):
            lane_error_sources.append(
                {
                    "path": str(config.lanes_file),
                    "resolved_path": str(config.lanes_file),
                    "kind": "lane_config",
                    "resolved_kind": "lane_config",
                    "lane_id": str(lane_error.get("id", "")).strip(),
                    "owner": str(lane_error.get("owner", "unknown")).strip() or "unknown",
                    "ok": False,
                    "missing": False,
                    "recoverable_missing": False,
                    "fallback_used": False,
                    "error": str(lane_error.get("error", "")).strip(),
                    "event_count": 0,
                }
            )
        for lane in lanes:
            lane_file = Path(lane["conversation_log_file"])
            lane_path = str(lane_file)
            lane_id = str(lane["id"])
            lane_owner = str(lane["owner"]).strip() or "unknown"
            lane_owner_map[lane_id] = lane_owner
            exists = any(item["path"] == lane_path for item in source_specs)
            if not exists:
                source_specs.append(
                    {
                        "path": lane_path,
                        "kind": "lane",
                        "lane_id": lane_id,
                        "owner": lane_owner,
                        "fallback_path": str(_lane_events_file(config, lane_id)),
                    }
                )

    events: list[dict[str, Any]] = []
    source_reports: list[dict[str, Any]] = []
    for source in source_specs:
        source_path = Path(source["path"])
        fallback_path_raw = str(source.get("fallback_path", "")).strip()
        fallback_path = Path(fallback_path_raw) if fallback_path_raw else None
        source_events: list[dict[str, Any]] = []
        source_error = ""
        source_ok = True
        recoverable_missing = False
        source_kind = source["kind"]
        resolved_path = source_path
        fallback_used = False
        missing = False
        if source_path.exists():
            try:
                source_events = _tail_ndjson(source_path, line_limit)
            except Exception as err:
                source_error = str(err)
                if source["kind"] == "lane" and fallback_path and fallback_path.exists():
                    try:
                        source_events = _tail_ndjson(fallback_path, line_limit)
                        resolved_path = fallback_path
                        source_kind = "lane_events"
                        fallback_used = True
                        source_error = f"{source_error} (fallback lane events used)"
                    except Exception as fallback_err:
                        source_error = f"{source_error}; fallback failed: {fallback_err}"
                source_ok = False
                errors.append(f"{source['path']}: {source_error}")
        else:
            missing = True
            if fallback_path and fallback_path.exists():
                try:
                    source_events = _tail_ndjson(fallback_path, line_limit)
                    resolved_path = fallback_path
                    source_kind = "lane_events"
                    fallback_used = True
                    if source["kind"] == "lane":
                        recoverable_missing = True
                except Exception as err:
                    source_ok = False
                    source_error = str(err)
                    errors.append(f"{source['path']}: {source_error}")
            elif source["kind"] == "lane":
                # A lane may not have produced logs yet; missing lane files are recoverable.
                recoverable_missing = True
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
                    lane_owner_map=lane_owner_map,
                )
            )
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
                "recoverable_missing": recoverable_missing,
                "fallback_used": fallback_used,
                "error": source_error,
                "event_count": len(source_events),
            }
        )
    source_reports.extend(lane_error_sources)
    events = sorted(events, key=_conversation_event_sort_key)[-line_limit:]

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
