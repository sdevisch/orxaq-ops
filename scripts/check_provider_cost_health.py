#!/usr/bin/env python3
"""Validate freshness and provider health for provider-cost summary artifacts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _int_value(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_value(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _bool_value(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_required_providers(raw: str) -> list[str]:
    providers: list[str] = []
    for chunk in str(raw or "").split(","):
        name = chunk.strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered not in providers:
            providers.append(lowered)
    return providers


UNCONFIGURED_PROVIDER_STATUSES = {
    "",
    "skipped",
    "disabled",
    "unconfigured",
    "not_configured",
    "missing_key",
    "missing_api_key",
    "no_api_key",
    "missing_credentials",
    "no_credentials",
    "not_enabled",
}


def _provider_telemetry_mode(
    providers: list[dict[str, Any]],
    *,
    providers_ok: list[str],
    required_providers: list[str],
) -> str:
    if required_providers:
        return "required"
    if not providers:
        return "unconfigured"
    if providers_ok:
        return "configured"
    statuses: list[str] = []
    for item in providers:
        status_name = str(item.get("status", "")).strip().lower()
        statuses.append(status_name)
    if statuses and all(status_name in UNCONFIGURED_PROVIDER_STATUSES for status_name in statuses):
        return "unconfigured"
    return "degraded"


def evaluate_health(
    payload: dict[str, Any],
    *,
    max_age_sec: int,
    required_providers: list[str],
    allow_stale: bool,
    allow_unconfigured: bool,
    daily_budget_usd: float,
    budget_warning_ratio: float,
    budget_enforce_hard_stop: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []

    providers_raw = payload.get("providers", [])
    providers = providers_raw if isinstance(providers_raw, list) else []
    provider_status_map: dict[str, dict[str, Any]] = {}
    providers_ok: list[str] = []
    providers_non_ok: list[str] = []
    for item in providers:
        if not isinstance(item, dict):
            continue
        provider_name = str(item.get("provider", "")).strip().lower()
        if not provider_name:
            continue
        status_name = str(item.get("status", "")).strip() or "unknown"
        ok = bool(item.get("ok", False))
        provider_status_map[provider_name] = {"ok": ok, "status": status_name}
        if ok:
            providers_ok.append(provider_name)
        else:
            providers_non_ok.append(f"{provider_name}:{status_name}")

    telemetry_mode = _provider_telemetry_mode(
        providers,
        providers_ok=providers_ok,
        required_providers=required_providers,
    )
    unconfigured_provider_data = telemetry_mode == "unconfigured" and not required_providers
    provider_data_present = telemetry_mode in {"configured", "required"} or len(providers_non_ok) > 0

    summary_ok = bool(payload.get("ok", False))
    if not summary_ok:
        if allow_unconfigured and unconfigured_provider_data:
            warnings.append("summary_ok_false_unconfigured")
        else:
            failures.append("summary_ok_false")

    freshness = payload.get("data_freshness", {})
    if not isinstance(freshness, dict):
        freshness = {}
    age_sec = _int_value(freshness.get("age_sec", -1), -1)
    stale = bool(freshness.get("stale", age_sec < 0 or age_sec > max_age_sec))
    if age_sec < 0:
        if allow_unconfigured and unconfigured_provider_data:
            warnings.append("freshness_age_unknown_unconfigured")
        else:
            failures.append("freshness_age_unknown")
    if max_age_sec > 0 and age_sec >= 0 and age_sec > max_age_sec:
        if allow_unconfigured and unconfigured_provider_data:
            warnings.append(f"freshness_age_exceeded_unconfigured:{age_sec}>{max_age_sec}")
        else:
            failures.append(f"freshness_age_exceeded:{age_sec}>{max_age_sec}")
    if stale and not allow_stale:
        if allow_unconfigured and unconfigured_provider_data:
            warnings.append("freshness_stale_unconfigured")
        else:
            failures.append("freshness_stale")

    if required_providers:
        for provider_name in required_providers:
            provider_status = provider_status_map.get(provider_name)
            if provider_status is None:
                failures.append(f"required_provider_missing:{provider_name}")
                continue
            if not bool(provider_status.get("ok", False)):
                failures.append(
                    f"required_provider_not_ok:{provider_name}:{provider_status.get('status', 'unknown')}"
                )
    elif providers and not providers_ok:
        if allow_unconfigured and unconfigured_provider_data:
            warnings.append("no_provider_ok_unconfigured")
        else:
            failures.append("no_provider_ok")

    cost_windows = payload.get("cost_windows_usd", {})
    if not isinstance(cost_windows, dict):
        cost_windows = {}
    budget_cap = max(0.0, _float_value(daily_budget_usd, 0.0))
    warning_ratio = _float_value(budget_warning_ratio, 0.8)
    if not (0.0 < warning_ratio < 1.0):
        warning_ratio = 0.8
    spend_today = max(0.0, _float_value(cost_windows.get("today", 0.0), 0.0))
    spend_7d = max(0.0, _float_value(cost_windows.get("last_7d", 0.0), 0.0))
    budget_enabled = budget_cap > 0.0
    warning_threshold = budget_cap * warning_ratio if budget_enabled else 0.0
    budget_state = "disabled"
    utilization_ratio = 0.0
    if budget_enabled:
        utilization_ratio = spend_today / budget_cap if budget_cap > 0 else 0.0
        if spend_today > budget_cap:
            budget_state = "exceeded"
        elif spend_today >= warning_threshold:
            budget_state = "warning"
        else:
            budget_state = "ok"
    if budget_state == "warning":
        warnings.append(
            f"budget_warning:{spend_today:.6f}>={warning_threshold:.6f}"
        )
    if budget_state == "exceeded":
        failures.append(f"budget_daily_exceeded:{spend_today:.6f}>{budget_cap:.6f}")

    return {
        "ok": len(failures) == 0,
        "summary_ok": summary_ok,
        "timestamp": str(payload.get("timestamp", "")).strip(),
        "records_total": _int_value(payload.get("records_total", 0), 0),
        "age_sec": age_sec,
        "stale": stale,
        "stale_threshold_sec": _int_value(freshness.get("stale_threshold_sec", max_age_sec), max_age_sec),
        "max_age_sec": max_age_sec,
        "allow_stale": allow_stale,
        "required_providers": required_providers,
        "providers_ok": providers_ok,
        "providers_non_ok": providers_non_ok,
        "provider_telemetry_mode": telemetry_mode,
        "provider_data_present": provider_data_present,
        "allow_unconfigured": bool(allow_unconfigured),
        "failures": failures,
        "warnings": warnings,
        "budget": {
            "enabled": budget_enabled,
            "state": budget_state,
            "daily_budget_usd": round(budget_cap, 8),
            "daily_warning_threshold_usd": round(warning_threshold, 8),
            "daily_spend_usd": round(spend_today, 8),
            "daily_remaining_usd": round(max(0.0, budget_cap - spend_today) if budget_enabled else 0.0, 8),
            "rolling_7d_spend_usd": round(spend_7d, 8),
            "warning_ratio": round(warning_ratio, 6),
            "utilization_ratio": round(utilization_ratio, 6),
            "utilization_percent": round(utilization_ratio * 100.0, 2),
            "enforce_hard_stop": bool(budget_enforce_hard_stop),
            "hard_stop": bool(budget_enforce_hard_stop) and budget_enabled and spend_today >= budget_cap,
        },
    }


def _load_summary(summary_file: Path) -> tuple[dict[str, Any], str]:
    if not summary_file.exists():
        return {}, f"summary_missing:{summary_file}"
    if not summary_file.is_file():
        return {}, f"summary_not_file:{summary_file}"
    try:
        payload = json.loads(summary_file.read_text(encoding="utf-8"))
    except Exception as err:
        return {}, f"summary_parse_error:{err}"
    if not isinstance(payload, dict):
        return {}, "summary_must_be_object"
    return payload, ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate provider-cost summary freshness and provider status.")
    parser.add_argument(
        "--summary-file",
        default=str(ROOT / "artifacts" / "autonomy" / "provider_costs" / "summary.json"),
        help="Path to provider-cost summary JSON.",
    )
    parser.add_argument(
        "--max-age-sec",
        type=int,
        default=max(
            1,
            _int_value(
                os.getenv(
                    "ORXAQ_PROVIDER_COST_HEALTH_MAX_AGE_SEC",
                    os.getenv("ORXAQ_AUTONOMY_PROVIDER_COST_STALE_SEC", "900"),
                ),
                900,
            ),
        ),
        help="Maximum acceptable freshness age in seconds.",
    )
    parser.add_argument(
        "--require-providers",
        default=os.getenv("ORXAQ_PROVIDER_COST_HEALTH_REQUIRE_PROVIDERS", ""),
        help="Comma-separated provider names that must report ok status.",
    )
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Allow stale summaries without failing.",
    )
    parser.add_argument(
        "--allow-unconfigured",
        dest="allow_unconfigured",
        action="store_true",
        default=_bool_value(
            os.getenv(
                "ORXAQ_PROVIDER_COST_ALLOW_UNCONFIGURED",
                os.getenv("ORXAQ_AUTONOMY_PROVIDER_COST_ALLOW_UNCONFIGURED", "1"),
            ),
            True,
        ),
        help="Treat unconfigured provider telemetry as warning-only when no required providers are set.",
    )
    parser.add_argument(
        "--no-allow-unconfigured",
        dest="allow_unconfigured",
        action="store_false",
        help="Fail health checks when provider telemetry is unconfigured.",
    )
    parser.add_argument(
        "--daily-budget-usd",
        type=float,
        default=max(
            0.0,
            _float_value(
                os.getenv(
                    "ORXAQ_AUTONOMY_SWARM_DAILY_BUDGET_USD",
                    os.getenv("ORXAQ_SWARM_DAILY_BUDGET_USD", "100"),
                ),
                100.0,
            ),
        ),
        help="Daily swarm spend cap in USD.",
    )
    parser.add_argument(
        "--budget-warning-ratio",
        type=float,
        default=_float_value(
            os.getenv(
                "ORXAQ_AUTONOMY_SWARM_BUDGET_WARNING_RATIO",
                os.getenv("ORXAQ_SWARM_BUDGET_WARNING_RATIO", "0.8"),
            ),
            0.8,
        ),
        help="Budget warning threshold ratio (0-1).",
    )
    parser.add_argument(
        "--budget-enforce-hard-stop",
        dest="budget_enforce_hard_stop",
        action="store_true",
        default=_bool_value(
            os.getenv(
                "ORXAQ_AUTONOMY_SWARM_BUDGET_ENFORCE_HARD_STOP",
                os.getenv("ORXAQ_SWARM_BUDGET_ENFORCE_HARD_STOP", "1"),
            ),
            True,
        ),
        help="Mark budget cap as hard-stop policy in output.",
    )
    parser.add_argument(
        "--no-budget-enforce-hard-stop",
        dest="budget_enforce_hard_stop",
        action="store_false",
        help="Disable hard-stop policy marker for budget cap.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to write JSON health payload.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON payload only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary_file = Path(args.summary_file).expanduser().resolve()
    required_providers = _parse_required_providers(args.require_providers)
    payload, load_error = _load_summary(summary_file)
    if load_error:
        result = {
            "ok": False,
            "summary_file": str(summary_file),
            "failures": [load_error],
            "required_providers": required_providers,
        }
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print("Provider cost health check failed:")
            for failure in result["failures"]:
                print(f"- {failure}")
        return 1

    result = evaluate_health(
        payload,
        max_age_sec=max(1, int(args.max_age_sec)),
        required_providers=required_providers,
        allow_stale=bool(args.allow_stale),
        allow_unconfigured=bool(args.allow_unconfigured),
        daily_budget_usd=max(0.0, float(args.daily_budget_usd)),
        budget_warning_ratio=float(args.budget_warning_ratio),
        budget_enforce_hard_stop=bool(args.budget_enforce_hard_stop),
    )
    result["summary_file"] = str(summary_file)
    output_path = Path(str(args.output or "")).expanduser().resolve() if str(args.output or "").strip() else None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ok"]:
        providers_ok = ", ".join(result["providers_ok"]) if result["providers_ok"] else "none"
        budget = result.get("budget", {}) if isinstance(result.get("budget", {}), dict) else {}
        print(
            "Provider cost health OK: "
            f"age_sec={result['age_sec']} stale={result['stale']} providers_ok={providers_ok} "
            f"budget_state={budget.get('state', 'disabled')} "
            f"today=${_float_value(budget.get('daily_spend_usd', 0.0), 0.0):.4f}/"
            f"${_float_value(budget.get('daily_budget_usd', 0.0), 0.0):.4f}"
        )
    else:
        print("Provider cost health check failed:")
        for failure in result["failures"]:
            print(f"- {failure}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
