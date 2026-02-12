"""Provider router checks for OpenAI-compatible multi-provider gateways."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


@dataclass(frozen=True)
class RouterProvider:
    name: str
    kind: str
    base_url: str
    required: bool


@dataclass(frozen=True)
class RouterProviderStatus:
    name: str
    kind: str
    base_url: str
    checked_url: str
    required: bool
    status: str
    latency_ms: float | None
    error: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.status == "up"
        return payload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_structured_payload(raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except Exception:
        try:
            import yaml  # type: ignore
        except Exception as err:  # noqa: BLE001
            raise ValueError("router config must be JSON or YAML (requires PyYAML)") from err
        return yaml.safe_load(raw_text)


def _read_router_config(config_path: str) -> dict[str, Any]:
    raw = Path(config_path).expanduser().read_text(encoding="utf-8")
    payload = _load_structured_payload(raw)
    if not isinstance(payload, dict):
        raise ValueError("router config root must be an object")
    return payload


def _extract_providers(payload: dict[str, Any]) -> list[RouterProvider]:
    rows = payload.get("providers", [])
    if not isinstance(rows, list):
        raise ValueError("router config must define providers[]")
    providers: list[RouterProvider] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        kind = str(row.get("kind", "openai_compat")).strip().lower()
        base_url = str(row.get("base_url", "")).strip()
        required = bool(row.get("required", False))
        if not name or not base_url:
            continue
        providers.append(RouterProvider(name=name, kind=kind, base_url=base_url, required=required))
    return providers


def _provider_name_set(payload: dict[str, Any]) -> set[str]:
    return {
        str(row.get("name", "")).strip()
        for row in payload.get("providers", [])
        if isinstance(row, dict) and str(row.get("name", "")).strip()
    }


def _read_profile(profile_path: Path) -> dict[str, Any]:
    raw = profile_path.read_text(encoding="utf-8")
    payload = _load_structured_payload(raw)
    if not isinstance(payload, dict):
        raise ValueError("router profile must be an object")
    return payload


def apply_router_profile(
    *,
    root: str = ".",
    profile_name: str,
    base_config_path: str = "./config/router.example.yaml",
    # Router profiles have a different schema than provider profiles (profiles/*.yaml).
    profiles_dir: str = "./router_profiles",
    output_path: str = "./config/router.active.yaml",
) -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    if not profile_name.strip():
        raise ValueError("profile_name is required")

    base_config_file = Path(base_config_path).expanduser()
    if not base_config_file.is_absolute():
        base_config_file = (root_path / base_config_file).resolve()

    profiles_root = Path(profiles_dir).expanduser()
    if not profiles_root.is_absolute():
        profiles_root = (root_path / profiles_root).resolve()
    profile_file = (profiles_root / f"{profile_name.strip()}.yaml").resolve()
    if not profile_file.exists():
        profile_file = (profiles_root / f"{profile_name.strip()}.json").resolve()
    if not profile_file.exists():
        raise ValueError(f"profile not found: {profile_name}")

    output_file = Path(output_path).expanduser()
    if not output_file.is_absolute():
        output_file = (root_path / output_file).resolve()

    base_payload = _read_router_config(str(base_config_file))
    profile_payload = _read_profile(profile_file)
    providers = base_payload.get("providers", [])
    if not isinstance(providers, list):
        raise ValueError("router config must define providers[]")

    provider_names = _provider_name_set(base_payload)
    required_names_raw = profile_payload.get("required_providers", [])
    if not isinstance(required_names_raw, list):
        raise ValueError("profile required_providers must be a list")
    required_names = {str(item).strip() for item in required_names_raw if str(item).strip()}
    unknown_required = sorted(name for name in required_names if name not in provider_names)
    if unknown_required:
        raise ValueError(f"profile includes unknown providers: {', '.join(unknown_required)}")

    active_payload = json.loads(json.dumps(base_payload))
    for row in active_payload.get("providers", []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        row["required"] = name in required_names

    lanes = profile_payload.get("lanes", None)
    if isinstance(lanes, dict):
        active_payload["lanes"] = {
            str(lane).strip(): [str(item).strip() for item in values if str(item).strip()]
            for lane, values in lanes.items()
            if isinstance(values, list) and str(lane).strip()
        }
    router_cfg = active_payload.get("router", {})
    if not isinstance(router_cfg, dict):
        router_cfg = {}
    profile_router = profile_payload.get("router", {})
    if isinstance(profile_router, dict):
        fallback = profile_router.get("fallback_order", None)
        if isinstance(fallback, list):
            router_cfg["fallback_order"] = [str(item).strip() for item in fallback if str(item).strip()]
    active_payload["router"] = router_cfg
    active_payload["profile"] = {
        "name": profile_name.strip(),
        "source": str(profile_file),
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(active_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "profile": profile_name.strip(),
        "base_config": str(base_config_file),
        "profile_path": str(profile_file),
        "active_config": str(output_file),
        "required_providers": sorted(required_names),
    }


def _resolve_lane_providers(payload: dict[str, Any], lane: str | None) -> list[str]:
    lanes = payload.get("lanes", {})
    if not isinstance(lanes, dict):
        return []
    if lane:
        values = lanes.get(lane, [])
        return [str(item).strip() for item in values if str(item).strip()] if isinstance(values, list) else []
    router_cfg = payload.get("router", {})
    fallback = []
    if isinstance(router_cfg, dict):
        fallback_raw = router_cfg.get("fallback_order", [])
        if isinstance(fallback_raw, list):
            fallback = [str(item).strip() for item in fallback_raw if str(item).strip()]
    order = fallback or [str(name).strip() for name in lanes.keys()]
    names: list[str] = []
    for lane_name in order:
        values = lanes.get(lane_name, [])
        if not isinstance(values, list):
            continue
        for item in values:
            name = str(item).strip()
            if name and name not in names:
                names.append(name)
    return names


def _models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/models"):
        return base
    return f"{base}/models"


def _check_provider(provider: RouterProvider, timeout_sec: int) -> RouterProviderStatus:
    checked_url = _models_url(provider.base_url)
    parsed = urllib_parse.urlparse(checked_url)
    if parsed.scheme not in {"http", "https"}:
        return RouterProviderStatus(
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            checked_url=checked_url,
            required=provider.required,
            status="down" if provider.required else "skipped",
            latency_ms=None,
            error="unsupported URL scheme",
        )
    request = urllib_request.Request(
        checked_url,
        method="GET",
        headers={"User-Agent": "orxaq-autonomy/router-check"},
    )
    started = time.monotonic()
    try:
        with urllib_request.urlopen(request, timeout=max(1, int(timeout_sec))) as response:  # nosec B310
            status_code = int(getattr(response, "status", 200))
            body = response.read().decode("utf-8", errors="replace")
        latency_ms = round((time.monotonic() - started) * 1000.0, 3)
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}")
        payload = json.loads(body)
        if isinstance(payload, dict):
            if not isinstance(payload.get("data", []), list) and not isinstance(payload.get("models", []), list):
                raise ValueError("models list missing in response")
        return RouterProviderStatus(
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            checked_url=checked_url,
            required=provider.required,
            status="up",
            latency_ms=latency_ms,
            error="",
        )
    except urllib_error.HTTPError as err:
        latency_ms = round((time.monotonic() - started) * 1000.0, 3)
        return RouterProviderStatus(
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            checked_url=checked_url,
            required=provider.required,
            status="down" if provider.required else "skipped",
            latency_ms=latency_ms,
            error=f"HTTP {err.code}",
        )
    except urllib_error.URLError as err:
        return RouterProviderStatus(
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            checked_url=checked_url,
            required=provider.required,
            status="down" if provider.required else "skipped",
            latency_ms=None,
            error=str(err.reason)[:300],
        )
    except Exception as err:  # noqa: BLE001
        return RouterProviderStatus(
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            checked_url=checked_url,
            required=provider.required,
            status="down" if provider.required else "skipped",
            latency_ms=None,
            error=str(err)[:300],
        )


def run_router_check(
    *,
    root: str = ".",
    config_path: str = "./config/router.example.yaml",
    output_path: str = "./artifacts/router_check.json",
    profile: str = "",
    profiles_dir: str = "./router_profiles",
    active_config_output: str = "./config/router.active.yaml",
    lane: str = "",
    timeout_sec: int = 5,
) -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    config_file = Path(config_path).expanduser()
    if not config_file.is_absolute():
        config_file = (root_path / config_file).resolve()
    output_file = Path(output_path).expanduser()
    if not output_file.is_absolute():
        output_file = (root_path / output_file).resolve()

    profile_name = profile.strip()
    profile_result: dict[str, Any] | None = None
    if profile_name:
        profile_result = apply_router_profile(
            root=str(root_path),
            profile_name=profile_name,
            base_config_path=str(config_file),
            profiles_dir=profiles_dir,
            output_path=active_config_output,
        )
        config_file = Path(profile_result["active_config"]).resolve()

    payload = _read_router_config(str(config_file))
    providers = _extract_providers(payload)
    selected_names = _resolve_lane_providers(payload, lane.strip() or None)
    selected_name_set = set(selected_names)

    selected = [row for row in providers if row.name in selected_name_set] if selected_name_set else providers
    statuses = [_check_provider(row, timeout_sec=max(1, int(timeout_sec))) for row in selected]

    required_total = sum(1 for row in statuses if row.required)
    required_down = sum(1 for row in statuses if row.required and row.status != "up")
    up_total = sum(1 for row in statuses if row.status == "up")
    down_total = sum(1 for row in statuses if row.status == "down")
    skipped_total = sum(1 for row in statuses if row.status == "skipped")

    report = {
        "schema_version": "router-check.v1",
        "timestamp": _utc_now_iso(),
        "root": str(root_path),
        "config_path": str(config_file),
        "output_path": str(output_file),
        "profile": profile_name or None,
        "profile_result": profile_result,
        "lane": lane.strip() or None,
        "providers": [row.to_dict() for row in statuses],
        "summary": {
            "provider_total": len(statuses),
            "provider_up": up_total,
            "provider_down": down_total,
            "provider_skipped": skipped_total,
            "required_total": required_total,
            "required_down": required_down,
            "all_required_up": required_down == 0,
            "overall_ok": required_down == 0,
        },
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
