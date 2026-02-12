#!/usr/bin/env python3
"""Validate external API interoperability policy readiness."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None


DEFAULT_POLICY = Path("config/api_interop_policy.json")
DEFAULT_BACKLOG = Path("../orxaq/ops/backlog/distributed_todo.yaml")
DEFAULT_OUTPUT = Path("artifacts/autonomy/api_interop_policy_health.json")

REQUIRED_PROTOCOLS = {"rest", "mcp", "webhook", "sse"}
REQUIRED_STANDARDS = {"openapi", "json_schema", "asyncapi", "cloudevents"}
REQUIRED_ASYNC_GUARANTEES = {"at_least_once", "dead_letter", "replay_safe_ids"}
REQUIRED_ADAPTERS = {"grpc", "jsonrpc"}
REQUIRED_PARITY_PROTOCOLS = {"rest", "mcp"}
REQUIRED_SECURITY_CONTROLS = {"oauth2", "oidc", "token_scopes"}
REQUIRED_BREAKGLASS_FIELDS = {"reason", "scope", "ttl", "rollback_proof", "audit_trail"}
REQUIRED_TELEMETRY_DIMS = {
    "protocol",
    "route",
    "auth_context",
    "latency_ms",
    "error_class",
    "downstream_dependency",
    "contract_version",
}
REQUIRED_ACTIVATION_BACKLOG_TASKS = {"B11-T6", "B11-T7", "B9-T4"}
REQUIRED_ACTIVATION_API_TASKS = {"B12-T1", "B12-T2", "B12-T3", "B12-T6", "B12-T8"}
REQUIRED_BACKLOG_TASKS = {
    "B12-EPIC",
    "B12-T1",
    "B12-T2",
    "B12-T3",
    "B12-T4",
    "B12-T5",
    "B12-T6",
    "B12-T7",
    "B12-T8",
    "B12-T9",
}
EXPECTED_BACKLOG_DEPENDENCIES = {
    "B12-EPIC": {"B11-T6", "B11-T7", "B9-T4"},
    "B12-T1": {"B12-EPIC"},
    "B12-T3": {"B12-T1", "B12-T2"},
    "B12-T6": {"B12-T2", "B12-T3", "B12-T4"},
    "B12-T8": {"B12-T5", "B12-T6", "B12-T7"},
    "B12-T9": {"B10-T3", "B12-T2", "B12-T3"},
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_text_list(value: Any) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        text = _as_text(item)
        if text:
            out.append(text)
    return out


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_backlog_fallback(text: str) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    reading_deps = False

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        id_match = re.match(r"^\s*-\s+id:\s*(.+?)\s*$", line)
        if id_match:
            if current is not None:
                tasks.append(current)
            current = {"id": id_match.group(1).strip(), "dependencies": []}
            reading_deps = False
            continue

        if current is None:
            continue

        if re.match(r"^\s*dependencies:\s*", line):
            reading_deps = True
            continue

        if reading_deps:
            dep_match = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if dep_match:
                dep = dep_match.group(1).strip()
                if dep:
                    current["dependencies"].append(dep)
                continue
            if line and not line.startswith(" "):
                reading_deps = False

    if current is not None:
        tasks.append(current)

    return {"tasks": tasks}


def _load_backlog(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists() or not path.is_file():
        return ({}, "missing")
    text = path.read_text(encoding="utf-8", errors="replace")
    if yaml is not None:
        try:
            payload = yaml.safe_load(text)
        except Exception:  # noqa: BLE001
            payload = {}
        if isinstance(payload, dict):
            return (payload, "yaml")
    return (_parse_backlog_fallback(text), "fallback")


def evaluate(*, policy: dict[str, Any], backlog: dict[str, Any], backlog_parse_mode: str) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []

    schema = _as_text(policy.get("schema_version"))
    if not schema.startswith("api-interop-policy.v"):
        violations.append({"type": "api_interop_schema_invalid", "value": schema})

    release_phase = _as_text(policy.get("release_phase", "foundation")).lower() or "foundation"
    if release_phase not in {"foundation", "late_stage"}:
        violations.append({"type": "release_phase_invalid", "value": release_phase})

    activation = policy.get("activation", {}) if isinstance(policy.get("activation"), dict) else {}
    activation_checks_passed = 0
    if _as_bool(activation.get("late_stage_only", False), False):
        activation_checks_passed += 1
    else:
        violations.append({"type": "activation_not_late_stage_only"})

    if _as_bool(activation.get("requires_upgrade_controls", False), False):
        activation_checks_passed += 1
    else:
        violations.append({"type": "activation_missing_upgrade_controls_prereq"})

    activation_backlog_tasks = {
        item for item in _normalize_text_list(activation.get("requires_backlog_tasks", []))
    }
    missing_activation_backlog = sorted(REQUIRED_ACTIVATION_BACKLOG_TASKS - activation_backlog_tasks)
    if missing_activation_backlog:
        violations.append({"type": "activation_backlog_tasks_missing", "missing": missing_activation_backlog})
    else:
        activation_checks_passed += 1

    activation_api_tasks = {
        item for item in _normalize_text_list(activation.get("requires_api_backlog_tasks", []))
    }
    missing_activation_api = sorted(REQUIRED_ACTIVATION_API_TASKS - activation_api_tasks)
    if missing_activation_api:
        violations.append({"type": "activation_api_tasks_missing", "missing": missing_activation_api})
    else:
        activation_checks_passed += 1

    protocols = policy.get("protocols", {}) if isinstance(policy.get("protocols"), dict) else {}
    required_protocol_rows = _as_list(protocols.get("required"))
    protocol_ids: set[str] = set()
    for row in required_protocol_rows:
        if not isinstance(row, dict):
            continue
        protocol_id = _as_text(row.get("id", "")).lower()
        if not protocol_id:
            continue
        protocol_ids.add(protocol_id)
        if not _as_bool(row.get("enabled", False), False):
            violations.append({"type": "required_protocol_disabled", "protocol": protocol_id})

    missing_protocols = sorted(REQUIRED_PROTOCOLS - protocol_ids)
    if missing_protocols:
        violations.append({"type": "missing_required_protocols", "missing": missing_protocols})

    standard_ids = {item.lower() for item in _normalize_text_list(protocols.get("standards", []))}
    missing_standards = sorted(REQUIRED_STANDARDS - standard_ids)
    if missing_standards:
        violations.append({"type": "missing_required_standards", "missing": missing_standards})

    async_cfg = protocols.get("async", {}) if isinstance(protocols.get("async"), dict) else {}
    if not _as_bool(async_cfg.get("requires_asyncapi", False), False):
        violations.append({"type": "asyncapi_requirement_missing"})
    if not _as_bool(async_cfg.get("requires_cloudevents_envelope", False), False):
        violations.append({"type": "cloudevents_requirement_missing"})
    async_guarantees = {item.lower() for item in _normalize_text_list(async_cfg.get("required_delivery_guarantees", []))}
    missing_guarantees = sorted(REQUIRED_ASYNC_GUARANTEES - async_guarantees)
    if missing_guarantees:
        violations.append({"type": "delivery_guarantees_missing", "missing": missing_guarantees})

    adapters = protocols.get("adapters", {}) if isinstance(protocols.get("adapters"), dict) else {}
    adapter_ids = {item.lower() for item in _normalize_text_list(adapters.get("required", []))}
    missing_adapters = sorted(REQUIRED_ADAPTERS - adapter_ids)
    if missing_adapters:
        violations.append({"type": "adapter_missing", "missing": missing_adapters})
    parity_protocols = {item.lower() for item in _normalize_text_list(adapters.get("parity_with", []))}
    missing_parity = sorted(REQUIRED_PARITY_PROTOCOLS - parity_protocols)
    if missing_parity:
        violations.append({"type": "adapter_parity_missing", "missing": missing_parity})
    if not _as_bool(adapters.get("deterministic_fallback_required", False), False):
        violations.append({"type": "adapter_fallback_not_deterministic"})

    contracts = policy.get("contracts", {}) if isinstance(policy.get("contracts"), dict) else {}
    for key in {"openapi_required", "json_schema_required", "schema_registry_required"}:
        if not _as_bool(contracts.get(key, False), False):
            violations.append({"type": "contract_control_missing", "field": key})

    compatibility = contracts.get("compatibility", {}) if isinstance(contracts.get("compatibility"), dict) else {}
    for key in {"backward_required", "forward_required", "breaking_change_requires_exemption", "exemption_audit_required"}:
        if not _as_bool(compatibility.get(key, False), False):
            violations.append({"type": "compatibility_control_missing", "field": key})
    if _as_int(compatibility.get("deprecation_window_days_min", 0), 0) < 30:
        violations.append({"type": "deprecation_window_too_small"})

    security = policy.get("security", {}) if isinstance(policy.get("security"), dict) else {}
    security_controls = {item.lower() for item in _normalize_text_list(security.get("required_controls", []))}
    missing_security_controls = sorted(REQUIRED_SECURITY_CONTROLS - security_controls)
    if missing_security_controls:
        violations.append({"type": "security_controls_missing", "missing": missing_security_controls})
    for key in {"tenant_isolation_required", "rate_limits_required", "webhook_signing_required"}:
        if not _as_bool(security.get(key, False), False):
            violations.append({"type": "security_control_missing", "field": key})
    breakglass_fields = {item.lower() for item in _normalize_text_list(security.get("breakglass_required_fields", []))}
    missing_breakglass = sorted(REQUIRED_BREAKGLASS_FIELDS - breakglass_fields)
    if missing_breakglass:
        violations.append({"type": "security_breakglass_fields_missing", "missing": missing_breakglass})

    execution = (
        policy.get("routing_and_execution", {})
        if isinstance(policy.get("routing_and_execution"), dict)
        else {}
    )
    for key in {
        "local_first_required",
        "cloud_requires_explicit_trigger",
        "user_mode_only",
        "github_coordination_required",
    }:
        if not _as_bool(execution.get(key, False), False):
            violations.append({"type": "execution_control_missing", "field": key})

    observability = policy.get("observability", {}) if isinstance(policy.get("observability"), dict) else {}
    for key in {
        "slo_required",
        "trace_required",
        "developer_diagnostics_required",
        "causal_learning_required",
        "version_capture_required",
    }:
        if not _as_bool(observability.get(key, False), False):
            violations.append({"type": "observability_control_missing", "field": key})
    telemetry_dims = {item.lower() for item in _normalize_text_list(observability.get("required_dimensions", []))}
    missing_dims = sorted(REQUIRED_TELEMETRY_DIMS - telemetry_dims)
    if missing_dims:
        violations.append({"type": "observability_dimension_missing", "missing": missing_dims})

    release_gates = policy.get("release_gates", {}) if isinstance(policy.get("release_gates"), dict) else {}
    for key in {
        "conformance_suite_required",
        "compatibility_checks_required",
        "sdk_generated_from_contracts_required",
        "docs_generated_from_contracts_required",
        "block_promotion_on_failure",
    }:
        if not _as_bool(release_gates.get(key, False), False):
            violations.append({"type": "release_gate_missing", "field": key})

    backlog_tasks = backlog.get("tasks", []) if isinstance(backlog.get("tasks"), list) else []
    task_map: dict[str, dict[str, Any]] = {}
    for row in backlog_tasks:
        if not isinstance(row, dict):
            continue
        task_id = _as_text(row.get("id", ""))
        if task_id:
            task_map[task_id] = row

    for task_id in REQUIRED_BACKLOG_TASKS:
        if task_id not in task_map:
            violations.append({"type": "backlog_task_missing", "task_id": task_id})

    for task_id in sorted(activation_backlog_tasks | activation_api_tasks):
        if task_id not in task_map:
            violations.append({"type": "activation_task_missing_in_backlog", "task_id": task_id})

    def deps_for(task_id: str) -> set[str]:
        row = task_map.get(task_id, {})
        deps = row.get("dependencies", []) if isinstance(row, dict) else []
        if not isinstance(deps, list):
            return set()
        return {_as_text(item) for item in deps if _as_text(item)}

    dependency_checks_passed = 0
    for task_id, expected_deps in EXPECTED_BACKLOG_DEPENDENCIES.items():
        deps = deps_for(task_id)
        missing = sorted(expected_deps - deps)
        if missing:
            violations.append({"type": "backlog_dependency_missing", "task_id": task_id, "missing": missing})
        else:
            dependency_checks_passed += 1

    ok = len(violations) == 0

    return {
        "schema_version": "api-interop-policy-health.v1",
        "generated_at_utc": _utc_now_iso(),
        "ok": ok,
        "summary": {
            "violation_count": len(violations),
            "release_phase": release_phase,
            "protocol_count": len(protocol_ids),
            "standards_count": len(standard_ids),
            "adapter_count": len(adapter_ids),
            "security_control_count": len(security_controls),
            "telemetry_dimension_count": len(telemetry_dims),
            "backlog_task_count": len(task_map),
            "activation_prereq_checks_passed": activation_checks_passed,
            "dependency_checks_passed": dependency_checks_passed,
            "backlog_parse_mode": backlog_parse_mode,
        },
        "artifacts": {
            "policy": {},
            "backlog": {},
        },
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate external API interoperability policy readiness.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY), help="API interoperability policy JSON path.")
    parser.add_argument("--backlog-file", default=str(DEFAULT_BACKLOG), help="Backlog YAML path.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output JSON report path.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when policy report is unhealthy.")
    parser.add_argument("--json", action="store_true", help="Print compact JSON summary to stdout.")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()

    policy_path = Path(args.policy_file).expanduser()
    if not policy_path.is_absolute():
        policy_path = (root / policy_path).resolve()

    backlog_path = Path(args.backlog_file).expanduser()
    if not backlog_path.is_absolute():
        backlog_path = (root / backlog_path).resolve()

    output_path = Path(args.output).expanduser()
    if not output_path.is_absolute():
        output_path = (root / output_path).resolve()

    policy = _load_json(policy_path)
    backlog_payload, backlog_parse_mode = _load_backlog(backlog_path)

    report = evaluate(policy=policy, backlog=backlog_payload, backlog_parse_mode=backlog_parse_mode)
    report["artifacts"] = {
        "policy": {
            "path": str(policy_path),
            "exists": policy_path.exists(),
            "parse_ok": bool(policy),
        },
        "backlog": {
            "path": str(backlog_path),
            "exists": backlog_path.exists(),
            "parse_ok": bool(backlog_payload),
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    if args.json:
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        print(
            json.dumps(
                {
                    "output": str(output_path),
                    "ok": report.get("ok", False),
                    "violation_count": summary.get("violation_count", 0),
                    "release_phase": summary.get("release_phase", ""),
                    "activation_prereq_checks_passed": summary.get("activation_prereq_checks_passed", 0),
                    "dependency_checks_passed": summary.get("dependency_checks_passed", 0),
                },
                sort_keys=True,
            )
        )

    if args.strict and not _as_bool(report.get("ok", False), False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
