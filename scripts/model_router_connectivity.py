#!/usr/bin/env python3
"""Validate connectivity for configured model-router endpoints."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_CONFIG = Path("config/litellm_swarm_router.json")
DEFAULT_OUTPUT = Path("artifacts/model_connectivity.json")


@dataclass(frozen=True)
class EndpointConfig:
    endpoint_id: str
    provider: str
    api_base: str
    healthcheck_path: str
    auth_mode: str
    api_key_env: str
    required: bool
    model_names: list[str] = field(default_factory=list)
    healthcheck_headers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_text(value: Any) -> str:
    return str(value).strip()


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


def _normalize_base_url(raw: str) -> str:
    return raw.strip().rstrip("/")


def _normalize_path(raw: str) -> str:
    value = raw.strip()
    if not value:
        return "/v1/models"
    if not value.startswith("/"):
        return f"/{value}"
    return value


def _normalize_provider(raw: str) -> str:
    value = raw.strip().lower()
    return value or "openai_compatible"


def _default_health_path(provider: str) -> str:
    if provider == "gemini":
        return "/v1beta/models"
    return "/v1/models"


def load_router_config(config_path: Path) -> list[EndpointConfig]:
    payload = _load_json(config_path)
    model_list = payload.get("model_list", [])
    if not isinstance(model_list, list):
        return []

    merged: dict[tuple[str, str, str, str, str, str, bool, tuple[tuple[str, str], ...]], EndpointConfig] = {}
    for row in model_list:
        if not isinstance(row, dict):
            continue
        model_name = _as_text(row.get("model_name", ""))
        litellm_params = row.get("litellm_params", {})
        if not isinstance(litellm_params, dict):
            continue

        api_base = _normalize_base_url(_as_text(litellm_params.get("api_base", "")))
        if not api_base:
            continue
        provider = _normalize_provider(_as_text(litellm_params.get("provider", "")))
        endpoint_id = _as_text(litellm_params.get("endpoint_id", ""))
        if not endpoint_id:
            endpoint_id = provider or "endpoint"

        health_path = _normalize_path(
            _as_text(litellm_params.get("healthcheck_path", _default_health_path(provider)))
        )
        auth_mode = _as_text(litellm_params.get("healthcheck_auth", ""))
        if not auth_mode:
            auth_mode = "bearer" if _as_text(litellm_params.get("api_key_env", "")) else "none"
        api_key_env = _as_text(litellm_params.get("api_key_env", ""))
        required = _as_bool(litellm_params.get("required", True), True)

        header_payload = litellm_params.get("healthcheck_headers", {})
        health_headers: dict[str, str] = {}
        if isinstance(header_payload, dict):
            for key, value in header_payload.items():
                normalized_key = _as_text(key)
                normalized_value = _as_text(value)
                if normalized_key and normalized_value:
                    health_headers[normalized_key] = normalized_value
        header_items = tuple(sorted(health_headers.items()))

        dedupe_key = (endpoint_id, provider, api_base, health_path, auth_mode, api_key_env, required, header_items)
        existing = merged.get(dedupe_key)
        if existing is None:
            merged[dedupe_key] = EndpointConfig(
                endpoint_id=endpoint_id,
                provider=provider,
                api_base=api_base,
                healthcheck_path=health_path,
                auth_mode=auth_mode,
                api_key_env=api_key_env,
                required=required,
                model_names=[model_name] if model_name else [],
                healthcheck_headers=health_headers,
            )
            continue

        model_names = list(existing.model_names)
        if model_name and model_name not in model_names:
            model_names.append(model_name)
        merged[dedupe_key] = EndpointConfig(
            endpoint_id=existing.endpoint_id,
            provider=existing.provider,
            api_base=existing.api_base,
            healthcheck_path=existing.healthcheck_path,
            auth_mode=existing.auth_mode,
            api_key_env=existing.api_key_env,
            required=existing.required,
            model_names=model_names,
            healthcheck_headers=dict(existing.healthcheck_headers),
        )

    return sorted(merged.values(), key=lambda item: item.endpoint_id)


def _build_healthcheck_url(endpoint: EndpointConfig, api_key: str) -> str:
    url = f"{endpoint.api_base}{endpoint.healthcheck_path}"
    if endpoint.auth_mode == "query-key" and api_key:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode({'key': api_key})}"
    return url


def _build_headers(endpoint: EndpointConfig, api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json", **endpoint.healthcheck_headers}
    if endpoint.auth_mode == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if endpoint.auth_mode == "x-api-key" and api_key:
        headers["x-api-key"] = api_key
    return headers


def _extract_model_count(payload: Any) -> int:
    if isinstance(payload, dict):
        data = payload.get("data", [])
        if isinstance(data, list):
            return len(data)
        models = payload.get("models", [])
        if isinstance(models, list):
            return len(models)
    return 0


def probe_endpoint(endpoint: EndpointConfig, *, timeout_sec: int = 6) -> dict[str, Any]:
    api_key = _as_text(os.getenv(endpoint.api_key_env, "")) if endpoint.api_key_env else ""
    headers = _build_headers(endpoint, api_key)
    url = _build_healthcheck_url(endpoint, api_key)
    request = Request(url, method="GET", headers=headers)
    started = time.monotonic()

    try:
        with urlopen(request, timeout=max(1, int(timeout_sec))) as response:
            status_code = int(response.status)
            body_text = response.read().decode("utf-8", errors="replace")
        latency_ms = round((time.monotonic() - started) * 1000.0, 3)
        payload = json.loads(body_text) if body_text else {}
        model_count = _extract_model_count(payload)
        return {
            "id": endpoint.endpoint_id,
            "provider": endpoint.provider,
            "api_base": endpoint.api_base,
            "required": bool(endpoint.required),
            "healthcheck_url": url,
            "healthcheck_path": endpoint.healthcheck_path,
            "model_names": endpoint.model_names,
            "ok": 200 <= status_code < 300,
            "status_code": status_code,
            "model_count": model_count,
            "latency_ms": latency_ms,
            "error": "",
            "auth_mode": endpoint.auth_mode,
            "auth_configured": bool(api_key) or endpoint.auth_mode == "none",
        }
    except HTTPError as err:
        latency_ms = round((time.monotonic() - started) * 1000.0, 3)
        body_text = ""
        try:
            body_text = err.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        return {
            "id": endpoint.endpoint_id,
            "provider": endpoint.provider,
            "api_base": endpoint.api_base,
            "required": bool(endpoint.required),
            "healthcheck_url": url,
            "healthcheck_path": endpoint.healthcheck_path,
            "model_names": endpoint.model_names,
            "ok": False,
            "status_code": int(err.code),
            "model_count": 0,
            "latency_ms": latency_ms,
            "error": f"http_{err.code}:{body_text[:300]}",
            "auth_mode": endpoint.auth_mode,
            "auth_configured": bool(api_key) or endpoint.auth_mode == "none",
        }
    except Exception as err:  # noqa: BLE001
        latency_ms = round((time.monotonic() - started) * 1000.0, 3)
        return {
            "id": endpoint.endpoint_id,
            "provider": endpoint.provider,
            "api_base": endpoint.api_base,
            "required": bool(endpoint.required),
            "healthcheck_url": url,
            "healthcheck_path": endpoint.healthcheck_path,
            "model_names": endpoint.model_names,
            "ok": False,
            "status_code": 0,
            "model_count": 0,
            "latency_ms": latency_ms,
            "error": str(err)[:300],
            "auth_mode": endpoint.auth_mode,
            "auth_configured": bool(api_key) or endpoint.auth_mode == "none",
        }


def run_connectivity_report(
    *,
    config_path: Path,
    output_path: Path,
    timeout_sec: int = 6,
) -> dict[str, Any]:
    endpoints = load_router_config(config_path)
    rows = [probe_endpoint(endpoint, timeout_sec=timeout_sec) for endpoint in endpoints]
    endpoint_total = len(rows)
    required_rows = [row for row in rows if bool(row.get("required", True))]
    optional_rows = [row for row in rows if not bool(row.get("required", True))]
    endpoint_required_total = len(required_rows)
    endpoint_optional_total = len(optional_rows)
    endpoint_healthy = sum(1 for row in required_rows if bool(row.get("ok", False)))
    endpoint_unhealthy = endpoint_required_total - endpoint_healthy
    optional_endpoint_healthy = sum(1 for row in optional_rows if bool(row.get("ok", False)))
    optional_endpoint_unhealthy = endpoint_optional_total - optional_endpoint_healthy
    payload = {
        "schema_version": "model-router-connectivity.v1",
        "generated_at_utc": _utc_now_iso(),
        "config_path": str(config_path.resolve()),
        "router": "litellm",
        "endpoint_total": endpoint_total,
        "endpoint_required_total": endpoint_required_total,
        "endpoint_optional_total": endpoint_optional_total,
        "endpoint_healthy": endpoint_healthy,
        "endpoint_unhealthy": endpoint_unhealthy,
        "optional_endpoint_healthy": optional_endpoint_healthy,
        "optional_endpoint_unhealthy": optional_endpoint_unhealthy,
        "all_healthy": endpoint_unhealthy == 0,
        "all_endpoints_healthy": endpoint_unhealthy == 0 and optional_endpoint_unhealthy == 0,
        "summary": {
            "endpoint_total": endpoint_total,
            "endpoint_required_total": endpoint_required_total,
            "endpoint_optional_total": endpoint_optional_total,
            "endpoint_healthy": endpoint_healthy,
            "endpoint_unhealthy": endpoint_unhealthy,
            "optional_endpoint_healthy": optional_endpoint_healthy,
            "optional_endpoint_unhealthy": optional_endpoint_unhealthy,
            "all_healthy": endpoint_unhealthy == 0,
            "all_endpoints_healthy": endpoint_unhealthy == 0 and optional_endpoint_unhealthy == 0,
        },
        "endpoints": rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate model-router endpoint connectivity.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Router config path (LiteLLM-style model_list JSON).",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="JSON output report path.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=6,
        help="HTTP timeout in seconds per endpoint.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any endpoint is unhealthy.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    report = run_connectivity_report(
        config_path=config_path,
        output_path=output_path,
        timeout_sec=max(1, int(args.timeout_sec)),
    )
    print(
        json.dumps(
            {
                "endpoint_total": report.get("endpoint_total", 0),
                "endpoint_required_total": report.get("endpoint_required_total", 0),
                "endpoint_optional_total": report.get("endpoint_optional_total", 0),
                "endpoint_healthy": report.get("endpoint_healthy", 0),
                "endpoint_unhealthy": report.get("endpoint_unhealthy", 0),
                "optional_endpoint_healthy": report.get("optional_endpoint_healthy", 0),
                "optional_endpoint_unhealthy": report.get("optional_endpoint_unhealthy", 0),
                "all_healthy": bool(report.get("all_healthy", False)),
                "output": str(output_path),
            },
            sort_keys=True,
        )
    )
    if args.strict and int(report.get("endpoint_unhealthy", 0)) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
