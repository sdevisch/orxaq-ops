"""Dashboard action system — mutations, confirmations, and audit logging.

Provides operator actions for steering the swarm pipeline from the dashboard:
- Task management (reprioritize, unblock, clear errors)
- Breakglass grant lifecycle (issue, revoke)
- Git lock healing
- Runner lifecycle (start, stop, ensure)

All actions are audit-logged, rate-limited, and optionally require two-phase
confirmation (dry-run preview → confirm with token).

Zero external dependencies beyond the Python standard library.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _read_json(path: Path) -> dict[str, Any] | list[Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, (dict, list)) else {}


def _read_json_dict(path: Path) -> dict[str, Any]:
    result = _read_json(path)
    return result if isinstance(result, dict) else {}


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Atomic JSON write using tempfile + fsync + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
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


def _read_ndjson(path: Path, *, tail: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if tail > 0:
        lines = lines[-tail:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    return out


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


TASK_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")
VALID_DASHBOARD_STATUSES = {"pending", "blocked", "done"}
MAX_TTL_MINUTES = 1440  # 24 hours


# ── Audit Log ────────────────────────────────────────────────────────────────

class ActionAuditLog:
    """Append-only NDJSON audit log for dashboard actions."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def log(self, entry: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, default=str, sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)

    def recent(self, tail: int = 100) -> list[dict[str, Any]]:
        return _read_ndjson(self._path, tail=tail)

    @property
    def path(self) -> Path:
        return self._path


# ── Confirmation Tokens ──────────────────────────────────────────────────────

_PENDING_CONFIRMATIONS: dict[str, dict[str, Any]] = {}


def generate_confirm_token(action_id: str, params: dict[str, Any]) -> str:
    """Generate a single-use confirmation token valid for 5 minutes."""
    nonce = secrets.token_hex(8)
    token = f"tok-{nonce}"
    _PENDING_CONFIRMATIONS[token] = {
        "action_id": action_id,
        "params_json": json.dumps(params, sort_keys=True, default=str),
        "created_at": time.time(),
        "expires_at": time.time() + 300,
    }
    # Clean up expired tokens while we're here
    _cleanup_expired_tokens()
    return token


def validate_confirm_token(token: str, action_id: str) -> bool:
    """Validate and consume a confirmation token (single-use)."""
    entry = _PENDING_CONFIRMATIONS.get(token)
    if not entry:
        return False
    if time.time() > entry["expires_at"]:
        del _PENDING_CONFIRMATIONS[token]
        return False
    if entry["action_id"] != action_id:
        return False
    del _PENDING_CONFIRMATIONS[token]
    return True


def _cleanup_expired_tokens() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING_CONFIRMATIONS.items() if now > v["expires_at"]]
    for k in expired:
        del _PENDING_CONFIRMATIONS[k]


# ── Rate Limiting ────────────────────────────────────────────────────────────

_RATE_WINDOWS: dict[str, list[float]] = {}
RATE_LIMIT_GLOBAL = 30
RATE_LIMIT_HIGH = 5


def check_rate_limit(risk_level: str) -> str | None:
    """Returns error message if rate-limited, else None."""
    now = time.time()
    cutoff = now - 60.0

    _RATE_WINDOWS.setdefault("global", [])
    _RATE_WINDOWS["global"] = [t for t in _RATE_WINDOWS["global"] if t > cutoff]
    if len(_RATE_WINDOWS["global"]) >= RATE_LIMIT_GLOBAL:
        return "Rate limit exceeded (30 actions/minute)"

    if risk_level == "high":
        _RATE_WINDOWS.setdefault("high", [])
        _RATE_WINDOWS["high"] = [t for t in _RATE_WINDOWS["high"] if t > cutoff]
        if len(_RATE_WINDOWS["high"]) >= RATE_LIMIT_HIGH:
            return "Rate limit exceeded for high-risk actions (5/minute)"

    _RATE_WINDOWS["global"].append(now)
    if risk_level == "high":
        _RATE_WINDOWS.setdefault("high", []).append(now)
    return None


def reset_rate_limits() -> None:
    """Reset rate limits (for testing)."""
    _RATE_WINDOWS.clear()


