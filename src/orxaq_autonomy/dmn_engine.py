"""Deterministic DMN-style decision table evaluator for scaling policy."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _inputs_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DmnRule:
    rule_id: str
    conditions: dict[str, Any]
    output: dict[str, Any]


@dataclass(frozen=True)
class DmnDecisionTable:
    version: str
    rules: tuple[DmnRule, ...]
    default_output: dict[str, Any]


def _match_condition(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if "eq" in expected:
            return actual == expected.get("eq")
        if "gt" in expected:
            return _int_value(actual, 0) > _int_value(expected.get("gt"), 0)
        if "gte" in expected:
            return _int_value(actual, 0) >= _int_value(expected.get("gte"), 0)
        if "lt" in expected:
            return _int_value(actual, 0) < _int_value(expected.get("lt"), 0)
        if "lte" in expected:
            return _int_value(actual, 0) <= _int_value(expected.get("lte"), 0)
        if "in" in expected and isinstance(expected.get("in"), list):
            return actual in expected.get("in")
        return False
    return actual == expected


def _rule_matches(rule: DmnRule, facts: dict[str, Any]) -> bool:
    for key, expected in rule.conditions.items():
        if not _match_condition(facts.get(key), expected):
            return False
    return True


def _default_scaling_table() -> DmnDecisionTable:
    return DmnDecisionTable(
        version="scaling_v1",
        rules=(
            DmnRule(
                rule_id="failures_present_scale_down",
                conditions={"failed_count": {"gt": 0}},
                output={"action": "scale_down", "reason": "failures_present", "target_delta": -1},
            ),
            DmnRule(
                rule_id="capacity_saturated_scale_up",
                conditions={
                    "parallel_groups_at_limit": {"gt": 0},
                    "started_count": {"eq": 0},
                    "restarted_count": {"eq": 0},
                },
                output={"action": "scale_up", "reason": "capacity_saturated", "target_delta": 1},
            ),
            DmnRule(
                rule_id="recent_scale_down_hold",
                conditions={"scaled_down_count": {"gt": 0}},
                output={"action": "hold", "reason": "recent_scale_down", "target_delta": 0},
            ),
            DmnRule(
                rule_id="recent_scale_up_hold",
                conditions={"scaled_up_count": {"gt": 0}},
                output={"action": "hold", "reason": "recent_scale_up", "target_delta": 0},
            ),
        ),
        default_output={"action": "hold", "reason": "stable", "target_delta": 0},
    )


def _table_from_payload(payload: dict[str, Any]) -> DmnDecisionTable:
    version = str(payload.get("version", "scaling_v1")).strip() or "scaling_v1"
    raw_rules = payload.get("rules", [])
    rules: list[DmnRule] = []
    if isinstance(raw_rules, list):
        for index, item in enumerate(raw_rules):
            if not isinstance(item, dict):
                continue
            conditions = item.get("conditions", {})
            output = item.get("output", {})
            if not isinstance(conditions, dict) or not isinstance(output, dict):
                continue
            rule_id = str(item.get("rule_id", f"rule_{index + 1}")).strip() or f"rule_{index + 1}"
            rules.append(DmnRule(rule_id=rule_id, conditions=conditions, output=output))
    default_output = payload.get("default_output", {})
    if not isinstance(default_output, dict):
        default_output = {}
    if not rules:
        return _default_scaling_table()
    return DmnDecisionTable(version=version, rules=tuple(rules), default_output=default_output)


def load_scaling_decision_table(root_dir: Path) -> DmnDecisionTable:
    env_path = str(os.environ.get("ORXAQ_AUTONOMY_SCALING_DMN_TABLE_FILE", "")).strip()
    table_path = Path(env_path).resolve() if env_path else (root_dir / "config" / "autonomy" / "scaling_dmn_table.json").resolve()
    if not table_path.exists():
        return _default_scaling_table()
    try:
        payload = json.loads(table_path.read_text(encoding="utf-8"))
    except Exception:
        return _default_scaling_table()
    if not isinstance(payload, dict):
        return _default_scaling_table()
    return _table_from_payload(payload)


def evaluate_scaling_decision(
    *,
    facts: dict[str, Any],
    table: DmnDecisionTable,
) -> dict[str, Any]:
    matched_rule_ids: list[str] = []
    output = dict(table.default_output)
    for rule in table.rules:
        if not _rule_matches(rule, facts):
            continue
        matched_rule_ids.append(rule.rule_id)
        output = dict(rule.output)
        break
    action = str(output.get("action", "hold")).strip() or "hold"
    reason = str(output.get("reason", "stable")).strip() or "stable"
    target_delta = _int_value(output.get("target_delta", 0), 0)
    return {
        "action": action,
        "reason": reason,
        "target_delta": target_delta,
        "decision_trace": {
            "decision_table_version": table.version,
            "matched_rule_ids": matched_rule_ids,
            "inputs_hash": _inputs_hash(facts),
        },
    }
