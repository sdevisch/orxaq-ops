"""Leader lease backends with monotonic epoch fencing."""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _parse_iso(value: Any) -> dt.datetime | None:
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


def _normalize_node_id(value: str) -> str:
    raw = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip().lower())
    normalized = "-".join(part for part in raw.replace("_", "-").split("-") if part)
    return normalized or "node-unknown"


def _default_node_id() -> str:
    host = socket.gethostname().strip() or "host"
    return _normalize_node_id(host)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except Exception:
        return default


@dataclass(frozen=True)
class LeaderLeaseConfig:
    root_dir: Path
    node_id: str
    lease_file: Path
    ttl_sec: int
    backend: str

    @classmethod
    def from_root(cls, root: Path) -> "LeaderLeaseConfig":
        root = root.resolve()
        node_raw = str(os.environ.get("ORXAQ_AUTONOMY_NODE_ID", "")).strip()
        node_id = _normalize_node_id(node_raw) if node_raw else _default_node_id()
        lease_file = Path(
            os.environ.get(
                "ORXAQ_AUTONOMY_LEADER_LEASE_FILE",
                str(root / "state" / "event_mesh" / "leader_lease.json"),
            )
        ).resolve()
        ttl_sec = max(5, _int_value(os.environ.get("ORXAQ_AUTONOMY_LEADER_LEASE_TTL_SEC", "45"), 45))
        backend = str(os.environ.get("ORXAQ_AUTONOMY_LEADER_LEASE_BACKEND", "file")).strip().lower() or "file"
        return cls(
            root_dir=root,
            node_id=node_id,
            lease_file=lease_file,
            ttl_sec=ttl_sec,
            backend=backend,
        )


def _file_read_lease_snapshot(config: LeaderLeaseConfig) -> dict[str, Any]:
    payload = _read_json(config.lease_file)
    now_utc = _now_utc()
    leader_id = str(payload.get("leader_id", "")).strip()
    epoch = max(0, _int_value(payload.get("epoch", 0), 0))
    expires = _parse_iso(payload.get("lease_expires_at"))
    expired = expires is None or expires <= now_utc
    return {
        "ok": True,
        "node_id": config.node_id,
        "leader_id": leader_id,
        "epoch": epoch,
        "lease_expires_at": expires.isoformat() if expires else "",
        "ttl_sec": config.ttl_sec,
        "expired": bool(expired),
        "is_leader": bool(leader_id and leader_id == config.node_id and not expired),
        "source_file": str(config.lease_file),
        "backend": "file",
    }


def _file_acquire_or_renew_lease(config: LeaderLeaseConfig) -> dict[str, Any]:
    snapshot = _file_read_lease_snapshot(config)
    now_utc = _now_utc()
    leader_id = str(snapshot.get("leader_id", "")).strip()
    epoch = max(0, _int_value(snapshot.get("epoch", 0), 0))
    expired = bool(snapshot.get("expired", True))
    is_local_leader = bool(snapshot.get("is_leader", False))

    outcome = "follower"
    if is_local_leader:
        outcome = "renewed"
    elif expired or not leader_id:
        leader_id = config.node_id
        epoch = max(1, epoch + 1)
        outcome = "acquired"
    else:
        return {
            **snapshot,
            "updated_at": _now_iso(),
            "outcome": outcome,
        }

    expires = now_utc + dt.timedelta(seconds=max(5, int(config.ttl_sec)))
    payload = {
        "leader_id": leader_id,
        "epoch": int(epoch),
        "lease_expires_at": expires.isoformat(),
        "ttl_sec": int(config.ttl_sec),
        "updated_at": _now_iso(),
    }
    _write_json_atomic(config.lease_file, payload)
    return {
        "ok": True,
        "node_id": config.node_id,
        "leader_id": leader_id,
        "epoch": int(epoch),
        "lease_expires_at": payload["lease_expires_at"],
        "ttl_sec": int(config.ttl_sec),
        "expired": False,
        "is_leader": leader_id == config.node_id,
        "source_file": str(config.lease_file),
        "backend": "file",
        "updated_at": payload["updated_at"],
        "outcome": outcome,
    }


def _backend_unavailable_snapshot(config: LeaderLeaseConfig, backend: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "node_id": config.node_id,
        "leader_id": "",
        "epoch": 0,
        "lease_expires_at": "",
        "ttl_sec": config.ttl_sec,
        "expired": True,
        "is_leader": False,
        "source_file": str(config.lease_file),
        "backend": backend,
        "observer_mode": True,
        "retryable": True,
        "next_retry_sec": 5,
        "lease_key": "orxaq/control/leader",
        "outcome": "backend_unavailable",
        "detail": detail,
        "updated_at": _now_iso(),
    }


def _allow_file_fallback() -> bool:
    raw = str(os.environ.get("ORXAQ_AUTONOMY_LEADER_LEASE_FILE_FALLBACK", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return False


def _backend_snapshot_or_fallback(config: LeaderLeaseConfig, *, backend: str, detail: str) -> dict[str, Any]:
    if _allow_file_fallback():
        snapshot = _file_acquire_or_renew_lease(config)
        return {
            **snapshot,
            "requested_backend": backend,
            "fallback_backend": "file",
            "observer_mode": False,
            "detail": detail,
            "outcome": "fallback_file",
        }
    return _backend_unavailable_snapshot(config, backend, detail)


def read_lease_snapshot(config: LeaderLeaseConfig) -> dict[str, Any]:
    backend = str(config.backend or "file").strip().lower() or "file"
    if backend == "file":
        return _file_read_lease_snapshot(config)
    if backend == "etcd":
        endpoints = str(os.environ.get("ORXAQ_AUTONOMY_LEADER_ETCD_ENDPOINTS", "")).strip()
        if not endpoints:
            return _backend_snapshot_or_fallback(config, backend="etcd", detail="missing_ORXAQ_AUTONOMY_LEADER_ETCD_ENDPOINTS")
        return _backend_snapshot_or_fallback(config, backend="etcd", detail="etcd_backend_not_configured_in_runtime")
    if backend == "postgres":
        dsn = str(os.environ.get("ORXAQ_AUTONOMY_LEADER_POSTGRES_DSN", "")).strip()
        if not dsn:
            return _backend_snapshot_or_fallback(config, backend="postgres", detail="missing_ORXAQ_AUTONOMY_LEADER_POSTGRES_DSN")
        return _backend_snapshot_or_fallback(config, backend="postgres", detail="postgres_backend_not_configured_in_runtime")
    return _backend_snapshot_or_fallback(config, backend=backend, detail="unknown_backend")


def acquire_or_renew_lease(config: LeaderLeaseConfig) -> dict[str, Any]:
    backend = str(config.backend or "file").strip().lower() or "file"
    if backend == "file":
        return _file_acquire_or_renew_lease(config)
    return read_lease_snapshot(config)