# ── Action Context ───────────────────────────────────────────────────────────

@dataclass
class ActionContext:
    """Immutable context for action execution."""
    artifacts_dir: Path
    repo_dir: Path | None
    state_file: Path
    config_tasks_file: Path
    audit_log: ActionAuditLog


@dataclass
class ActionResult:
    """Result of an action execution."""
    ok: bool
    action_id: str
    dry_run: bool
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    audit_id: str | None = None
    confirm_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action_id": self.action_id,
            "dry_run": self.dry_run,
            "message": self.message,
            "detail": self.detail,
            "audit_id": self.audit_id,
            "confirm_token": self.confirm_token,
        }


def build_context(artifacts_dir: Path, repo_dir: Path | None = None) -> ActionContext:
    """Build action context from resolved paths."""
    repo_root = artifacts_dir.parent
    return ActionContext(
        artifacts_dir=artifacts_dir,
        repo_dir=repo_dir,
        state_file=repo_root / "state" / "state.json",
        config_tasks_file=repo_root / "config" / "tasks.json",
        audit_log=ActionAuditLog(artifacts_dir / "autonomy" / "dashboard_actions_audit.ndjson"),
    )


# ── Action Definitions ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActionDef:
    action_id: str
    risk_level: str  # "low" | "medium" | "high"
    description: str
    requires_confirmation: bool
    param_schema: dict[str, str]  # param_name -> description
    handler: Callable[[ActionContext, dict[str, Any], bool], ActionResult]


# ── Action Handlers ──────────────────────────────────────────────────────────

def _load_state(ctx: ActionContext) -> dict[str, dict[str, Any]]:
    """Load task state from state.json."""
    data = _read_json_dict(ctx.state_file)
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _load_tasks(ctx: ActionContext) -> list[dict[str, Any]]:
    """Load tasks from config/tasks.json."""
    data = _read_json(ctx.config_tasks_file)
    return data if isinstance(data, list) else []


# ── task.reprioritize ────────────────────────────────────────────────────────

