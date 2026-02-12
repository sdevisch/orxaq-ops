#!/usr/bin/env python3
"""Local LM Studio fleet operations: discovery, benchmarking, and optional model sync."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_CONFIG = Path("config/local_model_fleet.json")
DEFAULT_OUTPUT = Path("artifacts/autonomy/local_models/fleet_status.json")
DEFAULT_BENCH_LOG = Path("artifacts/autonomy/local_models/benchmarks.ndjson")
_CAPACITY_ERROR_TERMS = ("429", "too many requests", "rate limit", "resource exhausted", "concurrency", "timed out")


@dataclass(frozen=True)
class Endpoint:
    endpoint_id: str
    base_url: str
    role: str
    enabled: bool
    max_parallel: int


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_ndjson(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _load_endpoints(cfg: dict[str, Any]) -> list[Endpoint]:
    out: list[Endpoint] = []
    for item in cfg.get("endpoints", []):
        if not isinstance(item, dict):
            continue
        endpoint_id = str(item.get("id", "")).strip()
        base_url = str(item.get("base_url", "")).strip().rstrip("/")
        if not endpoint_id or not base_url:
            continue
        out.append(
            Endpoint(
                endpoint_id=endpoint_id,
                base_url=base_url,
                role=str(item.get("role", "default")).strip() or "default",
                enabled=bool(item.get("enabled", True)),
                max_parallel=max(1, int(item.get("max_parallel", 1) or 1)),
            )
        )
    return out


def _http_json(method: str, url: str, payload: dict[str, Any] | None, timeout_sec: int) -> tuple[bool, Any, str, float]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer lm-studio"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=max(1, timeout_sec)) as response:
            text = response.read().decode("utf-8", errors="replace")
        latency_ms = (time.monotonic() - started) * 1000.0
        try:
            parsed = json.loads(text)
        except Exception:
            return False, {}, "response_not_json", latency_ms
        return True, parsed, "", latency_ms
    except urllib.error.HTTPError as err:
        latency_ms = (time.monotonic() - started) * 1000.0
        body_text = ""
        try:
            body_text = err.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        return False, {}, f"http_{err.code}:{body_text[:300]}", latency_ms
    except Exception as err:  # noqa: BLE001
        latency_ms = (time.monotonic() - started) * 1000.0
        return False, {}, str(err), latency_ms


def _normalized_endpoint_key(raw_url: str) -> str:
    value = str(raw_url).strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"http://{value}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return ""
    port = parsed.port
    if port is None:
        port = 443 if (parsed.scheme or "").strip().lower() == "https" else 80
    return f"{host}:{port}"


def _score_model_complexity(model_name: str) -> tuple[str, int]:
    name = model_name.lower()
    score = 35
    if "70b" in name:
        score += 35
    elif "32b" in name:
        score += 25
    elif "14b" in name or "13b" in name:
        score += 15
    elif "7b" in name:
        score += 8
    if "coder" in name:
        score += 8
    if "r1" in name or "reason" in name:
        score += 12
    if "embed" in name:
        score = 5

    if score >= 85:
        return "deep_research", score
    if score >= 65:
        return "complex", score
    if score >= 45:
        return "standard", score
    return "simple", score


def probe_models(endpoints: list[Endpoint], timeout_sec: int) -> dict[str, Any]:
    endpoint_rows: list[dict[str, Any]] = []
    complexity_counts = {"simple": 0, "standard": 0, "complex": 0, "deep_research": 0}
    unique_models: set[str] = set()

    for endpoint in endpoints:
        if not endpoint.enabled:
            endpoint_rows.append(
                {
                    "id": endpoint.endpoint_id,
                    "base_url": endpoint.base_url,
                    "role": endpoint.role,
                    "enabled": False,
                    "ok": False,
                    "error": "disabled",
                    "latency_ms": 0.0,
                    "models": [],
                }
            )
            continue

        ok, payload, error, latency_ms = _http_json("GET", f"{endpoint.base_url}/models", None, timeout_sec)
        model_rows: list[dict[str, Any]] = []
        if ok and isinstance(payload, dict):
            data = payload.get("data", [])
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    model_id = str(item.get("id", "")).strip()
                    if not model_id:
                        continue
                    complexity, score = _score_model_complexity(model_id)
                    model_rows.append(
                        {
                            "id": model_id,
                            "complexity": complexity,
                            "complexity_score": score,
                        }
                    )
                    unique_models.add(model_id)
                    complexity_counts[complexity] += 1

        endpoint_rows.append(
            {
                "id": endpoint.endpoint_id,
                "base_url": endpoint.base_url,
                "role": endpoint.role,
                "enabled": endpoint.enabled,
                "ok": ok,
                "error": error,
                "latency_ms": round(latency_ms, 3),
                "max_parallel": endpoint.max_parallel,
                "models": sorted(model_rows, key=lambda item: (item["complexity_score"], item["id"]), reverse=True),
            }
        )

    return {
        "timestamp": _now_iso(),
        "summary": {
            "endpoint_total": len(endpoint_rows),
            "endpoint_healthy": sum(1 for row in endpoint_rows if bool(row.get("ok", False))),
            "endpoint_unhealthy": sum(1 for row in endpoint_rows if not bool(row.get("ok", False))),
            "model_unique_total": len(unique_models),
            "complexity_counts": complexity_counts,
        },
        "endpoints": endpoint_rows,
    }


def _select_benchmark_models(endpoint_row: dict[str, Any], models_per_endpoint: int) -> list[str]:
    models = endpoint_row.get("models", [])
    if not isinstance(models, list):
        return []
    selected: list[str] = []
    for row in models:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id", "")).strip()
        if model_id and model_id not in selected:
            selected.append(model_id)
        if len(selected) >= models_per_endpoint:
            break
    return selected


def _pick_probe_model(
    endpoint_row: dict[str, Any],
    *,
    preferred_models: list[str],
    complexity_preference: list[str],
) -> str:
    models = endpoint_row.get("models", [])
    if not isinstance(models, list):
        return ""
    available: list[str] = []
    by_complexity: dict[str, list[str]] = {"simple": [], "standard": [], "complex": [], "deep_research": []}
    for row in models:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id", "")).strip()
        if not model_id:
            continue
        available.append(model_id)
        complexity = str(row.get("complexity", "")).strip().lower()
        if complexity in by_complexity:
            by_complexity[complexity].append(model_id)
    for preferred in preferred_models:
        if preferred in available:
            return preferred
    for complexity in complexity_preference:
        options = by_complexity.get(str(complexity).strip().lower(), [])
        if options:
            return options[0]
    return available[0] if available else ""


def _single_completion(
    *,
    base_url: str,
    model: str,
    prompt: str,
    timeout_sec: int,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    ok, response, error, latency_ms = _http_json(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        payload,
        timeout_sec,
    )
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    return {
        "ok": bool(ok),
        "error": str(error).strip(),
        "latency_ms": round(float(latency_ms), 3),
        "usage": usage if isinstance(usage, dict) else {},
    }


def _build_context_probe_prompt(target_tokens: int) -> str:
    words = max(64, int(target_tokens))
    return (
        "Analyze this deterministic payload and reply with exactly OK. "
        + ("ctx " * words)
    ).strip()


def capability_scan(
    probe_payload: dict[str, Any],
    *,
    capability_cfg: dict[str, Any],
) -> dict[str, Any]:
    enabled = bool(capability_cfg.get("enabled", True))
    if not enabled:
        return {"timestamp": _now_iso(), "enabled": False, "summary": {"endpoint_total": 0}}

    timeout_sec = max(3, int(capability_cfg.get("timeout_sec", 25) or 25))
    prompt = str(capability_cfg.get("probe_prompt", "Reply with exactly OK")).strip() or "Reply with exactly OK"
    max_tokens = max(8, int(capability_cfg.get("probe_max_tokens", 48) or 48))
    temperature = float(capability_cfg.get("probe_temperature", 0) or 0)
    success_threshold = float(capability_cfg.get("success_rate_threshold", 0.8) or 0.8)
    latency_guard_ratio = float(capability_cfg.get("latency_guard_ratio", 0.95) or 0.95)
    max_parallel_cap = max(1, int(capability_cfg.get("max_parallel_probe", 8) or 8))
    preferred_models = [str(item).strip() for item in capability_cfg.get("preferred_models", []) if str(item).strip()]
    complexity_preference = [
        str(item).strip().lower()
        for item in capability_cfg.get("complexity_preference", ["standard", "simple", "complex", "deep_research"])
        if str(item).strip()
    ]
    context_token_steps = [max(256, int(item)) for item in capability_cfg.get("context_token_steps", [1024, 4096, 8192, 16384]) if str(item).strip()]
    context_max_tokens = max(4, int(capability_cfg.get("context_probe_max_tokens", 8) or 8))
    context_model_preference = [str(item).strip() for item in capability_cfg.get("context_model_preference", []) if str(item).strip()]
    endpoint_rows: list[dict[str, Any]] = []

    for endpoint_row in probe_payload.get("endpoints", []):
        if not isinstance(endpoint_row, dict):
            continue
        if not bool(endpoint_row.get("ok", False)):
            continue
        endpoint_id = str(endpoint_row.get("id", "")).strip() or "unknown"
        base_url = str(endpoint_row.get("base_url", "")).strip().rstrip("/")
        if not base_url:
            continue
        configured_parallel = max(1, int(endpoint_row.get("max_parallel", 1) or 1))
        peak_parallel = max(1, min(max_parallel_cap, configured_parallel * 3))
        probe_model = _pick_probe_model(
            endpoint_row,
            preferred_models=preferred_models,
            complexity_preference=complexity_preference,
        )
        context_model = _pick_probe_model(
            endpoint_row,
            preferred_models=context_model_preference or preferred_models,
            complexity_preference=["complex", "deep_research", "standard", "simple"],
        ) or probe_model
        if not probe_model:
            continue
        parallel_trials: list[dict[str, Any]] = []
        recommended_parallel = 1
        observed_max_success = 0
        capacity_events = 0
        for parallelism in range(1, peak_parallel + 1):
            futures: list[concurrent.futures.Future[dict[str, Any]]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as executor:
                for slot in range(parallelism):
                    slot_prompt = f"{prompt}\nslot={slot + 1}/{parallelism}; endpoint={endpoint_id}; model={probe_model}"
                    futures.append(
                        executor.submit(
                            _single_completion,
                            base_url=base_url,
                            model=probe_model,
                            prompt=slot_prompt,
                            timeout_sec=timeout_sec,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        )
                    )
            responses = [future.result() for future in futures]
            ok_count = sum(1 for row in responses if bool(row.get("ok", False)))
            success_rate = ok_count / max(1, len(responses))
            latencies = [float(row.get("latency_ms", 0.0) or 0.0) for row in responses if bool(row.get("ok", False))]
            avg_latency_ms = sum(latencies) / max(1, len(latencies))
            errors_joined = " ".join(str(row.get("error", "")).lower() for row in responses if not bool(row.get("ok", False)))
            had_capacity_error = any(term in errors_joined for term in _CAPACITY_ERROR_TERMS)
            if had_capacity_error:
                capacity_events += 1
            healthy = (
                success_rate >= success_threshold
                and avg_latency_ms <= (timeout_sec * 1000.0 * latency_guard_ratio)
                and not had_capacity_error
            )
            if healthy:
                recommended_parallel = parallelism
            if success_rate >= success_threshold:
                observed_max_success = parallelism
            parallel_trials.append(
                {
                    "parallelism": parallelism,
                    "request_count": len(responses),
                    "ok_count": ok_count,
                    "success_rate": round(success_rate, 4),
                    "avg_latency_ms": round(avg_latency_ms, 3),
                    "healthy": healthy,
                    "capacity_error": had_capacity_error,
                }
            )
            if not healthy and parallelism > max(2, recommended_parallel + 1):
                break

        context_trials: list[dict[str, Any]] = []
        max_context_tokens_success = 0
        if context_model:
            for token_target in sorted(set(context_token_steps)):
                response = _single_completion(
                    base_url=base_url,
                    model=context_model,
                    prompt=_build_context_probe_prompt(token_target),
                    timeout_sec=timeout_sec,
                    max_tokens=context_max_tokens,
                    temperature=0,
                )
                passed = bool(response.get("ok", False))
                if passed:
                    max_context_tokens_success = token_target
                context_trials.append(
                    {
                        "token_target": token_target,
                        "ok": passed,
                        "latency_ms": response.get("latency_ms", 0.0),
                        "error": response.get("error", ""),
                    }
                )
                if not passed:
                    break

        endpoint_rows.append(
            {
                "id": endpoint_id,
                "base_url": base_url,
                "endpoint_key": _normalized_endpoint_key(base_url),
                "probe_model": probe_model,
                "context_model": context_model,
                "configured_max_parallel": configured_parallel,
                "recommended_parallel": max(1, min(configured_parallel, recommended_parallel)),
                "observed_max_parallel_success": observed_max_success,
                "capacity_events": capacity_events,
                "max_context_tokens_success": max_context_tokens_success,
                "parallel_trials": parallel_trials,
                "context_trials": context_trials,
            }
        )

    endpoint_total = len(endpoint_rows)
    return {
        "timestamp": _now_iso(),
        "enabled": True,
        "summary": {
            "endpoint_total": endpoint_total,
            "endpoint_scanned": endpoint_total,
            "recommended_parallel_total": sum(max(1, int(row.get("recommended_parallel", 1) or 1)) for row in endpoint_rows),
            "by_endpoint": {
                str(row.get("id", "")).strip(): {
                    "endpoint_key": str(row.get("endpoint_key", "")).strip(),
                    "base_url": str(row.get("base_url", "")).strip(),
                    "recommended_parallel": max(1, int(row.get("recommended_parallel", 1) or 1)),
                    "max_context_tokens_success": max(0, int(row.get("max_context_tokens_success", 0) or 0)),
                    "probe_model": str(row.get("probe_model", "")).strip(),
                }
                for row in endpoint_rows
            },
        },
        "endpoints": endpoint_rows,
    }


def benchmark_models(
    probe_payload: dict[str, Any],
    *,
    timeout_sec: int,
    max_tokens: int,
    temperature: float,
    models_per_endpoint: int,
    prompt: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for endpoint_row in probe_payload.get("endpoints", []):
        if not isinstance(endpoint_row, dict):
            continue
        if not bool(endpoint_row.get("ok", False)):
            continue
        base_url = str(endpoint_row.get("base_url", "")).strip().rstrip("/")
        if not base_url:
            continue
        endpoint_id = str(endpoint_row.get("id", "")).strip() or "unknown"
        models = _select_benchmark_models(endpoint_row, models_per_endpoint)
        for model in models:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            ok, response, error, latency_ms = _http_json(
                "POST",
                f"{base_url}/chat/completions",
                payload,
                timeout_sec,
            )
            choices = response.get("choices", []) if isinstance(response, dict) else []
            text = ""
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message", {})
                if isinstance(message, dict):
                    text = str(message.get("content", "")).strip()
            usage = response.get("usage", {}) if isinstance(response, dict) else {}
            rows.append(
                {
                    "timestamp": _now_iso(),
                    "endpoint_id": endpoint_id,
                    "base_url": base_url,
                    "model": model,
                    "ok": ok,
                    "error": error,
                    "latency_ms": round(latency_ms, 3),
                    "response_chars": len(text),
                    "usage": usage if isinstance(usage, dict) else {},
                }
            )

    by_endpoint: dict[str, dict[str, Any]] = {}
    for row in rows:
        endpoint_id = str(row.get("endpoint_id", "unknown"))
        stats = by_endpoint.setdefault(
            endpoint_id,
            {
                "requests": 0,
                "ok": 0,
                "errors": 0,
                "latency_ms_sum": 0.0,
                "latency_ms_avg": 0.0,
                "best_model": "",
                "best_latency_ms": 0.0,
            },
        )
        stats["requests"] += 1
        if bool(row.get("ok", False)):
            stats["ok"] += 1
            latency = float(row.get("latency_ms", 0.0) or 0.0)
            stats["latency_ms_sum"] += latency
            if not stats["best_model"] or latency < float(stats["best_latency_ms"]):
                stats["best_model"] = str(row.get("model", ""))
                stats["best_latency_ms"] = round(latency, 3)
        else:
            stats["errors"] += 1

    for stats in by_endpoint.values():
        ok = int(stats["ok"])
        stats["latency_ms_avg"] = round(stats["latency_ms_sum"] / max(1, ok), 3)
        stats.pop("latency_ms_sum", None)

    return {
        "timestamp": _now_iso(),
        "rows": rows,
        "summary": {
            "requests": len(rows),
            "ok": sum(1 for row in rows if bool(row.get("ok", False))),
            "errors": sum(1 for row in rows if not bool(row.get("ok", False))),
            "by_endpoint": by_endpoint,
        },
    }


def sync_missing_models(
    probe_payload: dict[str, Any],
    target_models: list[str],
    command_template: str,
) -> dict[str, Any]:
    normalized_targets = [str(item).strip() for item in target_models if str(item).strip()]
    if not normalized_targets:
        return {
            "timestamp": _now_iso(),
            "ok": True,
            "executed": [],
            "missing": [],
            "message": "No target models configured",
        }

    endpoint_models: dict[str, set[str]] = {}
    endpoint_urls: dict[str, str] = {}
    for endpoint_row in probe_payload.get("endpoints", []):
        if not isinstance(endpoint_row, dict):
            continue
        endpoint_id = str(endpoint_row.get("id", "")).strip() or "unknown"
        endpoint_urls[endpoint_id] = str(endpoint_row.get("base_url", "")).strip()
        models = endpoint_models.setdefault(endpoint_id, set())
        for row in endpoint_row.get("models", []):
            if isinstance(row, dict):
                model_id = str(row.get("id", "")).strip()
                if model_id:
                    models.add(model_id)

    missing: list[dict[str, str]] = []
    for endpoint_id, models in endpoint_models.items():
        for model in normalized_targets:
            if model not in models:
                missing.append({"endpoint_id": endpoint_id, "base_url": endpoint_urls.get(endpoint_id, ""), "model": model})

    executed: list[dict[str, Any]] = []
    template = command_template.strip()
    if template:
        for row in missing:
            cmd = (
                template.replace("{endpoint_id}", row["endpoint_id"])
                .replace("{base_url}", row["base_url"])
                .replace("{model}", row["model"])
            )
            proc = subprocess.run(shlex.split(cmd), check=False, capture_output=True, text=True)
            executed.append(
                {
                    "endpoint_id": row["endpoint_id"],
                    "model": row["model"],
                    "cmd": cmd,
                    "returncode": int(proc.returncode),
                    "stdout": (proc.stdout or "").strip()[:500],
                    "stderr": (proc.stderr or "").strip()[:500],
                }
            )

    prewarmed: list[dict[str, Any]] = []
    for endpoint_id, models in endpoint_models.items():
        base_url = str(endpoint_urls.get(endpoint_id, "")).strip().rstrip("/")
        if not base_url:
            continue
        for model in normalized_targets:
            if model not in models:
                continue
            response = _single_completion(
                base_url=base_url,
                model=model,
                prompt="Reply with exactly OK.",
                timeout_sec=8,
                max_tokens=8,
                temperature=0,
            )
            prewarmed.append(
                {
                    "endpoint_id": endpoint_id,
                    "base_url": base_url,
                    "model": model,
                    "ok": bool(response.get("ok", False)),
                    "latency_ms": float(response.get("latency_ms", 0.0) or 0.0),
                    "error": str(response.get("error", "")).strip()[:300],
                }
            )

    return {
        "timestamp": _now_iso(),
        "ok": True,
        "executed": executed,
        "missing": missing,
        "prewarmed": prewarmed,
        "message": "executed_and_prewarmed" if template else "prewarmed_only_no_command_template",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate local LM Studio model fleet")
    parser.add_argument("command", choices=["probe", "benchmark", "sync", "capability-scan", "full-cycle"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--bench-log", default=str(DEFAULT_BENCH_LOG))
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    config_path = Path(args.config).resolve()
    output_path = Path(args.output).resolve()
    bench_log_path = Path(args.bench_log).resolve()

    cfg = _read_json(config_path)
    endpoints = _load_endpoints(cfg)
    bench_cfg = cfg.get("benchmark", {}) if isinstance(cfg.get("benchmark", {}), dict) else {}
    timeout_sec = max(1, int(bench_cfg.get("timeout_sec", 15) or 15))
    max_tokens = max(64, int(bench_cfg.get("max_tokens", 220) or 220))
    temperature = float(bench_cfg.get("temperature", 0) or 0)
    models_per_endpoint = max(1, int(bench_cfg.get("models_per_endpoint", 3) or 3))
    prompt = str(bench_cfg.get("prompt", "Return compact JSON: {\"ok\": true}"))

    command_template = str(os.getenv("ORXAQ_LOCAL_MODEL_SYNC_COMMAND", "")).strip() or str(cfg.get("download_command_template", "")).strip()

    probe_payload: dict[str, Any] = {}
    benchmark_payload: dict[str, Any] = {}
    sync_payload: dict[str, Any] = {}
    capability_payload: dict[str, Any] = {}

    if args.command in {"probe", "benchmark", "sync", "capability-scan", "full-cycle"}:
        probe_payload = probe_models(endpoints, timeout_sec)

    if args.command in {"benchmark", "full-cycle"}:
        benchmark_payload = benchmark_models(
            probe_payload,
            timeout_sec=timeout_sec,
            max_tokens=max_tokens,
            temperature=temperature,
            models_per_endpoint=models_per_endpoint,
            prompt=prompt,
        )
        for row in benchmark_payload.get("rows", []):
            if isinstance(row, dict):
                _append_ndjson(bench_log_path, row)

    if args.command in {"sync", "full-cycle"}:
        target_models = cfg.get("target_models", []) if isinstance(cfg.get("target_models", []), list) else []
        sync_payload = sync_missing_models(probe_payload, [str(item) for item in target_models], command_template)

    if args.command in {"capability-scan", "full-cycle"}:
        capability_cfg = cfg.get("capability_scan", {}) if isinstance(cfg.get("capability_scan", {}), dict) else {}
        capability_payload = capability_scan(
            probe_payload,
            capability_cfg=capability_cfg,
        )

    output_payload = {
        "timestamp": _now_iso(),
        "config_file": str(config_path),
        "output_file": str(output_path),
        "bench_log_file": str(bench_log_path),
        "probe": probe_payload,
        "benchmark": benchmark_payload,
        "sync": sync_payload,
        "capability_scan": capability_payload,
    }
    _write_json(output_path, output_payload)
    print(json.dumps(output_payload.get("probe", {}).get("summary", {}), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
