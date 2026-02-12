#!/usr/bin/env python3
"""Validate backend portfolio and upgrade lifecycle policy readiness."""

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


DEFAULT_BACKEND_POLICY = Path("config/backend_portfolio_policy.json")
DEFAULT_UPGRADE_POLICY = Path("config/upgrade_lifecycle_policy.json")
DEFAULT_BACKLOG = Path("../orxaq/ops/backlog/distributed_todo.yaml")
DEFAULT_OUTPUT = Path("artifacts/autonomy/backend_upgrade_policy_health.json")

REQUIRED_BACKENDS = {"spark", "dask", "spark_jit_numpy_hybrid"}
REQUIRED_VARIANTS = {"pandas", "numpy", "jit_numpy", "narwhals", "ibis"}
REQUIRED_ROUTING_CRITERIA = {
    "latency",
    "throughput",
    "cost",
    "determinism",
    "correctness",
    "memory_pressure",
    "startup_overhead",
    "operability",
    "failure_blast_radius",
}
REQUIRED_RELEASE_STATES = [
    "preflight",
    "shadow",
    "canary",
    "ramp",
    "steady",
    "deprecate",
    "retire",
]
REQUIRED_UPGRADE_ACTIVATION_TASKS = {"B9-T2", "B10-T7", "B10-T8"}
REQUIRED_BREAKGLASS_FIELDS = {"reason", "scope", "ttl", "rollback_proof", "audit_trail"}


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


