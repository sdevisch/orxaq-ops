#!/usr/bin/env python3
"""Revoke active breakglass grant and append audit evidence."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    parser = argparse.ArgumentParser(description="Revoke active breakglass grant.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--policy-file", default="config/privilege_policy.json", help="Privilege policy JSON.")
    parser.add_argument("--reason", required=True, help="Reason for revocation.")
    parser.add_argument("--requested-by", default="codex", help="Actor requesting revocation.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary line.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    policy_file = _resolve(root, args.policy_file, root / "config" / "privilege_policy.json")
    policy = _load_json(policy_file)
    breakglass = policy.get("breakglass", {}) if isinstance(policy.get("breakglass"), dict) else {}
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

    active = _load_json(active_grant_file)
    had_active = bool(active)
    grant_id = _as_text(active.get("grant_id", ""))
    if active_grant_file.exists():
        active_grant_file.unlink(missing_ok=True)

    _append_ndjson(
        audit_log_file,
        {
            "timestamp": _now_iso(),
            "event_type": "breakglass_grant_revoked",
            "grant_id": grant_id,
            "had_active_grant": had_active,
            "reason": _as_text(args.reason),
            "requested_by": _as_text(args.requested_by),
            "active_grant_file": str(active_grant_file),
        },
    )

    payload = {
        "ok": True,
        "had_active_grant": had_active,
        "grant_id": grant_id,
        "active_grant_file": str(active_grant_file),
        "audit_log_file": str(audit_log_file),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            f"breakglass revoked had_active_grant={had_active} grant_id={grant_id or 'none'} "
            f"active_grant_file={active_grant_file}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
