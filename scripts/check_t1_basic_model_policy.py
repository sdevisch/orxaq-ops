#!/usr/bin/env python3
"""Enforce T1-model usage for basic coding tasks unless explicitly escalated."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_POLICY = Path("config/t1_model_policy.json")
DEFAULT_METRICS = Path("artifacts/autonomy/response_metrics.ndjson")
DEFAULT_OUTPUT = Path("artifacts/autonomy/t1_basic_model_policy.json")


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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
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


def _field_present(payload: dict[str, Any], field_name: str) -> bool:
    value = payload.get(field_name)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _matches_any_regex(patterns: list[re.Pattern[str]], text: str) -> bool:
    if not text:
        return False
    for pattern in patterns:
        if pattern.search(text):
            return True
    return False


def evaluate_policy(
    *,
    metrics: list[dict[str, Any]],
    policy: dict[str, Any],
    lookback_hours_override: int | None = None,
) -> dict[str, Any]:
    enabled = _as_bool(policy.get("enabled", True), True)
    lookback_hours = max(1, lookback_hours_override if lookback_hours_override is not None else _as_int(policy.get("lookback_hours", 168), 168))
    basic_max_difficulty = _as_int(policy.get("basic_task_max_difficulty", 55), 55)
    max_violations = max(0, _as_int(policy.get("max_violations", 0), 0))

    t1_models = set(_normalize_list(policy.get("t1_models", [])))
    t1_prefixes = _normalize_list(policy.get("t1_model_prefixes", []))

    escalation = policy.get("escalation", {}) if isinstance(policy.get("escalation"), dict) else {}
    escalation_min_difficulty = _as_int(escalation.get("min_difficulty", 70), 70)
    escalation_reasons = {item.lower() for item in _normalize_list(escalation.get("routing_reason_allowlist", []))}
    escalation_tasks = set(_normalize_list(escalation.get("task_allowlist", [])))
    escalation_task_regex_raw = _normalize_list(escalation.get("task_regex_allowlist", []))
    escalation_notes_regex_raw = _normalize_list(escalation.get("notes_regex", []))

    monitoring = policy.get("monitoring", {}) if isinstance(policy.get("monitoring"), dict) else {}
    require_recent_metrics = _as_bool(monitoring.get("require_recent_metrics", True), True)
    max_metrics_age_minutes = max(1, _as_int(monitoring.get("max_metrics_age_minutes", 240), 240))
    min_scanned_metrics = max(0, _as_int(monitoring.get("min_scanned_metrics", 1), 1))
    max_parse_skip_ratio = _as_float(monitoring.get("max_parse_skip_ratio", 0.1), 0.1)
    if max_parse_skip_ratio < 0.0:
        max_parse_skip_ratio = 0.0
    if max_parse_skip_ratio > 1.0:
        max_parse_skip_ratio = 1.0
    required_fields = _normalize_list(monitoring.get("required_fields", ["task_id", "prompt_difficulty_score", "routing_reason"]))

    escalation_task_regex: list[re.Pattern[str]] = []
    escalation_notes_regex: list[re.Pattern[str]] = []
    for pattern in escalation_task_regex_raw:
        try:
            escalation_task_regex.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    for pattern in escalation_notes_regex_raw:
        try:
            escalation_notes_regex.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue

    cutoff = _now_utc() - timedelta(hours=lookback_hours)

    scanned = 0
    basic_tasks = 0
    escalated_tasks = 0
    compliant_basic_tasks = 0
    violations: list[dict[str, Any]] = []
    parse_skips = 0
    timestamp_parsed_rows = 0
    latest_metric_ts: datetime | None = None
    telemetry_missing_required_count = 0
    telemetry_missing_model_count = 0
    telemetry_violations: list[dict[str, Any]] = []

    for row in metrics:
        ts = _parse_iso(row.get("timestamp"))
        if ts is None:
            parse_skips += 1
            continue
        timestamp_parsed_rows += 1
        if latest_metric_ts is None or ts > latest_metric_ts:
            latest_metric_ts = ts
        if ts < cutoff:
            continue

        scanned += 1
        task_id = _as_text(row.get("task_id", ""))
        difficulty = _as_int(row.get("prompt_difficulty_score", 0), 0)
        routing_reason = _as_text(row.get("routing_reason", "")).lower()
        notes = _as_text(row.get("notes", ""))
        summary = _as_text(row.get("summary", ""))
        model = _as_text(row.get("routing_selected_model", "")) or _as_text(row.get("model", ""))

        missing_fields: list[str] = []
        for field_name in required_fields:
            if not _field_present(row, field_name):
                missing_fields.append(field_name)
        if missing_fields:
            telemetry_missing_required_count += 1
            telemetry_violations.append(
                {
                    "type": "missing_required_fields",
                    "timestamp": ts.isoformat().replace("+00:00", "Z"),
                    "task_id": task_id,
                    "missing_fields": missing_fields,
                }
            )

        if not model:
            telemetry_missing_model_count += 1
            telemetry_violations.append(
                {
                    "type": "missing_model",
                    "timestamp": ts.isoformat().replace("+00:00", "Z"),
                    "task_id": task_id,
                    "missing_fields": ["routing_selected_model", "model"],
                }
            )

        is_basic = difficulty <= basic_max_difficulty
        if not is_basic:
            continue
        basic_tasks += 1

        escalated = False
        if difficulty >= escalation_min_difficulty:
            escalated = True
        if routing_reason and routing_reason in escalation_reasons:
            escalated = True
        if task_id and task_id in escalation_tasks:
            escalated = True
        if not escalated and _matches_any_regex(escalation_task_regex, task_id):
            escalated = True
        if not escalated and _matches_any_regex(escalation_notes_regex, notes):
            escalated = True
        if not escalated and _matches_any_regex(escalation_notes_regex, summary):
            escalated = True
        if escalated:
            escalated_tasks += 1

        t1_match = False
        if model in t1_models:
            t1_match = True
        if not t1_match:
            for prefix in t1_prefixes:
                if prefix and model.startswith(prefix):
                    t1_match = True
                    break

        if t1_match or escalated:
            compliant_basic_tasks += 1
            continue

        violations.append(
            {
                "timestamp": ts.isoformat().replace("+00:00", "Z"),
                "task_id": task_id,
                "difficulty": difficulty,
                "selected_model": model,
                "routing_reason": routing_reason,
                "notes": notes[:180],
                "summary": summary[:180],
            }
        )

    violation_count = len(violations)
    parse_ratio_denominator = parse_skips + timestamp_parsed_rows
    parse_skip_ratio = (parse_skips / parse_ratio_denominator) if parse_ratio_denominator > 0 else 0.0

    latest_metric_age_minutes = -1
    if latest_metric_ts is not None:
        age_sec = (_now_utc() - latest_metric_ts).total_seconds()
        latest_metric_age_minutes = max(0, int(age_sec // 60))

    parse_quality_ok = parse_skip_ratio <= max_parse_skip_ratio
    freshness_ok = (not require_recent_metrics) or (
        latest_metric_age_minutes >= 0 and latest_metric_age_minutes <= max_metrics_age_minutes
    )
    min_volume_ok = scanned >= min_scanned_metrics
    required_fields_ok = telemetry_missing_required_count == 0 and telemetry_missing_model_count == 0
    idle_freshness_waiver = (
        not freshness_ok
        and parse_quality_ok
        and min_volume_ok
        and required_fields_ok
        and basic_tasks == 0
        and violation_count == 0
    )
    observability_ok = parse_quality_ok and (freshness_ok or idle_freshness_waiver) and min_volume_ok and required_fields_ok

    ok = observability_ok and (violation_count <= max_violations if enabled else True)

    return {
        "schema_version": "t1-basic-model-policy-report.v1",
        "generated_at_utc": _utc_now_iso(),
        "ok": ok,
        "enabled": enabled,
        "lookback_hours": lookback_hours,
        "basic_task_max_difficulty": basic_max_difficulty,
        "max_violations": max_violations,
        "summary": {
            "scanned_metrics": scanned,
            "parse_skips": parse_skips,
            "basic_tasks": basic_tasks,
            "basic_tasks_escalated": escalated_tasks,
            "basic_tasks_compliant": compliant_basic_tasks,
            "violation_count": violation_count,
            "observability_ok": observability_ok,
            "latest_metric_age_minutes": latest_metric_age_minutes,
            "parse_skip_ratio": round(parse_skip_ratio, 4),
            "telemetry_missing_required_rows": telemetry_missing_required_count,
            "telemetry_missing_model_rows": telemetry_missing_model_count,
            "idle_freshness_waiver": idle_freshness_waiver,
        },
        "policy": {
            "t1_models": sorted(t1_models),
            "t1_model_prefixes": t1_prefixes,
            "escalation_min_difficulty": escalation_min_difficulty,
            "escalation_routing_reasons": sorted(escalation_reasons),
            "escalation_task_allowlist": sorted(escalation_tasks),
            "escalation_task_regex": escalation_task_regex_raw,
            "escalation_notes_regex": escalation_notes_regex_raw,
        },
        "observability": {
            "ok": observability_ok,
            "require_recent_metrics": require_recent_metrics,
            "max_metrics_age_minutes": max_metrics_age_minutes,
            "latest_metric_age_minutes": latest_metric_age_minutes,
            "min_scanned_metrics": min_scanned_metrics,
            "scanned_metrics": scanned,
            "max_parse_skip_ratio": max_parse_skip_ratio,
            "parse_skip_ratio": parse_skip_ratio,
            "required_fields": required_fields,
            "missing_required_field_rows": telemetry_missing_required_count,
            "missing_model_rows": telemetry_missing_model_count,
            "freshness_ok": freshness_ok,
            "idle_freshness_waiver": idle_freshness_waiver,
        },
        "violations": violations,
        "telemetry_violations": telemetry_violations,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate that basic coding tasks use T1 models unless escalated.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY), help="Path to policy config JSON.")
    parser.add_argument("--metrics-file", default=str(DEFAULT_METRICS), help="Path to response metrics NDJSON.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output report JSON path.")
    parser.add_argument("--lookback-hours", type=int, default=0, help="Override policy lookback window in hours.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when violations exceed max_violations.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary line.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()

    policy_file = Path(args.policy_file)
    if not policy_file.is_absolute():
        policy_file = (root / policy_file).resolve()

    metrics_file = Path(args.metrics_file)
    if not metrics_file.is_absolute():
        metrics_file = (root / metrics_file).resolve()

    output_file = Path(args.output)
    if not output_file.is_absolute():
        output_file = (root / output_file).resolve()

    policy = _load_json(policy_file)
    metrics = _load_ndjson(metrics_file)
    report = evaluate_policy(
        metrics=metrics,
        policy=policy,
        lookback_hours_override=(max(1, int(args.lookback_hours)) if int(args.lookback_hours or 0) > 0 else None),
    )
    report["artifacts"] = {
        "policy_file": str(policy_file),
        "metrics_file": str(metrics_file),
        "output_file": str(output_file),
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    payload = {
        "output": str(output_file),
        "ok": bool(report.get("ok", False)),
        "violation_count": int((report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}).get("violation_count", 0) or 0),
        "basic_tasks": int((report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}).get("basic_tasks", 0) or 0),
        "observability_ok": bool((report.get("observability", {}) if isinstance(report.get("observability"), dict) else {}).get("ok", False)),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "t1-basic-model-policy "
            f"ok={payload['ok']} basic_tasks={payload['basic_tasks']} violations={payload['violation_count']}"
        )

    if args.strict and not bool(report.get("ok", False)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