def evaluate(
    *,
    backend_policy: dict[str, Any],
    upgrade_policy: dict[str, Any],
    backlog: dict[str, Any],
    backlog_parse_mode: str,
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []

    backend_schema = _as_text(backend_policy.get("schema_version"))
    if not backend_schema.startswith("backend-portfolio-policy.v"):
        violations.append({"type": "backend_schema_invalid", "value": backend_schema})

    upgrade_schema = _as_text(upgrade_policy.get("schema_version"))
    if not upgrade_schema.startswith("upgrade-lifecycle-policy.v"):
        violations.append({"type": "upgrade_schema_invalid", "value": upgrade_schema})

    release_phase = _as_text(backend_policy.get("release_phase", "foundation")).lower() or "foundation"
    if release_phase not in {"foundation", "late_stage"}:
        violations.append({"type": "release_phase_invalid", "value": release_phase})

    portfolio = backend_policy.get("portfolio", {}) if isinstance(backend_policy.get("portfolio"), dict) else {}
    backend_rows = _as_list(portfolio.get("backends"))
    variant_rows = _as_list(portfolio.get("coding_variants"))

    backend_ids: set[str] = set()
    for row in backend_rows:
        if not isinstance(row, dict):
            continue
        backend_id = _as_text(row.get("id", "")).lower()
        if backend_id:
            backend_ids.add(backend_id)
        supports_user_mode = _as_bool(row.get("supports_user_mode", False), False)
        if not supports_user_mode:
            violations.append({"type": "backend_not_user_mode", "backend_id": backend_id or "unknown"})

    missing_backends = sorted(REQUIRED_BACKENDS - backend_ids)
    if missing_backends:
        violations.append({"type": "missing_required_backends", "missing": missing_backends})

    variant_ids = {
        _as_text(row.get("id", "")).lower()
        for row in variant_rows
        if isinstance(row, dict) and _as_text(row.get("id", ""))
    }
    missing_variants = sorted(REQUIRED_VARIANTS - variant_ids)
    if missing_variants:
        violations.append({"type": "missing_required_variants", "missing": missing_variants})

    routing = backend_policy.get("routing", {}) if isinstance(backend_policy.get("routing"), dict) else {}
    if not _as_bool(routing.get("specialist_router_required", False), False):
        violations.append({"type": "specialist_router_required_false"})
    routing_criteria = {item.lower() for item in _normalize_text_list(routing.get("criteria", []))}
    missing_routing_criteria = sorted(REQUIRED_ROUTING_CRITERIA - routing_criteria)
    if missing_routing_criteria:
        violations.append({"type": "missing_routing_criteria", "missing": missing_routing_criteria})
    if not _as_bool(routing.get("cloud_escalation_requires_trigger", False), False):
        violations.append({"type": "cloud_trigger_policy_missing"})

    distributed_mode = (
        backend_policy.get("distributed_mode", {}) if isinstance(backend_policy.get("distributed_mode"), dict) else {}
    )
    if not _as_bool(distributed_mode.get("github_coordination_required", False), False):
        violations.append({"type": "github_coordination_not_required"})
    if not _as_bool(distributed_mode.get("user_mode_only", False), False):
        violations.append({"type": "user_mode_only_not_enforced"})

    consent = distributed_mode.get("organization_opt_in_compute", {})
    consent = consent if isinstance(consent, dict) else {}
    if not _as_bool(consent.get("enabled", False), False):
        violations.append({"type": "org_opt_in_compute_disabled"})
    consent_fields = {item.lower() for item in _normalize_text_list(consent.get("required_fields", []))}
    for required_field in {
        "user_id",
        "machine_id",
        "consent_reason",
        "granted_at",
        "expires_at",
        "revoked",
        "revoked_at",
        "revoke_reason",
    }:
        if required_field not in consent_fields:
            violations.append({"type": "consent_required_field_missing", "field": required_field})

    os_parity = backend_policy.get("os_parity", {}) if isinstance(backend_policy.get("os_parity"), dict) else {}
    os_required = {item.lower() for item in _normalize_text_list(os_parity.get("required_os", []))}
    for required_os in {"macos", "windows"}:
        if required_os not in os_required:
            violations.append({"type": "os_parity_missing", "os": required_os})

    telemetry = backend_policy.get("telemetry", {}) if isinstance(backend_policy.get("telemetry"), dict) else {}
    if not _as_bool(telemetry.get("require_version_capture", False), False):
        violations.append({"type": "telemetry_version_capture_disabled"})
    telemetry_dims = {item.lower() for item in _normalize_text_list(telemetry.get("required_dimensions", []))}
    for dim in {
        "backend_id",
        "coding_variant",
        "dataset_profile",
        "package_versions",
        "latency_ms",
        "quality_score",
    }:
        if dim not in telemetry_dims:
            violations.append({"type": "telemetry_dimension_missing", "dimension": dim})

    causal = backend_policy.get("causal_learning", {}) if isinstance(backend_policy.get("causal_learning"), dict) else {}
    if not _as_bool(causal.get("enabled", False), False):
        violations.append({"type": "causal_learning_disabled"})
    if not _as_bool(causal.get("require_hypothesis_ids", False), False):
        violations.append({"type": "causal_hypothesis_id_not_required"})

    activation = upgrade_policy.get("activation", {}) if isinstance(upgrade_policy.get("activation"), dict) else {}
    if not _as_bool(activation.get("routing_mechanism_required", False), False):
        violations.append({"type": "activation_missing_routing_prereq"})
    if not _as_bool(activation.get("ab_testing_required", False), False):
        violations.append({"type": "activation_missing_ab_prereq"})
    activation_tasks = {item for item in _normalize_text_list(activation.get("requires_backlog_tasks", []))}
    missing_activation_tasks = sorted(REQUIRED_UPGRADE_ACTIVATION_TASKS - activation_tasks)
    if missing_activation_tasks:
        violations.append({"type": "activation_missing_backlog_tasks", "missing": missing_activation_tasks})

    state_machine = upgrade_policy.get("state_machine", {}) if isinstance(upgrade_policy.get("state_machine"), dict) else {}
    states = _normalize_text_list(state_machine.get("ordered_states", []))
    if states != REQUIRED_RELEASE_STATES:
        violations.append({"type": "release_state_machine_invalid", "states": states})
    if _as_text(state_machine.get("invalid_transition_action", "")).lower() != "block":
        violations.append({"type": "invalid_transition_action_not_block"})

    rollout = upgrade_policy.get("rollout", {}) if isinstance(upgrade_policy.get("rollout"), dict) else {}
    rollout_strategies = {item.lower() for item in _normalize_text_list(rollout.get("strategies", []))}
    for strategy in {"shadow", "canary", "weighted_ramp"}:
        if strategy not in rollout_strategies:
            violations.append({"type": "rollout_strategy_missing", "strategy": strategy})
    weight_steps = [_as_int(item, -1) for item in _as_list(rollout.get("weight_steps_percent", []))]
    if weight_steps != [1, 5, 10, 25, 50, 100]:
        violations.append({"type": "rollout_weight_steps_invalid", "value": weight_steps})

    scale = upgrade_policy.get("scale", {}) if isinstance(upgrade_policy.get("scale"), dict) else {}
    if not _as_bool(scale.get("old_new_coexistence_required", False), False):
        violations.append({"type": "old_new_coexistence_not_required"})
    if not _as_bool(scale.get("graceful_scale_down_required", False), False):
        violations.append({"type": "graceful_scale_down_not_required"})
    if _as_int(scale.get("rollback_headroom_percent_min", 0), 0) <= 0:
        violations.append({"type": "rollback_headroom_invalid"})

    migration = upgrade_policy.get("migration", {}) if isinstance(upgrade_policy.get("migration"), dict) else {}
    for key in {"schema_contract_required", "forward_backward_compat_required", "reversible_when_feasible"}:
        if not _as_bool(migration.get(key, False), False):
            violations.append({"type": "migration_control_missing", "field": key})

    safety = upgrade_policy.get("safety", {}) if isinstance(upgrade_policy.get("safety"), dict) else {}
    for key in {"automatic_pause_on_warning", "automatic_rollback_on_hard_fail"}:
        if not _as_bool(safety.get(key, False), False):
            violations.append({"type": "safety_control_missing", "field": key})
    breakglass_fields = {item.lower() for item in _normalize_text_list(safety.get("breakglass_required_fields", []))}
    missing_breakglass = sorted(REQUIRED_BREAKGLASS_FIELDS - breakglass_fields)
    if missing_breakglass:
        violations.append({"type": "upgrade_breakglass_fields_missing", "missing": missing_breakglass})

    environment = upgrade_policy.get("environment", {}) if isinstance(upgrade_policy.get("environment"), dict) else {}
    targets = {item.lower() for item in _normalize_text_list(environment.get("required_targets", []))}
    for target in {"macos", "windows", "local", "cloud"}:
        if target not in targets:
            violations.append({"type": "environment_target_missing", "target": target})
    if not _as_bool(environment.get("cloud_requires_explicit_trigger", False), False):
        violations.append({"type": "cloud_trigger_not_required_in_upgrade_policy"})

    backlog_tasks = backlog.get("tasks", []) if isinstance(backlog.get("tasks"), list) else []
    task_map: dict[str, dict[str, Any]] = {}
    for row in backlog_tasks:
        if not isinstance(row, dict):
            continue
        task_id = _as_text(row.get("id", ""))
        if task_id:
            task_map[task_id] = row

    for task_id in {"B10-EPIC", "B10-T7", "B10-T8", "B11-EPIC", "B11-T2", "B11-T5"}:
        if task_id not in task_map:
            violations.append({"type": "backlog_task_missing", "task_id": task_id})

    def deps_for(task_id: str) -> set[str]:
        row = task_map.get(task_id, {})
        deps = row.get("dependencies", []) if isinstance(row, dict) else []
        if isinstance(deps, list):
            return { _as_text(item) for item in deps if _as_text(item)}
        return set()

    expected_dep_checks = {
        "B11-EPIC": {"B9-T2", "B10-T7", "B10-T8"},
        "B11-T2": {"B9-T2", "B10-T7"},
        "B11-T5": {"B11-T2", "B11-T4"},
    }
    dependency_checks_passed = 0
    for task_id, expected in expected_dep_checks.items():
        deps = deps_for(task_id)
        missing = sorted(expected - deps)
        if missing:
            violations.append({"type": "backlog_dependency_missing", "task_id": task_id, "missing": missing})
        else:
            dependency_checks_passed += 1

    ok = len(violations) == 0

    return {
        "schema_version": "backend-upgrade-policy-health.v1",
        "generated_at_utc": _utc_now_iso(),
        "ok": ok,
        "summary": {
            "violation_count": len(violations),
            "release_phase": release_phase,
            "backend_count": len(backend_ids),
            "variant_count": len(variant_ids),
            "required_backends_present_count": len(REQUIRED_BACKENDS - set(missing_backends)),
            "required_variants_present_count": len(REQUIRED_VARIANTS - set(missing_variants)),
            "routing_criteria_count": len(routing_criteria),
            "upgrade_state_count": len(states),
            "backlog_task_count": len(task_map),
            "activation_task_checks_passed": len(REQUIRED_UPGRADE_ACTIVATION_TASKS - set(missing_activation_tasks)),
            "dependency_checks_passed": dependency_checks_passed,
            "backlog_parse_mode": backlog_parse_mode,
        },
        "artifacts": {
            "backend_policy": {},
            "upgrade_policy": {},
            "backlog": {},
        },
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate backend portfolio and upgrade lifecycle policy readiness.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--backend-policy-file", default=str(DEFAULT_BACKEND_POLICY), help="Backend portfolio policy JSON path.")
    parser.add_argument("--upgrade-policy-file", default=str(DEFAULT_UPGRADE_POLICY), help="Upgrade lifecycle policy JSON path.")
    parser.add_argument("--backlog-file", default=str(DEFAULT_BACKLOG), help="Backlog YAML path.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output JSON report path.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when policy report is unhealthy.")
    parser.add_argument("--json", action="store_true", help="Print compact JSON summary to stdout.")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()

    backend_policy_path = Path(args.backend_policy_file).expanduser()
    if not backend_policy_path.is_absolute():
        backend_policy_path = (root / backend_policy_path).resolve()

    upgrade_policy_path = Path(args.upgrade_policy_file).expanduser()
    if not upgrade_policy_path.is_absolute():
        upgrade_policy_path = (root / upgrade_policy_path).resolve()

    backlog_path = Path(args.backlog_file).expanduser()
    if not backlog_path.is_absolute():
        backlog_path = (root / backlog_path).resolve()

    output_path = Path(args.output).expanduser()
    if not output_path.is_absolute():
        output_path = (root / output_path).resolve()

    backend_policy = _load_json(backend_policy_path)
    upgrade_policy = _load_json(upgrade_policy_path)
    backlog_payload, backlog_parse_mode = _load_backlog(backlog_path)

    report = evaluate(
        backend_policy=backend_policy,
        upgrade_policy=upgrade_policy,
        backlog=backlog_payload,
        backlog_parse_mode=backlog_parse_mode,
    )

    report["artifacts"] = {
        "backend_policy": {
            "path": str(backend_policy_path),
            "exists": backend_policy_path.exists(),
            "parse_ok": bool(backend_policy),
        },
        "upgrade_policy": {
            "path": str(upgrade_policy_path),
            "exists": upgrade_policy_path.exists(),
            "parse_ok": bool(upgrade_policy),
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
