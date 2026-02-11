"""Deterministic breakglass state and immutable audit ledger helpers."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATE_ENV = "ORXAQ_BREAKGLASS_STATE_FILE"
LEDGER_ENV = "ORXAQ_BREAKGLASS_LEDGER_FILE"
TOKEN_ENV = "ORXAQ_BREAKGLASS_TOKEN"
ACTOR_ENV = "ORXAQ_BREAKGLASS_ACTOR"

DEFAULT_TTL_SEC = 1800
GENESIS_HASH = "GENESIS"
STATE_RELATIVE_PATH = Path("artifacts/governance/breakglass_state.json")
LEDGER_RELATIVE_PATH = Path("artifacts/governance/breakglass_ledger.ndjson")
SCOPE_PATTERN = "abcdefghijklmnopqrstuvwxyz0123456789._-"


@dataclass(frozen=True)
class BreakglassPaths:
    state_file: Path
    ledger_file: Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_path(repo_root: Path, raw: str, default: Path) -> Path:
    value = str(raw or "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    return path


def resolve_paths(repo_root: Path) -> BreakglassPaths:
    root = repo_root.resolve()
    state_file = _resolve_path(root, os.environ.get(STATE_ENV, ""), STATE_RELATIVE_PATH)
    ledger_file = _resolve_path(root, os.environ.get(LEDGER_ENV, ""), LEDGER_RELATIVE_PATH)
    return BreakglassPaths(state_file=state_file, ledger_file=ledger_file)


def _actor(default_actor: str = "unknown") -> str:
    explicit = str(os.environ.get(ACTOR_ENV, "")).strip()
    if explicit:
        return explicit
    user = str(os.environ.get("USER", "")).strip() or str(os.environ.get("USERNAME", "")).strip()
    host = socket.gethostname().strip()
    if user and host:
        return f"{user}@{host}"
    if user:
        return user
    return default_actor


def _read_state(paths: BreakglassPaths) -> dict[str, Any]:
    if not paths.state_file.exists():
        return {}
    try:
        raw = json.loads(paths.state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _write_state(paths: BreakglassPaths, payload: dict[str, Any]) -> None:
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _state_hash(payload: dict[str, Any]) -> str:
    return _sha256_text(_canonical_json(payload))


def _read_last_ledger_hash(ledger_file: Path) -> str:
    if not ledger_file.exists():
        return GENESIS_HASH
    last_hash = GENESIS_HASH
    with ledger_file.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise RuntimeError("breakglass ledger contains non-object JSON entry")
            entry_hash = str(entry.get("entry_hash", "")).strip()
            if not entry_hash:
                raise RuntimeError("breakglass ledger entry missing entry_hash")
            last_hash = entry_hash
    return last_hash


def _append_ledger(
    paths: BreakglassPaths,
    *,
    event: str,
    session_id: str,
    actor: str,
    details: dict[str, Any],
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    ts = timestamp or _utc_now()
    prev_hash = _read_last_ledger_hash(paths.ledger_file)
    base_payload = {
        "schema_version": 1,
        "timestamp": _iso_utc(ts),
        "event": event,
        "session_id": session_id,
        "actor": actor,
        "details": details,
    }
    entry_hash = _sha256_text(f"{prev_hash}\n{_canonical_json(base_payload)}")
    payload = dict(base_payload)
    payload["prev_hash"] = prev_hash
    payload["entry_hash"] = entry_hash

    paths.ledger_file.parent.mkdir(parents=True, exist_ok=True)
    with paths.ledger_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return payload


def _normalize_scopes(scopes: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for raw in scopes:
        scope = str(raw or "").strip().lower()
        if not scope:
            continue
        if any(ch not in SCOPE_PATTERN for ch in scope):
            raise ValueError(f"invalid breakglass scope {raw!r}")
        if scope not in normalized:
            normalized.append(scope)
    return normalized


def status(repo_root: Path) -> dict[str, Any]:
    paths = resolve_paths(repo_root)
    state = _read_state(paths)
    now = _utc_now()
    expires = _parse_utc(str(state.get("expires_at", "")))
    is_active = bool(state.get("active", False))
    is_expired = bool(is_active and expires is not None and expires <= now)
    is_valid = bool(is_active and not is_expired)

    payload = {
        "schema_version": 1,
        "state_file": str(paths.state_file),
        "ledger_file": str(paths.ledger_file),
        "active": is_active,
        "expired": is_expired,
        "valid": is_valid,
        "session_id": str(state.get("session_id", "")).strip(),
        "opened_at": str(state.get("opened_at", "")).strip(),
        "expires_at": str(state.get("expires_at", "")).strip(),
        "opened_by": str(state.get("opened_by", "")).strip(),
        "closed_at": str(state.get("closed_at", "")).strip(),
        "closed_by": str(state.get("closed_by", "")).strip(),
        "reason": str(state.get("reason", "")).strip(),
        "close_reason": str(state.get("close_reason", "")).strip(),
        "scopes": list(state.get("scopes", [])) if isinstance(state.get("scopes", []), list) else [],
        "token_present": bool(str(state.get("token_hash", "")).strip()),
        "token_hint": str(state.get("token_hint", "")).strip(),
        "last_updated_at": _iso_utc(now),
    }
    return payload


def open_session(
    repo_root: Path,
    *,
    scopes: list[str] | tuple[str, ...],
    reason: str,
    ttl_sec: int = DEFAULT_TTL_SEC,
    actor: str = "",
    token: str = "",
) -> dict[str, Any]:
    normalized_scopes = _normalize_scopes(scopes)
    if not normalized_scopes:
        raise ValueError("at least one breakglass scope is required")
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise ValueError("breakglass reason is required")
    ttl = max(1, int(ttl_sec))
    started_at = _utc_now()
    expires_at = started_at + timedelta(seconds=ttl)
    session_id = uuid.uuid4().hex
    actor_value = actor.strip() or _actor(default_actor="operator")
    token_value = token.strip() or secrets.token_urlsafe(32)
    token_hash = _sha256_text(token_value)
    token_hint = (
        f"{token_value[:4]}...{token_value[-4:]}"
        if len(token_value) > 8
        else f"{token_value[:2]}...{token_value[-1:]}"
    )

    paths = resolve_paths(repo_root)
    state = {
        "schema_version": 1,
        "active": True,
        "session_id": session_id,
        "opened_at": _iso_utc(started_at),
        "expires_at": _iso_utc(expires_at),
        "opened_by": actor_value,
        "reason": reason_text,
        "scopes": normalized_scopes,
        "token_hash": token_hash,
        "token_hint": token_hint,
        "closed_at": "",
        "closed_by": "",
        "close_reason": "",
    }
    _write_state(paths, state)
    state_digest = _state_hash(state)
    _append_ledger(
        paths,
        event="open",
        session_id=session_id,
        actor=actor_value,
        details={
            "reason": reason_text,
            "scopes": normalized_scopes,
            "expires_at": _iso_utc(expires_at),
            "state_hash": state_digest,
        },
        timestamp=started_at,
    )
    payload = status(repo_root)
    payload["ok"] = True
    payload["token"] = token_value
    payload["state_hash"] = state_digest
    return payload


def close_session(
    repo_root: Path,
    *,
    actor: str = "",
    reason: str = "",
    token: str = "",
    require_token: bool = False,
) -> dict[str, Any]:
    paths = resolve_paths(repo_root)
    state = _read_state(paths)
    actor_value = actor.strip() or _actor(default_actor="operator")
    now = _utc_now()
    if not state or not bool(state.get("active", False)):
        payload = status(repo_root)
        payload["ok"] = True
        payload["message"] = "breakglass already inactive"
        return payload

    state_token_hash = str(state.get("token_hash", "")).strip()
    token_value = str(token or "").strip() or str(os.environ.get(TOKEN_ENV, "")).strip()
    if require_token and not token_value:
        raise ValueError("breakglass token required to close session")
    if token_value and state_token_hash and _sha256_text(token_value) != state_token_hash:
        raise ValueError("breakglass token mismatch while closing session")

    state["active"] = False
    state["closed_at"] = _iso_utc(now)
    state["closed_by"] = actor_value
    state["close_reason"] = str(reason or "").strip()
    _write_state(paths, state)
    state_digest = _state_hash(state)
    _append_ledger(
        paths,
        event="close",
        session_id=str(state.get("session_id", "")).strip(),
        actor=actor_value,
        details={
            "close_reason": str(reason or "").strip(),
            "state_hash": state_digest,
        },
        timestamp=now,
    )
    payload = status(repo_root)
    payload["ok"] = True
    payload["state_hash"] = state_digest
    return payload


def validate_scope(
    repo_root: Path,
    *,
    scope: str,
    actor: str = "",
    token: str = "",
    context: str = "",
    record_usage: bool = True,
) -> tuple[bool, str]:
    scope_name = str(scope or "").strip().lower()
    if not scope_name:
        return False, "breakglass scope is required"
    snapshot = status(repo_root)
    if not snapshot["active"]:
        return False, "no active breakglass session"
    if snapshot["expired"]:
        return False, "breakglass session has expired"
    scopes = snapshot.get("scopes", [])
    if not isinstance(scopes, list) or scope_name not in [str(item).strip().lower() for item in scopes]:
        return False, f"breakglass scope {scope_name!r} is not enabled"

    paths = resolve_paths(repo_root)
    state = _read_state(paths)
    state_token_hash = str(state.get("token_hash", "")).strip()
    token_value = str(token or "").strip() or str(os.environ.get(TOKEN_ENV, "")).strip()
    if not token_value:
        return False, f"missing {TOKEN_ENV} for breakglass authorization"
    if not state_token_hash:
        return False, "breakglass state missing token hash"
    if _sha256_text(token_value) != state_token_hash:
        return False, "breakglass token mismatch"

    if record_usage:
        actor_value = actor.strip() or _actor(default_actor="operator")
        _append_ledger(
            paths,
            event="authorize",
            session_id=str(state.get("session_id", "")).strip(),
            actor=actor_value,
            details={
                "scope": scope_name,
                "context": str(context or "").strip(),
                "state_hash": _state_hash(state),
            },
            timestamp=_utc_now(),
        )
    return True, "breakglass authorization granted"
