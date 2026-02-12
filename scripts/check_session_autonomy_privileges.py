#!/usr/bin/env python3
"""Verify session/model autonomy authorization and extra_high privilege behavior."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_AGENTS_FILE = Path("AGENTS.md")
DEFAULT_OUTPUT = Path("artifacts/autonomy/session_autonomy_privileges.json")
DEFAULT_RUNTIME_MARKERS = {
    "approval_policy": "never",
    "sandbox_mode": "danger-full-access",
    "execution_context": "codex_desktop_or_cli",
}
DEFAULT_IDENTITY = {
    "identity_id": "codex.gpt5.high.v1",
    "agent_name": "Codex",
    "model_family": "GPT-5",
    "capability_tier": "high",
}
EXECUTION_PROFILE_ALIASES = {
    "standard": "standard",
    "default": "standard",
    "normal": "standard",
    "high": "high",
    "extra-high": "extra_high",
    "extrahigh": "extra_high",
    "extra_high": "extra_high",
    "xhigh": "extra_high",
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


def _as_text(value: Any) -> str:
    return str(value).strip()


def _resolve_path(root: Path, raw: Any, default: Path) -> Path:
    text = _as_text(raw)
    path = Path(text) if text else default
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


def _normalize_execution_profile(raw: Any) -> str:
    text = _as_text(raw).lower()
    if not text:
        return "high"
    return EXECUTION_PROFILE_ALIASES.get(text, "high")


def _extract_backtick_field(text: str, field_name: str) -> str:
    pattern = re.compile(rf"`{re.escape(field_name)}`\s*:\s*`([^`]+)`")
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _extract_runtime_markers(text: str) -> dict[str, str]:
    lines = text.splitlines()
    markers: dict[str, str] = {}
    in_markers_block = False
    for raw in lines:
        stripped = raw.strip()
        if not in_markers_block and "runtime_markers_required" in stripped:
            in_markers_block = True
            continue
        if not in_markers_block:
            continue
        marker_match = re.match(r"[-*]\s*`([^`=]+)=([^`]+)`\s*$", stripped)
        if marker_match:
            markers[marker_match.group(1).strip()] = marker_match.group(2).strip()
            continue
        if stripped.startswith("##") or stripped.startswith("###"):
            break
        if stripped.startswith("- `") and "=" not in stripped:
            break
    return markers


def load_agents_policy(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    runtime_markers = _extract_runtime_markers(text)
    if not runtime_markers:
        runtime_markers = dict(DEFAULT_RUNTIME_MARKERS)

    policy = {
        "policy_version": _extract_backtick_field(text, "policy_version") or "unknown",
        "identity_id": _extract_backtick_field(text, "identity_id") or DEFAULT_IDENTITY["identity_id"],
        "agent_name": _extract_backtick_field(text, "agent_name") or DEFAULT_IDENTITY["agent_name"],
        "model_family": _extract_backtick_field(text, "model_family") or DEFAULT_IDENTITY["model_family"],
        "capability_tier": _extract_backtick_field(text, "capability_tier") or DEFAULT_IDENTITY["capability_tier"],
        "identity_fingerprint": _extract_backtick_field(text, "identity_fingerprint"),
        "runtime_markers_required": runtime_markers,
        "source_file": str(path),
        "source_exists": path.exists(),
    }
    return policy


def evaluate_session_privileges(*, policy: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    runtime_identity_id = _as_text(runtime.get("identity_id", ""))
    runtime_agent_name = _as_text(runtime.get("agent_name", ""))
    runtime_model_family = _as_text(runtime.get("model_family", ""))
    runtime_capability_tier = _as_text(runtime.get("capability_tier", ""))
    runtime_fingerprint = _as_text(runtime.get("identity_fingerprint", ""))
    runtime_markers = runtime.get("runtime_markers", {})
    if not isinstance(runtime_markers, dict):
        runtime_markers = {}
    normalized_runtime_markers = {str(key): _as_text(value) for key, value in runtime_markers.items()}

    expected_id = _as_text(policy.get("identity_id", ""))
    expected_agent_name = _as_text(policy.get("agent_name", ""))
    expected_model_family = _as_text(policy.get("model_family", ""))
    expected_capability_tier = _as_text(policy.get("capability_tier", ""))
    expected_fingerprint = _as_text(policy.get("identity_fingerprint", ""))
    expected_markers = policy.get("runtime_markers_required", {})
    if not isinstance(expected_markers, dict):
        expected_markers = {}
    normalized_expected_markers = {str(key): _as_text(value) for key, value in expected_markers.items()}

    identity_match = (
        runtime_identity_id == expected_id
        and runtime_agent_name == expected_agent_name
        and runtime_model_family == expected_model_family
        and runtime_capability_tier == expected_capability_tier
    )
    fingerprint_match = True
    if expected_fingerprint:
        fingerprint_match = runtime_fingerprint == expected_fingerprint
    runtime_marker_match = all(
        normalized_runtime_markers.get(key, "") == value for key, value in normalized_expected_markers.items()
    )

    user_grant_detected = _as_bool(runtime.get("user_grant_detected"), False)
    critical_security_decision = _as_bool(runtime.get("critical_security_decision"), False)
    critical_security_gate = "hold" if critical_security_decision else "pass"
    autonomy_authorized = (
        identity_match
        and fingerprint_match
        and runtime_marker_match
        and user_grant_detected
        and critical_security_gate == "pass"
    )

    execution_profile = _normalize_execution_profile(runtime.get("execution_profile", "high"))
    requested_privileges = {
        "force_continuation": execution_profile == "extra_high",
        "assume_true_full_autonomy": execution_profile == "extra_high",
        "queue_persistent_mode": execution_profile == "extra_high",
    }
    effective_privileges = {
        key: bool(value and autonomy_authorized) for key, value in requested_privileges.items()
    }
    ok = autonomy_authorized and (
        execution_profile != "extra_high" or all(bool(value) for value in effective_privileges.values())
    )

    return {
        "schema_version": "session-autonomy-privileges.v1",
        "generated_at_utc": _utc_now_iso(),
        "policy_version": _as_text(policy.get("policy_version", "unknown")),
        "identity_id": runtime_identity_id,
        "identity_match": identity_match,
        "fingerprint_match": fingerprint_match,
        "runtime_marker_match": runtime_marker_match,
        "user_grant_detected": user_grant_detected,
        "critical_security_gate": critical_security_gate,
        "autonomy_authorized": autonomy_authorized,
        "execution_profile": execution_profile,
        "requested_privileges": requested_privileges,
        "effective_privileges": effective_privileges,
        "runtime_markers": normalized_runtime_markers,
        "expected_runtime_markers": normalized_expected_markers,
        "expected_identity": {
            "identity_id": expected_id,
            "agent_name": expected_agent_name,
            "model_family": expected_model_family,
            "capability_tier": expected_capability_tier,
            "identity_fingerprint": expected_fingerprint,
        },
        "ok": ok,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check session autonomy authorization and extra_high privileges.")
    parser.add_argument("--root", default=".", help="Workspace root used to resolve relative paths.")
    parser.add_argument("--agents-file", default=str(DEFAULT_AGENTS_FILE), help="AGENTS policy file path.")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Output JSON report path.",
    )
    parser.add_argument(
        "--execution-profile",
        default=os.getenv("ORXAQ_AUTONOMY_EXECUTION_PROFILE", "high"),
        help="Execution profile under test (standard|high|extra_high).",
    )
    parser.add_argument("--identity-id", default=os.getenv("ORXAQ_AUTONOMY_IDENTITY_ID", DEFAULT_IDENTITY["identity_id"]))
    parser.add_argument("--agent-name", default=os.getenv("ORXAQ_AUTONOMY_AGENT_NAME", DEFAULT_IDENTITY["agent_name"]))
    parser.add_argument(
        "--model-family",
        default=os.getenv("ORXAQ_AUTONOMY_MODEL_FAMILY", DEFAULT_IDENTITY["model_family"]),
    )
    parser.add_argument(
        "--capability-tier",
        default=os.getenv("ORXAQ_AUTONOMY_CAPABILITY_TIER", DEFAULT_IDENTITY["capability_tier"]),
    )
    parser.add_argument(
        "--identity-fingerprint",
        default=os.getenv("ORXAQ_AUTONOMY_IDENTITY_FINGERPRINT", ""),
    )
    parser.add_argument(
        "--approval-policy",
        default=os.getenv("ORXAQ_RUNTIME_APPROVAL_POLICY", os.getenv("APPROVAL_POLICY", "")),
    )
    parser.add_argument(
        "--sandbox-mode",
        default=os.getenv("ORXAQ_RUNTIME_SANDBOX_MODE", os.getenv("SANDBOX_MODE", "")),
    )
    parser.add_argument(
        "--execution-context",
        default=os.getenv("ORXAQ_RUNTIME_EXECUTION_CONTEXT", DEFAULT_RUNTIME_MARKERS["execution_context"]),
    )
    parser.add_argument(
        "--user-grant-detected",
        action=argparse.BooleanOptionalAction,
        default=_as_bool(os.getenv("ORXAQ_AUTONOMY_USER_GRANT", "1"), True),
    )
    parser.add_argument(
        "--critical-security-decision",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--strict", action="store_true", help="Exit with 1 when check fails.")
    parser.add_argument("--json", action="store_true", help="Emit full report JSON to stdout.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    policy_file = _resolve_path(root, args.agents_file, DEFAULT_AGENTS_FILE)
    output = _resolve_path(root, args.output, DEFAULT_OUTPUT)
    output.parent.mkdir(parents=True, exist_ok=True)

    policy = load_agents_policy(policy_file)
    runtime = {
        "identity_id": args.identity_id,
        "agent_name": args.agent_name,
        "model_family": args.model_family,
        "capability_tier": args.capability_tier,
        "identity_fingerprint": args.identity_fingerprint,
        "execution_profile": args.execution_profile,
        "user_grant_detected": bool(args.user_grant_detected),
        "critical_security_decision": bool(args.critical_security_decision),
        "runtime_markers": {
            "approval_policy": _as_text(args.approval_policy),
            "sandbox_mode": _as_text(args.sandbox_mode),
            "execution_context": _as_text(args.execution_context),
        },
    }
    report = evaluate_session_privileges(policy=policy, runtime=runtime)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        status = "PASS" if report["ok"] else "FAIL"
        print(
            f"{status} autonomy_authorized={report['autonomy_authorized']} "
            f"profile={report['execution_profile']} output={output}"
        )
    if args.strict and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
