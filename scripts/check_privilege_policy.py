#!/usr/bin/env python3
"""Validate least-privilege defaults and breakglass controls for swarm execution."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_POLICY = Path("config/privilege_policy.json")
DEFAULT_OUTPUT = Path("artifacts/autonomy/privilege_policy_health.json")
DEFAULT_ELEVATED_FLAG_TOKENS = {
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "bypassPermissions",
    "yolo",
}


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    return str(value).strip()


def _parse_iso(value: Any) -> datetime | None:
    text = _as_text(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _resolve_path(root: Path, raw: Any, default: Path) -> Path:
    text = _as_text(raw)
    path = Path(text) if text else default
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _normalize_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for item in values:
        text = _as_text(item)
        if text:
            out.append(text)
    return out


def evaluate_policy(
    *,
    policy: dict[str, Any],
    events: list[dict[str, Any]],
    active_grant: dict[str, Any],
    lookback_hours_override: int | None = None,
) -> dict[str, Any]:
    monitoring = policy.get("monitoring", {}) if isinstance(policy.get("monitoring"), dict) else {}
    breakglass = policy.get("breakglass", {}) if isinstance(policy.get("breakglass"), dict) else {}
    providers = policy.get("providers", {}) if isinstance(policy.get("providers"), dict) else {}

    lookback_hours = max(1, lookback_hours_override if lookback_hours_override is not None else _as_int(monitoring.get("lookback_hours", 168), 168))
    require_recent_events = _as_bool(monitoring.get("require_recent_events", True), True)
    max_event_age_minutes = max(1, _as_int(monitoring.get("max_event_age_minutes", 240), 240))
    min_scanned_events = max(0, _as_int(monitoring.get("min_scanned_events", 1), 1))
    breakglass_enabled = _as_bool(breakglass.get("enabled", True), True)
    required_fields = _normalize_list(
        breakglass.get(
            "required_fields",
            ["grant_id", "reason", "scope", "requested_by", "approved_by", "issued_at", "expires_at", "rollback_proof", "providers"],
        )
    )

    elevated_tokens: set[str] = set(DEFAULT_ELEVATED_FLAG_TOKENS)
    for provider_cfg in providers.values():
        if not isinstance(provider_cfg, dict):
            continue
        for token in _normalize_list(provider_cfg.get("elevated_args", [])):
            elevated_tokens.add(token)

    cutoff = _now_utc() - timedelta(hours=lookback_hours)
    scanned = 0
    parse_skips = 0
    latest_ts: datetime | None = None
    least_privilege_count = 0
    breakglass_count = 0
    decision_events = 0
    control_events = 0

    violations: list[dict[str, Any]] = []
    for row in events:
        ts = _parse_iso(row.get("timestamp"))
        if ts is None:
            parse_skips += 1
            continue
        if ts < cutoff:
            continue
        scanned += 1
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts

        event_type = _as_text(row.get("event_type", "privilege_decision")).lower()
        if event_type != "privilege_decision":
            control_events += 1
            continue
        decision_events += 1

        mode = _as_text(row.get("mode", "")).lower()
        task_id = _as_text(row.get("task_id", ""))
        provider = _as_text(row.get("provider", ""))
        command_args = _normalize_list(row.get("command_args", []))
        has_elevated_flag = any(token in elevated_tokens for token in command_args)

        if mode == "least_privilege":
            least_privilege_count += 1
            if has_elevated_flag:
                violations.append(
                    {
                        "type": "least_privilege_contains_elevated_flags",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "task_id": task_id,
                        "provider": provider,
                    }
                )
        elif mode == "breakglass_elevated":
            breakglass_count += 1
            if not breakglass_enabled:
                violations.append(
                    {
                        "type": "breakglass_used_when_disabled",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "task_id": task_id,
                        "provider": provider,
                    }
                )
            grant_id = _as_text(row.get("grant_id", ""))
            grant = row.get("grant", {}) if isinstance(row.get("grant"), dict) else {}
            missing = [field for field in required_fields if not _as_text(grant.get(field, "")).strip() and field != "providers"]
            if not grant_id:
                missing.append("grant_id")
            if missing:
                violations.append(
                    {
                        "type": "breakglass_missing_evidence",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "task_id": task_id,
                        "provider": provider,
                        "missing_fields": sorted(set(missing)),
                    }
                )
            expires_at = _parse_iso(grant.get("expires_at"))
            if expires_at is not None and _now_utc() > expires_at:
                violations.append(
                    {
                        "type": "breakglass_event_after_expiry",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "task_id": task_id,
                        "provider": provider,
                        "grant_id": grant_id,
                    }
                )
        else:
            violations.append(
                {
                    "type": "unknown_privilege_mode",
                    "timestamp": ts.isoformat().replace("+00:00", "Z"),
                    "task_id": task_id,
                    "provider": provider,
                    "mode": mode,
                }
            )

    latest_event_age_minutes = -1
    if latest_ts is not None:
        latest_event_age_minutes = max(0, int((_now_utc() - latest_ts).total_seconds() // 60))

    if breakglass_enabled and active_grant:
        active_expires = _parse_iso(active_grant.get("expires_at"))
        if active_expires is None:
            violations.append({"type": "active_grant_invalid_timestamp"})
        elif _now_utc() > active_expires:
            violations.append({"type": "active_grant_expired"})

    freshness_ok = (not require_recent_events) or (
        (latest_event_age_minutes >= 0 and latest_event_age_minutes <= max_event_age_minutes)
        or scanned == 0
    )
    volume_ok = scanned >= min_scanned_events
    ok = freshness_ok and volume_ok and len(violations) == 0

    return {
        "schema_version": "privilege-policy-health.v1",
        "generated_at_utc": _utc_now_iso(),
        "ok": ok,
        "lookback_hours": lookback_hours,
        "summary": {
            "scanned_events": scanned,
            "parse_skips": parse_skips,
            "least_privilege_events": least_privilege_count,
            "breakglass_events": breakglass_count,
            "decision_events": decision_events,
            "control_events": control_events,
            "violation_count": len(violations),
            "latest_event_age_minutes": latest_event_age_minutes,
            "freshness_ok": freshness_ok,
            "volume_ok": volume_ok,
        },
        "monitoring": {
            "require_recent_events": require_recent_events,
            "max_event_age_minutes": max_event_age_minutes,
            "min_scanned_events": min_scanned_events,
        },
        "breakglass": {
            "enabled": breakglass_enabled,
            "required_fields": required_fields,
            "active_grant_present": bool(active_grant),
        },
        "violations": violations,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check least-privilege execution policy and breakglass controls.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY), help="Privilege policy JSON file.")
    parser.add_argument("--audit-log-file", default="", help="Optional privilege audit NDJSON override.")
    parser.add_argument("--active-grant-file", default="", help="Optional active breakglass grant JSON override.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output report JSON path.")
    parser.add_argument("--lookback-hours", type=int, default=0, help="Override lookback window in hours.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when policy health is not ok.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()

    policy_file = _resolve_path(root, args.policy_file, DEFAULT_POLICY)
    policy = _load_json(policy_file)
    if not policy:
        policy = {}

    breakglass = policy.get("breakglass", {}) if isinstance(policy.get("breakglass"), dict) else {}
    default_audit = _resolve_path(root, breakglass.get("audit_log_file"), root / "artifacts" / "autonomy" / "privilege_escalations.ndjson")
    default_grant = _resolve_path(root, breakglass.get("active_grant_file"), root / "artifacts" / "autonomy" / "breakglass" / "active_grant.json")

    audit_log_file = _resolve_path(root, args.audit_log_file, default_audit)
    active_grant_file = _resolve_path(root, args.active_grant_file, default_grant)
    output_file = _resolve_path(root, args.output, DEFAULT_OUTPUT)

    events = _load_ndjson(audit_log_file)
    active_grant = _load_json(active_grant_file)
    report = evaluate_policy(
        policy=policy,
        events=events,
        active_grant=active_grant,
        lookback_hours_override=(max(1, int(args.lookback_hours)) if int(args.lookback_hours or 0) > 0 else None),
    )
    report["artifacts"] = {
        "policy_file": str(policy_file),
        "audit_log_file": str(audit_log_file),
        "active_grant_file": str(active_grant_file),
        "output_file": str(output_file),
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    payload = {
        "output": str(output_file),
        "ok": bool(report.get("ok", False)),
        "scanned_events": int(summary.get("scanned_events", 0) or 0),
        "violation_count": int(summary.get("violation_count", 0) or 0),
        "breakglass_events": int(summary.get("breakglass_events", 0) or 0),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "privilege-policy-health "
            f"ok={payload['ok']} scanned={payload['scanned_events']} "
            f"violations={payload['violation_count']} breakglass_events={payload['breakglass_events']}"
        )

    if args.strict and not bool(report.get("ok", False)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
