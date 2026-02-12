"""Provider registry loading and connectivity checks for autonomy routing."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerr
from urllib import parse, request

REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str
    lane: str
    base_url: str
    required: bool
    timeout_sec: int
    models_path: str
    api_key_env: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderResult:
    name: str
    kind: str
    lane: str
    required: bool
    status: str
    latency_ms: float | None
    checked_url: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.status == "up"
        return payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"invalid JSON and PyYAML unavailable for {path}") from err
        return yaml.safe_load(text)


def _providers_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("providers", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    return [row for row in rows if isinstance(row, dict)]


def load_providers(config_path: str) -> list[Provider]:
    rows = _providers_from_payload(_read_structured(Path(config_path)))
    out: list[Provider] = []
    for row in rows:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        out.append(
            Provider(
                name=name,
                kind=str(row.get("kind", "openai_compat")).strip().lower(),
                lane=str(row.get("lane", "L1")).strip(),
                base_url=str(row.get("base_url", "")).strip().rstrip("/"),
                required=bool(row.get("required", False)),
                timeout_sec=int(row.get("timeout_sec", 5) or 5),
                models_path=str(row.get("models_path", "/v1/models") or "/v1/models"),
                api_key_env=str(row.get("api_key_env", "") or "").strip(),
            )
        )
    return out


def _apply_profile_overrides(providers: list[Provider], profile_path: str | None) -> list[Provider]:
    if not profile_path:
        return providers
    payload = _read_structured(Path(profile_path))
    overrides = payload.get("overrides", {}) if isinstance(payload, dict) else {}
    required_set = set(overrides.get("required", []) if isinstance(overrides, dict) else [])
    optional_set = set(overrides.get("optional", []) if isinstance(overrides, dict) else [])
    out: list[Provider] = []
    for p in providers:
        req = p.required
        if p.name in required_set:
            req = True
        if p.name in optional_set:
            req = False
        out.append(
            Provider(
                name=p.name,
                kind=p.kind,
                lane=p.lane,
                base_url=p.base_url,
                required=req,
                timeout_sec=p.timeout_sec,
                models_path=p.models_path,
                api_key_env=p.api_key_env,
            )
        )
    return out


def _redact(text: str, secret: str) -> str:
    if secret and len(secret) > 3:
        return text.replace(secret, REDACTED)
    return text


def _endpoint_url(provider: Provider, api_key: str) -> str:
    if provider.kind == "gemini":
        # base URL for gemini often already includes /models.
        if api_key:
            separator = "&" if "?" in provider.base_url else "?"
            return f"{provider.base_url}{separator}key={api_key}"
        return provider.base_url
    path = provider.models_path if provider.models_path.startswith("/") else f"/{provider.models_path}"
    return f"{provider.base_url}{path}"


def _headers(provider: Provider, api_key: str) -> dict[str, str]:
    headers = {"User-Agent": "orxaq-autonomy/providers-check"}
    if provider.kind in {"openai", "openai_compat"} and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif provider.kind == "anthropic" and api_key:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    return headers


def _validate_payload(provider: Provider, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("response must be JSON object")
    if provider.kind in {"openai", "openai_compat", "anthropic"}:
        if not isinstance(payload.get("data", []), list):
            raise ValueError("expected data[]")
    elif provider.kind == "gemini":
        if not isinstance(payload.get("models", []), list):
            raise ValueError("expected models[]")


def _check_provider(provider: Provider) -> ProviderResult:
    api_key = os.environ.get(provider.api_key_env, "").strip() if provider.api_key_env else ""
    if provider.required and provider.api_key_env and not api_key:
        return ProviderResult(
            name=provider.name,
            kind=provider.kind,
            lane=provider.lane,
            required=provider.required,
            status="down",
            latency_ms=None,
            checked_url=provider.base_url,
            error=f"missing API key env var: {provider.api_key_env}",
        )

    if provider.kind not in {"openai", "openai_compat", "anthropic", "gemini"}:
        return ProviderResult(
            name=provider.name,
            kind=provider.kind,
            lane=provider.lane,
            required=provider.required,
            status="skipped",
            latency_ms=None,
            checked_url=provider.base_url,
            error="unsupported kind",
        )

    url = _endpoint_url(provider, api_key)
    redacted_url = _redact(url, api_key)
    req = request.Request(url, headers=_headers(provider, api_key), method="GET")
    started = time.monotonic()
    try:
        with request.urlopen(req, timeout=max(1, provider.timeout_sec)) as resp:  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
        latency = round((time.monotonic() - started) * 1000.0, 3)
        payload = json.loads(raw)
        _validate_payload(provider, payload)
        return ProviderResult(
            name=provider.name,
            kind=provider.kind,
            lane=provider.lane,
            required=provider.required,
            status="up",
            latency_ms=latency,
            checked_url=redacted_url,
            error="",
        )
    except (urlerr.URLError, TimeoutError) as err:
        return ProviderResult(
            name=provider.name,
            kind=provider.kind,
            lane=provider.lane,
            required=provider.required,
            status="down" if provider.required else "skipped",
            latency_ms=None,
            checked_url=redacted_url,
            error=_redact(str(err), api_key),
        )
    except Exception as err:  # noqa: BLE001
        return ProviderResult(
            name=provider.name,
            kind=provider.kind,
            lane=provider.lane,
            required=provider.required,
            status="down" if provider.required else "skipped",
            latency_ms=None,
            checked_url=redacted_url,
            error=_redact(str(err), api_key),
        )


def run_providers_check(
    *,
    root: str,
    config_path: str,
    output_path: str,
    timeout_sec: int,
    profile_path: str | None = None,
) -> dict[str, Any]:
    repo_root = Path(root).expanduser().resolve()
    config_file = Path(config_path).expanduser()
    if not config_file.is_absolute():
        config_file = (repo_root / config_file).resolve()
    output_file = Path(output_path).expanduser()
    if not output_file.is_absolute():
        output_file = (repo_root / output_file).resolve()

    providers = load_providers(str(config_file))
    providers = _apply_profile_overrides(providers, profile_path)
    if timeout_sec > 0:
        providers = [
            Provider(
                name=p.name,
                kind=p.kind,
                lane=p.lane,
                base_url=p.base_url,
                required=p.required,
                timeout_sec=timeout_sec,
                models_path=p.models_path,
                api_key_env=p.api_key_env,
            )
            for p in providers
        ]

    results = [_check_provider(p) for p in providers]
    provider_up = sum(1 for r in results if r.status == "up")
    provider_down = sum(1 for r in results if r.status == "down")
    provider_skipped = sum(1 for r in results if r.status == "skipped")
    required_total = sum(1 for r in results if r.required)
    required_down = sum(1 for r in results if r.required and r.status != "up")
    required_up = max(0, required_total - required_down)

    payload: dict[str, Any] = {
        "schema_version": "providers-check.v1",
        "generated_at_utc": _now_iso(),
        "providers": [r.to_dict() for r in results],
        "summary": {
            "provider_total": len(results),
            "provider_up": provider_up,
            "provider_down": provider_down,
            "provider_skipped": provider_skipped,
            "required_total": required_total,
            "required_up": required_up,
            "required_down": required_down,
            "all_required_up": required_down == 0,
        },
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