def action_task_reprioritize(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    task_id = str(params.get("task_id", ""))
    new_priority = params.get("new_priority")

    if not task_id or not TASK_ID_PATTERN.match(task_id):
        return ActionResult(ok=False, action_id="task.reprioritize", dry_run=dry_run,
                            message=f"Invalid task_id: {task_id!r}")

    try:
        new_priority = int(new_priority)
    except (TypeError, ValueError):
        return ActionResult(ok=False, action_id="task.reprioritize", dry_run=dry_run,
                            message=f"Invalid priority: {new_priority!r}")
    if new_priority < 0 or new_priority > 999:
        return ActionResult(ok=False, action_id="task.reprioritize", dry_run=dry_run,
                            message="Priority must be 0-999")

    tasks = _load_tasks(ctx)
    match = None
    old_priority = None
    for t in tasks:
        if isinstance(t, dict) and str(t.get("id", "")) == task_id:
            match = t
            old_priority = t.get("priority")
            break

    if match is None:
        return ActionResult(ok=False, action_id="task.reprioritize", dry_run=dry_run,
                            message=f"Task not found in config: {task_id}")

    if dry_run:
        return ActionResult(ok=True, action_id="task.reprioritize", dry_run=True,
                            message=f"Would change {task_id} priority from {old_priority} to {new_priority}",
                            detail={"task_id": task_id, "old_priority": old_priority, "new_priority": new_priority})

    match["priority"] = new_priority
    _write_json_atomic(ctx.config_tasks_file, tasks)

    return ActionResult(ok=True, action_id="task.reprioritize", dry_run=False,
                        message=f"Task {task_id} priority changed from {old_priority} to {new_priority}",
                        detail={"task_id": task_id, "old_priority": old_priority, "new_priority": new_priority})


# ── task.update-status ───────────────────────────────────────────────────────

def action_task_update_status(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    task_id = str(params.get("task_id", ""))
    new_status = str(params.get("new_status", ""))

    if not task_id or not TASK_ID_PATTERN.match(task_id):
        return ActionResult(ok=False, action_id="task.update-status", dry_run=dry_run,
                            message=f"Invalid task_id: {task_id!r}")

    if new_status not in VALID_DASHBOARD_STATUSES:
        return ActionResult(ok=False, action_id="task.update-status", dry_run=dry_run,
                            message=f"Invalid status: {new_status!r}. Allowed: {', '.join(sorted(VALID_DASHBOARD_STATUSES))}")

    state = _load_state(ctx)
    if task_id not in state:
        return ActionResult(ok=False, action_id="task.update-status", dry_run=dry_run,
                            message=f"Task not found in state: {task_id}")

    current_status = state[task_id].get("status", "unknown")

    if dry_run:
        return ActionResult(ok=True, action_id="task.update-status", dry_run=True,
                            message=f"Would change {task_id} from {current_status} to {new_status}",
                            detail={"task_id": task_id, "current_status": current_status, "new_status": new_status})

    state[task_id]["status"] = new_status
    state[task_id]["last_update"] = _utc_now_iso()
    if new_status == "pending":
        state[task_id]["last_error"] = ""
        state[task_id]["not_before"] = ""
    _write_json_atomic(ctx.state_file, state)

    return ActionResult(ok=True, action_id="task.update-status", dry_run=False,
                        message=f"Task {task_id} status changed from {current_status} to {new_status}",
                        detail={"task_id": task_id, "previous_status": current_status, "new_status": new_status})


# ── task.clear-error ─────────────────────────────────────────────────────────

def action_task_clear_error(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    task_id = str(params.get("task_id", ""))

    if not task_id or not TASK_ID_PATTERN.match(task_id):
        return ActionResult(ok=False, action_id="task.clear-error", dry_run=dry_run,
                            message=f"Invalid task_id: {task_id!r}")

    state = _load_state(ctx)
    if task_id not in state:
        return ActionResult(ok=False, action_id="task.clear-error", dry_run=dry_run,
                            message=f"Task not found in state: {task_id}")

    entry = state[task_id]
    old_error = entry.get("last_error", "")
    old_failures = entry.get("retryable_failures", 0)

    if dry_run:
        return ActionResult(ok=True, action_id="task.clear-error", dry_run=True,
                            message=f"Would clear error for {task_id} (failures: {old_failures})",
                            detail={"task_id": task_id, "current_error": old_error[:200],
                                    "retryable_failures": old_failures})

    entry["last_error"] = ""
    entry["retryable_failures"] = 0
    entry["not_before"] = ""
    entry["last_update"] = _utc_now_iso()
    if entry.get("status") == "blocked":
        entry["status"] = "pending"
    _write_json_atomic(ctx.state_file, state)

    return ActionResult(ok=True, action_id="task.clear-error", dry_run=False,
                        message=f"Cleared error for {task_id} and reset to pending",
                        detail={"task_id": task_id, "cleared_error": old_error[:200],
                                "cleared_failures": old_failures})


# ── breakglass.grant ─────────────────────────────────────────────────────────

def action_breakglass_grant(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    reason = str(params.get("reason", "")).strip()
    scope = str(params.get("scope", "")).strip()
    ttl_minutes = params.get("ttl_minutes", 20)
    task_allowlist = params.get("task_allowlist", [])

    if not reason:
        return ActionResult(ok=False, action_id="breakglass.grant", dry_run=dry_run,
                            message="reason is required")
    if not scope:
        return ActionResult(ok=False, action_id="breakglass.grant", dry_run=dry_run,
                            message="scope is required")
    try:
        ttl_minutes = int(ttl_minutes)
    except (TypeError, ValueError):
        return ActionResult(ok=False, action_id="breakglass.grant", dry_run=dry_run,
                            message=f"Invalid ttl_minutes: {ttl_minutes!r}")
    if ttl_minutes < 1 or ttl_minutes > MAX_TTL_MINUTES:
        return ActionResult(ok=False, action_id="breakglass.grant", dry_run=dry_run,
                            message=f"ttl_minutes must be 1-{MAX_TTL_MINUTES}")
    if not isinstance(task_allowlist, list):
        task_allowlist = []

    now = _utc_now()
    grant_id = f"bg-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    expires_at = now + timedelta(minutes=ttl_minutes)

    grant = {
        "grant_id": grant_id,
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "requested_by": "dashboard_operator",
        "approved_by": "dashboard_operator",
        "reason": reason,
        "scope": scope,
        "ttl_minutes": ttl_minutes,
        "task_allowlist": task_allowlist,
        "rollback_proof": "Grant will auto-expire; revoke via dashboard if needed.",
    }

    if dry_run:
        token = generate_confirm_token("breakglass.grant", params)
        return ActionResult(ok=True, action_id="breakglass.grant", dry_run=True,
                            message=f"Would issue breakglass grant ({ttl_minutes}m TTL)",
                            detail=grant, confirm_token=token)

    grants_dir = ctx.artifacts_dir / "autonomy" / "breakglass" / "grants"
    grants_dir.mkdir(parents=True, exist_ok=True)
    grant_path = grants_dir / f"{grant_id}.json"
    _write_json_atomic(grant_path, grant)

    # Also log to privilege_escalations.ndjson
    escalation_path = ctx.artifacts_dir / "autonomy" / "privilege_escalations.ndjson"
    escalation_path.parent.mkdir(parents=True, exist_ok=True)
    escalation_entry = json.dumps({
        "event_type": "breakglass_grant_created",
        "grant_id": grant_id,
        "timestamp": now.isoformat(),
        "reason": reason,
        "scope": scope,
        "source": "dashboard",
    }, default=str) + "\n"
    with escalation_path.open("a", encoding="utf-8") as f:
        f.write(escalation_entry)

    return ActionResult(ok=True, action_id="breakglass.grant", dry_run=False,
                        message=f"Breakglass grant {grant_id} issued ({ttl_minutes}m TTL)",
                        detail=grant)


# ── breakglass.revoke ────────────────────────────────────────────────────────

def action_breakglass_revoke(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    grant_id = str(params.get("grant_id", "")).strip()

    if not grant_id:
        return ActionResult(ok=False, action_id="breakglass.revoke", dry_run=dry_run,
                            message="grant_id is required")

    grants_dir = ctx.artifacts_dir / "autonomy" / "breakglass" / "grants"
    grant_path = grants_dir / f"{grant_id}.json"

    if not grant_path.exists():
        return ActionResult(ok=False, action_id="breakglass.revoke", dry_run=dry_run,
                            message=f"Grant not found: {grant_id}")

    grant_data = _read_json_dict(grant_path)

    if dry_run:
        token = generate_confirm_token("breakglass.revoke", params)
        return ActionResult(ok=True, action_id="breakglass.revoke", dry_run=True,
                            message=f"Would revoke grant {grant_id}",
                            detail=grant_data, confirm_token=token)

    grant_path.unlink(missing_ok=True)

    # Also update active_grant.json if it references this grant
    active_path = ctx.artifacts_dir / "autonomy" / "breakglass" / "active_grant.json"
    if active_path.exists():
        active = _read_json_dict(active_path)
        if active.get("grant_id") == grant_id:
            active_path.unlink(missing_ok=True)

    # Log to privilege_escalations.ndjson
    escalation_path = ctx.artifacts_dir / "autonomy" / "privilege_escalations.ndjson"
    escalation_path.parent.mkdir(parents=True, exist_ok=True)
    escalation_entry = json.dumps({
        "event_type": "breakglass_grant_revoked",
        "grant_id": grant_id,
        "timestamp": _utc_now_iso(),
        "source": "dashboard",
    }, default=str) + "\n"
    with escalation_path.open("a", encoding="utf-8") as f:
        f.write(escalation_entry)

    return ActionResult(ok=True, action_id="breakglass.revoke", dry_run=False,
                        message=f"Grant {grant_id} revoked",
                        detail={"grant_id": grant_id})


# ── git.heal-locks ───────────────────────────────────────────────────────────

def action_git_heal_locks(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    if not ctx.repo_dir:
        return ActionResult(ok=False, action_id="git.heal-locks", dry_run=dry_run,
                            message="repo_dir not configured")

    git_dir = ctx.repo_dir / ".git"
    if not git_dir.is_dir():
        return ActionResult(ok=False, action_id="git.heal-locks", dry_run=dry_run,
                            message="No .git directory found")

    lock_names = ("index.lock", "HEAD.lock", "packed-refs.lock")
    found: list[dict[str, Any]] = []
    now = time.time()

    for name in lock_names:
        lock_path = git_dir / name
        if lock_path.exists():
            try:
                age = now - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            found.append({"name": name, "age_sec": int(age), "path": str(lock_path)})

    if not found:
        return ActionResult(ok=True, action_id="git.heal-locks", dry_run=dry_run,
                            message="No stale git lock files found",
                            detail={"locks_found": 0})

    if dry_run:
        token = generate_confirm_token("git.heal-locks", params)
        return ActionResult(ok=True, action_id="git.heal-locks", dry_run=True,
                            message=f"Found {len(found)} git lock file(s) to remove",
                            detail={"locks": found}, confirm_token=token)

    removed: list[str] = []
    for lock in found:
        lock_path = Path(lock["path"])
        if lock_path.exists():
            lock_path.unlink(missing_ok=True)
            removed.append(lock["name"])

    return ActionResult(ok=True, action_id="git.heal-locks", dry_run=False,
                        message=f"Removed {len(removed)} git lock file(s): {', '.join(removed)}",
                        detail={"removed": removed})


# ── runner.release-lock ──────────────────────────────────────────────────────

def action_runner_release_lock(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    lock_path = ctx.artifacts_dir / "autonomy" / "runner.lock"

    if not lock_path.exists():
        return ActionResult(ok=True, action_id="runner.release-lock", dry_run=dry_run,
                            message="No runner lock file found",
                            detail={"lock_exists": False})

    lock_data = _read_json_dict(lock_path)
    lock_pid = int(lock_data.get("pid", 0)) if str(lock_data.get("pid", "")).isdigit() else 0
    pid_alive = _pid_is_running(lock_pid) if lock_pid else False

    if pid_alive:
        return ActionResult(ok=False, action_id="runner.release-lock", dry_run=dry_run,
                            message=f"Runner process is still alive (pid={lock_pid}). Cannot release lock.",
                            detail={"pid": lock_pid, "alive": True})

    if dry_run:
        token = generate_confirm_token("runner.release-lock", params)
        return ActionResult(ok=True, action_id="runner.release-lock", dry_run=True,
                            message=f"Would remove stale runner lock (pid={lock_pid}, not running)",
                            detail=lock_data, confirm_token=token)

    lock_path.unlink(missing_ok=True)

    return ActionResult(ok=True, action_id="runner.release-lock", dry_run=False,
                        message=f"Stale runner lock removed (pid={lock_pid})",
                        detail={"removed_pid": lock_pid})


# ── runner.stop ──────────────────────────────────────────────────────────────

def action_runner_stop(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    reason = str(params.get("reason", "Manual stop from dashboard")).strip()

    if dry_run:
        token = generate_confirm_token("runner.stop", params)
        lock_path = ctx.artifacts_dir / "autonomy" / "runner.lock"
        lock_data = _read_json_dict(lock_path)
        return ActionResult(ok=True, action_id="runner.stop", dry_run=True,
                            message=f"Would stop autonomy runner: {reason}",
                            detail={"reason": reason, "lock_data": lock_data},
                            confirm_token=token)

    try:
        result = subprocess.run(
            ["python3", "-m", "orxaq_autonomy", "--root", str(ctx.artifacts_dir.parent), "stop",
             "--reason", reason],
            capture_output=True, text=True, check=False, timeout=30,
            cwd=str(ctx.artifacts_dir.parent),
        )
        return ActionResult(ok=result.returncode == 0, action_id="runner.stop", dry_run=False,
                            message=f"Runner stop {'succeeded' if result.returncode == 0 else 'failed'}",
                            detail={"stdout": result.stdout[:500], "stderr": result.stderr[:500],
                                    "returncode": result.returncode, "reason": reason})
    except Exception as exc:
        return ActionResult(ok=False, action_id="runner.stop", dry_run=False,
                            message=f"Failed to stop runner: {exc}",
                            detail={"error": str(exc)})


# ── runner.start ─────────────────────────────────────────────────────────────

def action_runner_start(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    if dry_run:
        token = generate_confirm_token("runner.start", params)
        lock_path = ctx.artifacts_dir / "autonomy" / "runner.lock"
        lock_exists = lock_path.exists()
        return ActionResult(ok=True, action_id="runner.start", dry_run=True,
                            message="Would start autonomy runner",
                            detail={"lock_exists": lock_exists},
                            confirm_token=token)

    try:
        result = subprocess.run(
            ["python3", "-m", "orxaq_autonomy", "--root", str(ctx.artifacts_dir.parent), "start"],
            capture_output=True, text=True, check=False, timeout=30,
            cwd=str(ctx.artifacts_dir.parent),
        )
        return ActionResult(ok=result.returncode == 0, action_id="runner.start", dry_run=False,
                            message=f"Runner start {'succeeded' if result.returncode == 0 else 'failed'}",
                            detail={"stdout": result.stdout[:500], "stderr": result.stderr[:500],
                                    "returncode": result.returncode})
    except Exception as exc:
        return ActionResult(ok=False, action_id="runner.start", dry_run=False,
                            message=f"Failed to start runner: {exc}",
                            detail={"error": str(exc)})


# ── runner.ensure ────────────────────────────────────────────────────────────

def action_runner_ensure(ctx: ActionContext, params: dict[str, Any], dry_run: bool) -> ActionResult:
    if dry_run:
        token = generate_confirm_token("runner.ensure", params)
        lock_path = ctx.artifacts_dir / "autonomy" / "runner.lock"
        lock_data = _read_json_dict(lock_path)
        return ActionResult(ok=True, action_id="runner.ensure", dry_run=True,
                            message="Would ensure autonomy runner is alive (restart if stale)",
                            detail={"lock_data": lock_data},
                            confirm_token=token)

    try:
        result = subprocess.run(
            ["python3", "-m", "orxaq_autonomy", "--root", str(ctx.artifacts_dir.parent), "ensure"],
            capture_output=True, text=True, check=False, timeout=30,
            cwd=str(ctx.artifacts_dir.parent),
        )
        return ActionResult(ok=result.returncode == 0, action_id="runner.ensure", dry_run=False,
                            message=f"Runner ensure {'succeeded' if result.returncode == 0 else 'failed'}",
                            detail={"stdout": result.stdout[:500], "stderr": result.stderr[:500],
                                    "returncode": result.returncode})
    except Exception as exc:
        return ActionResult(ok=False, action_id="runner.ensure", dry_run=False,
                            message=f"Failed to ensure runner: {exc}",
                            detail={"error": str(exc)})


# ── Action Registry ──────────────────────────────────────────────────────────

ACTIONS: dict[str, ActionDef] = {
    "task.reprioritize": ActionDef(
        action_id="task.reprioritize",
        risk_level="low",
        description="Change a task's priority in the backlog",
        requires_confirmation=False,
        param_schema={"task_id": "Task identifier", "new_priority": "New priority (0-999, lower = higher)"},
        handler=action_task_reprioritize,
    ),
    "task.update-status": ActionDef(
        action_id="task.update-status",
        risk_level="low",
        description="Set a task's status (pending, blocked, done)",
        requires_confirmation=False,
        param_schema={"task_id": "Task identifier", "new_status": "New status: pending | blocked | done"},
        handler=action_task_update_status,
    ),
    "task.clear-error": ActionDef(
        action_id="task.clear-error",
        risk_level="low",
        description="Clear error state and reset task for retry",
        requires_confirmation=False,
        param_schema={"task_id": "Task identifier"},
        handler=action_task_clear_error,
    ),
    "breakglass.grant": ActionDef(
        action_id="breakglass.grant",
        risk_level="medium",
        description="Issue a time-limited breakglass privilege grant",
        requires_confirmation=True,
        param_schema={
            "reason": "Why the grant is needed",
            "scope": "What the grant allows",
            "ttl_minutes": "Grant duration in minutes (1-1440)",
            "task_allowlist": "List of task IDs allowed to use the grant",
        },
        handler=action_breakglass_grant,
    ),
    "breakglass.revoke": ActionDef(
        action_id="breakglass.revoke",
        risk_level="medium",
        description="Revoke an active breakglass grant",
        requires_confirmation=True,
        param_schema={"grant_id": "Grant ID to revoke"},
        handler=action_breakglass_revoke,
    ),
    "git.heal-locks": ActionDef(
        action_id="git.heal-locks",
        risk_level="medium",
        description="Remove stale git lock files (.git/index.lock etc.)",
        requires_confirmation=True,
        param_schema={},
        handler=action_git_heal_locks,
    ),
    "runner.release-lock": ActionDef(
        action_id="runner.release-lock",
        risk_level="medium",
        description="Remove stale runner lock when process is dead",
        requires_confirmation=True,
        param_schema={},
        handler=action_runner_release_lock,
    ),
    "runner.stop": ActionDef(
        action_id="runner.stop",
        risk_level="high",
        description="Stop the autonomy runner",
        requires_confirmation=True,
        param_schema={"reason": "Reason for stopping (optional)"},
        handler=action_runner_stop,
    ),
    "runner.start": ActionDef(
        action_id="runner.start",
        risk_level="high",
        description="Start the autonomy runner",
        requires_confirmation=True,
        param_schema={},
        handler=action_runner_start,
    ),
    "runner.ensure": ActionDef(
        action_id="runner.ensure",
        risk_level="high",
        description="Ensure runner is alive, restart if stale",
        requires_confirmation=True,
        param_schema={},
        handler=action_runner_ensure,
    ),
}


def get_action_catalog() -> list[dict[str, Any]]:
    """Return the action catalog for the GET /api/v2/actions endpoint."""
    return [
        {
            "action_id": a.action_id,
            "risk_level": a.risk_level,
            "description": a.description,
            "requires_confirmation": a.requires_confirmation,
            "param_schema": a.param_schema,
        }
        for a in ACTIONS.values()
    ]


# ── Action Dispatcher ────────────────────────────────────────────────────────

def dispatch_action(
    *,
    action_id: str,
    params: dict[str, Any],
    confirm_token: str,
    dry_run: bool,
    artifacts_dir: Path,
    repo_dir: Path | None,
) -> ActionResult:
    """Main entry point for executing a dashboard action."""
    # Look up action
    action_def = ACTIONS.get(action_id)
    if not action_def:
        return ActionResult(ok=False, action_id=action_id, dry_run=dry_run,
                            message=f"Unknown action: {action_id}")

    # Rate limit check
    rate_error = check_rate_limit(action_def.risk_level)
    if rate_error:
        return ActionResult(ok=False, action_id=action_id, dry_run=dry_run,
                            message=rate_error)

    # Confirmation check for non-dry-run requests on actions that require confirmation
    if action_def.requires_confirmation and not dry_run:
        if not confirm_token:
            return ActionResult(
                ok=False, action_id=action_id, dry_run=False,
                message="This action requires confirmation. POST with dry_run=true first to preview, "
                        "then use the returned confirm_token to execute.")
        if not validate_confirm_token(confirm_token, action_id):
            return ActionResult(
                ok=False, action_id=action_id, dry_run=False,
                message="Invalid or expired confirmation token. Request a new preview with dry_run=true.")

    # Build context and execute
    ctx = build_context(artifacts_dir, repo_dir)
    start_time = time.time()

    try:
        result = action_def.handler(ctx, params, dry_run)
    except Exception as exc:
        result = ActionResult(ok=False, action_id=action_id, dry_run=dry_run,
                              message=f"Action failed: {exc}",
                              detail={"error": str(exc)})

    duration_ms = int((time.time() - start_time) * 1000)

    # Audit log (skip for dry-runs — only log actual executions)
    if not dry_run:
        audit_id = f"act-{_utc_now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        result.audit_id = audit_id
        ctx.audit_log.log({
            "audit_id": audit_id,
            "timestamp": _utc_now_iso(),
            "action_id": action_id,
            "risk_level": action_def.risk_level,
            "params": params,
            "dry_run": False,
            "result_ok": result.ok,
            "result_message": result.message,
            "duration_ms": duration_ms,
        })

    return result
