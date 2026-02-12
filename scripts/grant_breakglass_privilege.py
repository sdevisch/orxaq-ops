#!/usr/bin/env python3
"""Create and optionally activate a temporary breakglass privilege grant."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _utc_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    return str(value).strip()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _append_ndjson(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _resolve(root: Path, raw: Any, default: Path) -> Path:
    text = _as_text(raw)
    path = Path(text) if text else default
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a temporary breakglass grant with audit evidence.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--policy-file", default="config/privilege_policy.json", help="Privilege policy JSON.")
    parser.add_argument("--grant-id", default="", help="Optional explicit grant id.")
    parser.add_argument("--provider", action="append", required=True, help="Allowed provider (repeatable).")
    parser.add_argument("--task-id", action="append", default=[], help="Optional allowed task id (repeatable).")
    parser.add_argument("--reason", required=True, help="Reason for temporary elevation.")
    parser.add_argument("--scope", required=True, help="Scope boundary for elevated execution.")
    parser.add_argument("--rollback-proof", required=True, help="Rollback plan/proof reference.")
    parser.add_argument("--requested-by", default="codex", help="Requesting actor.")
    parser.add_argument("--approved-by", default="operator", help="Approving actor.")
    parser.add_argument("--ttl-minutes", type=int, default=30, help="Grant TTL in minutes.")
    parser.add_argument("--activate", action=argparse.BooleanOptionalAction, default=True, help="Activate immediately.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary line.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    policy_file = _resolve(root, args.policy_file, root / "config" / "privilege_policy.json")
    policy = _load_json(policy_file)

    breakglass = policy.get("breakglass", {}) if isinstance(policy.get("breakglass"), dict) else {}
    max_ttl = max(1, _as_int(breakglass.get("max_ttl_minutes", 120), 120))
    ttl_minutes = max(1, int(args.ttl_minutes))
    if ttl_minutes > max_ttl:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"ttl_exceeds_policy:{ttl_minutes}>{max_ttl}",
                },
                sort_keys=True,
            )
        )
        return 1

    active_grant_file = _resolve(
        root,
        breakglass.get("active_grant_file"),
        root / "artifacts" / "autonomy" / "breakglass" / "active_grant.json",
    )
    audit_log_file = _resolve(
        root,
        breakglass.get("audit_log_file"),
        root / "artifacts" / "autonomy" / "privilege_escalations.ndjson",
    )
    grants_dir = active_grant_file.parent / "grants"

    now = _now_utc()
    expires_at = now + timedelta(minutes=ttl_minutes)
    grant_id = _as_text(args.grant_id) or f"bg-{now.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    providers = sorted({_as_text(item).lower() for item in args.provider if _as_text(item)})
    task_allowlist = sorted({_as_text(item) for item in args.task_id if _as_text(item)})

    grant = {
        "grant_id": grant_id,
        "reason": _as_text(args.reason),
        "scope": _as_text(args.scope),
        "requested_by": _as_text(args.requested_by),
        "approved_by": _as_text(args.approved_by),
        "issued_at": _utc_iso(now),
        "expires_at": _utc_iso(expires_at),
        "rollback_proof": _as_text(args.rollback_proof),
        "providers": providers,
        "task_allowlist": task_allowlist,
    }

    grant_file = (grants_dir / f"{grant_id}.json").resolve()
    _write_json(grant_file, grant)
    _append_ndjson(
        audit_log_file,
        {
            "timestamp": _utc_iso(now),
            "event_type": "breakglass_grant_created",
            "grant_id": grant_id,
            "providers": providers,
            "task_allowlist": task_allowlist,
            "ttl_minutes": ttl_minutes,
            "reason": grant["reason"],
            "scope": grant["scope"],
            "requested_by": grant["requested_by"],
            "approved_by": grant["approved_by"],
            "rollback_proof": grant["rollback_proof"],
        },
    )

    activated = False
    if bool(args.activate):
        _write_json(active_grant_file, grant)
        activated = True
        _append_ndjson(
            audit_log_file,
            {
                "timestamp": _utc_iso(_now_utc()),
                "event_type": "breakglass_grant_activated",
                "grant_id": grant_id,
                "active_grant_file": str(active_grant_file),
            },
        )

    payload = {
        "ok": True,
        "grant_id": grant_id,
        "grant_file": str(grant_file),
        "active_grant_file": str(active_grant_file),
        "audit_log_file": str(audit_log_file),
        "activated": activated,
        "expires_at": grant["expires_at"],
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            f"breakglass grant_id={grant_id} activated={activated} "
            f"expires_at={grant['expires_at']} grant_file={grant_file}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
