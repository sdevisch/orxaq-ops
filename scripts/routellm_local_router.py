#!/usr/bin/env python3
"""Lightweight local RouteLLM-compatible router service.

Endpoints:
- GET /health -> {"ok": true, ...}
- POST /route -> {"model": "<selected-model>", "strategy": "...", ...}
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_DEFAULT_OBJECTIVE_WEIGHTS = {
    "cost_speed": {"cost": 0.45, "speed": 0.45, "quality": 0.10},
    "balanced": {"cost": 0.30, "speed": 0.30, "quality": 0.40},
    "quality": {"cost": 0.10, "speed": 0.20, "quality": 0.70},
}
_PROFILE_WEIGHT_MULTIPLIERS = {
    "fast": {"cost": 1.10, "speed": 1.20, "quality": 0.75},
    "strong": {"cost": 0.85, "speed": 0.90, "quality": 1.25},
}


def _load_policy(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"policy must be a JSON object: {path}")
    return raw


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_models(raw: Any) -> list[str]:
    values: list[str]
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw]
    elif raw in (None, ""):
        values = []
    else:
        values = [part.strip() for part in re.split(r"[;,]", str(raw))]

    models: list[str] = []
    seen: set[str] = set()
    for item in values:
        model = str(item).strip()
        if not model:
            continue
        key = model.lower()
        if key in seen:
            continue
        seen.add(key)
        models.append(model)
    return models


def _canonical_allowed_model(model: str | None, allowed_models: list[str]) -> str | None:
    candidate = str(model or "").strip()
    if not candidate:
        return None
    if not allowed_models:
        return candidate
    lowered = candidate.lower()
    for item in allowed_models:
        allowed = str(item).strip()
        if allowed and allowed.lower() == lowered:
            return allowed
    return None


def _normalize_model_catalog(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    catalog: dict[str, dict[str, Any]] = {}
    for model_name, payload in raw.items():
        name = str(model_name).strip()
        if not name or not isinstance(payload, dict):
            continue
        catalog[name.lower()] = {
            "model": name,
            "input_per_million": _as_float(
                payload.get("input_per_million", payload.get("cost_input_per_million", 0.0)),
                0.0,
            ),
            "output_per_million": _as_float(
                payload.get("output_per_million", payload.get("cost_output_per_million", 0.0)),
                0.0,
            ),
            "blended_cost_per_million": _as_float(payload.get("blended_cost_per_million", 0.0), 0.0),
            "speed_tps": _as_float(payload.get("speed_tps", payload.get("tokens_per_sec", 0.0)), 0.0),
            "quality_score": _as_float(payload.get("quality_score", payload.get("quality", 0.0)), 0.0),
            "max_context_tokens": _as_int(payload.get("max_context_tokens", 0), 0),
            "local": _as_bool(payload.get("local", False), False),
            "family": str(payload.get("family", "")).strip(),
        }
    return catalog


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0.0:
        return default
    return numerator / denominator


def _normalized(value: float, min_value: float, max_value: float, invert: bool = False) -> float:
    if max_value <= min_value:
        return 1.0
    normalized = (value - min_value) / (max_value - min_value)
    normalized = max(0.0, min(1.0, normalized))
    if invert:
        normalized = 1.0 - normalized
    return normalized


def _model_profile(model: str, catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    key = model.lower()
    details = catalog.get(key, {}) if isinstance(catalog, dict) else {}

    input_cost = _as_float(details.get("input_per_million", 0.0), 0.0)
    output_cost = _as_float(details.get("output_per_million", 0.0), 0.0)
    blended_cost = _as_float(details.get("blended_cost_per_million", 0.0), 0.0)
    if blended_cost <= 0.0:
        blended_cost = (input_cost * 0.6) + (output_cost * 0.4)

    speed = _as_float(details.get("speed_tps", 0.0), 0.0)
    quality = _as_float(details.get("quality_score", 0.0), 0.0)
    max_context = _as_int(details.get("max_context_tokens", 0), 0)

    return {
        "model": model,
        "input_per_million": max(0.0, input_cost),
        "output_per_million": max(0.0, output_cost),
        "blended_cost_per_million": max(0.0, blended_cost),
        "speed_tps": max(0.0, speed),
        "quality_score": max(0.0, quality),
        "max_context_tokens": max(0, max_context),
        "local": _as_bool(details.get("local", False), False),
        "family": str(details.get("family", "")).strip(),
    }


def _resolve_objective(
    *,
    profile: str,
    provider_cfg: dict[str, Any],
    payload: dict[str, Any],
    difficulty: float,
    token_est: float,
) -> str:
    requested = str(payload.get("optimization_target", payload.get("objective", ""))).strip().lower()
    if requested in _DEFAULT_OBJECTIVE_WEIGHTS:
        return requested

    prompt = str(payload.get("prompt", "")).strip().lower()
    prompt_has_low_cost = False
    prompt_has_high_quality = False
    if prompt:
        low_cost_markers = ("unit test", "tests", "lint", "format", "documentation", "docs", "readme")
        high_quality_markers = ("architecture", "security", "threat", "migration", "refactor", "design")
        prompt_has_low_cost = any(marker in prompt for marker in low_cost_markers)
        prompt_has_high_quality = any(marker in prompt for marker in high_quality_markers)
        if prompt_has_high_quality and (profile == "strong" or difficulty >= 60.0 or token_est >= 6000):
            return "quality"
        if prompt_has_low_cost and not prompt_has_high_quality:
            return "cost_speed"

    if difficulty >= 70.0 or token_est >= 9000:
        return "quality" if profile == "strong" else "balanced"
    if difficulty <= 30.0 and token_est <= 4000:
        return "cost_speed"

    provider_default = str(provider_cfg.get("default_objective", "")).strip().lower()
    if provider_default in _DEFAULT_OBJECTIVE_WEIGHTS:
        return provider_default

    if prompt_has_high_quality:
        return "quality" if profile == "strong" else "balanced"

    return "balanced"


def _objective_weights(
    objective: str,
    *,
    profile: str,
    provider_cfg: dict[str, Any],
) -> dict[str, float]:
    base = dict(_DEFAULT_OBJECTIVE_WEIGHTS.get(objective, _DEFAULT_OBJECTIVE_WEIGHTS["balanced"]))

    provider_weights_raw = provider_cfg.get("objective_weights", {})
    if isinstance(provider_weights_raw, dict):
        override = provider_weights_raw.get(objective, {})
        if isinstance(override, dict):
            for key in ("cost", "speed", "quality"):
                if key in override:
                    base[key] = _as_float(override.get(key), base[key])

    multipliers = _PROFILE_WEIGHT_MULTIPLIERS.get(profile, {})
    for key in ("cost", "speed", "quality"):
        base[key] = max(0.0, _as_float(base.get(key, 0.0), 0.0) * _as_float(multipliers.get(key, 1.0), 1.0))

    total = sum(base.values())
    if total <= 0.0:
        return dict(_DEFAULT_OBJECTIVE_WEIGHTS["balanced"])
    return {key: round(_safe_div(value, total, 0.0), 6) for key, value in base.items()}


def _choose_model(
    *,
    candidates: list[dict[str, Any]],
    objective: str,
    profile: str,
    difficulty: float,
    token_est: float,
    weights: dict[str, float],
) -> tuple[str, list[dict[str, Any]]]:
    if not candidates:
        return "", []

    cost_values = [float(item.get("blended_cost_per_million", 0.0) or 0.0) for item in candidates]
    speed_values = [float(item.get("speed_tps", 0.0) or 0.0) for item in candidates]
    quality_values = [float(item.get("quality_score", 0.0) or 0.0) for item in candidates]

    min_cost, max_cost = min(cost_values), max(cost_values)
    min_speed, max_speed = min(speed_values), max(speed_values)
    min_quality, max_quality = min(quality_values), max(quality_values)

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        cost_score = _normalized(float(candidate.get("blended_cost_per_million", 0.0) or 0.0), min_cost, max_cost, invert=True)
        speed_score = _normalized(float(candidate.get("speed_tps", 0.0) or 0.0), min_speed, max_speed, invert=False)
        quality_score = _normalized(float(candidate.get("quality_score", 0.0) or 0.0), min_quality, max_quality, invert=False)

        context_penalty = 0.0
        max_context = int(candidate.get("max_context_tokens", 0) or 0)
        if max_context > 0 and token_est > 0:
            if token_est > max_context:
                context_penalty = 0.65
            elif token_est > (max_context * 0.9):
                context_penalty = 0.35

        profile_bias = 0.0
        if profile == "fast" and bool(candidate.get("local", False)):
            profile_bias += 0.03
        if profile == "strong" and objective == "quality":
            profile_bias += 0.02 * quality_score

        difficulty_bonus = 0.0
        if difficulty >= 75.0:
            difficulty_bonus += 0.07 * quality_score
        elif difficulty <= 25.0:
            difficulty_bonus += 0.05 * speed_score

        final_score = (
            (weights.get("cost", 0.0) * cost_score)
            + (weights.get("speed", 0.0) * speed_score)
            + (weights.get("quality", 0.0) * quality_score)
            + profile_bias
            + difficulty_bonus
            - context_penalty
        )

        ranked.append(
            {
                "model": candidate.get("model", ""),
                "score": round(final_score, 6),
                "cost_score": round(cost_score, 6),
                "speed_score": round(speed_score, 6),
                "quality_score": round(quality_score, 6),
                "context_penalty": round(context_penalty, 6),
                "blended_cost_per_million": round(float(candidate.get("blended_cost_per_million", 0.0) or 0.0), 6),
                "speed_tps": round(float(candidate.get("speed_tps", 0.0) or 0.0), 6),
                "quality_raw": round(float(candidate.get("quality_score", 0.0) or 0.0), 6),
            }
        )

    ranked.sort(key=lambda item: (float(item.get("score", 0.0)), float(item.get("quality_raw", 0.0))), reverse=True)
    selected_model = str(ranked[0].get("model", "")).strip()
    return selected_model, ranked


def route_model(policy: dict[str, Any], profile: str, payload: dict[str, Any]) -> dict[str, Any]:
    provider = str(payload.get("provider", "")).strip().lower() or "codex"
    requested_model = str(payload.get("requested_model", "")).strip()
    difficulty = _as_float(payload.get("prompt_difficulty_score", 0.0), 0.0)
    token_est = _as_float(payload.get("prompt_tokens_est", 0.0), 0.0)

    providers = policy.get("providers", {}) if isinstance(policy.get("providers", {}), dict) else {}
    provider_cfg = providers.get(provider, {}) if isinstance(providers.get(provider, {}), dict) else {}
    provider_enabled = _as_bool(provider_cfg.get("enabled", True), True)

    allowed_models = _normalize_models(provider_cfg.get("allowed_models", []))
    fallback_model = str(provider_cfg.get("fallback_model", "")).strip()
    respect_requested = _as_bool(provider_cfg.get("respect_requested_model", False), False)
    requested_allowed = _canonical_allowed_model(requested_model, allowed_models)
    fallback_allowed = _canonical_allowed_model(fallback_model, allowed_models)

    selected = requested_allowed or fallback_allowed or (allowed_models[0] if allowed_models else "")
    if not selected:
        selected = requested_model or fallback_model
    if not provider_enabled:
        return {
            "model": selected,
            "selected_model": selected,
            "strategy": "provider_disabled_fallback",
            "provider": provider,
            "objective": "disabled",
            "requested_model": requested_model,
            "allowed_models": allowed_models,
            "requested_model_allowed": bool(requested_model and requested_allowed),
            "fallback_model": fallback_model,
            "fallback_used": True,
            "reason": "provider_disabled",
        }

    if not allowed_models:
        return {
            "model": selected,
            "selected_model": selected,
            "strategy": "fallback",
            "provider": provider,
            "objective": "none",
            "requested_model": requested_model,
            "allowed_models": [],
            "requested_model_allowed": False,
            "fallback_model": fallback_model,
            "fallback_used": True,
            "reason": "no_allowed_models",
        }

    if requested_allowed and respect_requested:
        return {
            "model": requested_allowed,
            "selected_model": requested_allowed,
            "strategy": "requested_model_allowed",
            "provider": provider,
            "objective": "requested",
            "requested_model": requested_model,
            "allowed_models": allowed_models,
            "requested_model_allowed": True,
            "fallback_model": fallback_model,
            "fallback_used": False,
            "reason": "respect_requested_model",
        }

    catalog = _normalize_model_catalog(policy.get("model_catalog", {}))
    candidates = [_model_profile(model, catalog) for model in allowed_models]

    objective = _resolve_objective(
        profile=profile,
        provider_cfg=provider_cfg,
        payload=payload,
        difficulty=difficulty,
        token_est=token_est,
    )
    weights = _objective_weights(objective, profile=profile, provider_cfg=provider_cfg)

    selected_model, ranking = _choose_model(
        candidates=candidates,
        objective=objective,
        profile=profile,
        difficulty=difficulty,
        token_est=token_est,
        weights=weights,
    )

    if not selected_model:
        selected_model = fallback_allowed or requested_allowed or allowed_models[0]

    selected_profile = _model_profile(selected_model, catalog)
    blended_cost_per_million = float(selected_profile.get("blended_cost_per_million", 0.0) or 0.0)
    estimated_prompt_cost_usd = (token_est * blended_cost_per_million) / 1_000_000.0 if blended_cost_per_million > 0.0 else 0.0

    return {
        "model": selected_model,
        "selected_model": selected_model,
        "strategy": f"intelligent_{objective}",
        "provider": provider,
        "objective": objective,
        "weights": weights,
        "requested_model": requested_model,
        "allowed_models": allowed_models,
        "requested_model_allowed": bool(requested_model and requested_allowed),
        "fallback_model": fallback_model,
        "fallback_used": bool(not ranking),
        "reason": "score_ranked_selection" if ranking else "fallback_no_rank",
        "difficulty": round(difficulty, 4),
        "prompt_tokens_est": int(max(0.0, token_est)),
        "estimated_input_cost_per_million": round(float(selected_profile.get("input_per_million", 0.0) or 0.0), 6),
        "estimated_output_cost_per_million": round(float(selected_profile.get("output_per_million", 0.0) or 0.0), 6),
        "estimated_cost_per_million": round(blended_cost_per_million, 6),
        "estimated_prompt_cost_usd": round(estimated_prompt_cost_usd, 8),
        "estimated_speed_tps": round(float(selected_profile.get("speed_tps", 0.0) or 0.0), 6),
        "candidate_count": len(candidates),
        "candidate_scores": ranking[:6],
        "router_profile": profile,
        "router_version": "local-intelligent-v2",
    }


class RouteHandler(BaseHTTPRequestHandler):
    server_version = "local-routellm-router/2.0"

    def _json_response(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._json_response({"ok": False, "error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._json_response(
            {
                "ok": True,
                "profile": self.server.profile,  # type: ignore[attr-defined]
                "policy_file": str(self.server.policy_file),  # type: ignore[attr-defined]
                "router_version": "local-intelligent-v2",
            }
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/route":
            self._json_response({"ok": False, "error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json_response({"ok": False, "error": "invalid_json"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(payload, dict):
            self._json_response({"ok": False, "error": "invalid_payload"}, status=HTTPStatus.BAD_REQUEST)
            return

        policy = self.server.policy  # type: ignore[attr-defined]
        profile = str(self.server.profile)  # type: ignore[attr-defined]
        decision = route_model(policy, profile, payload)
        self._json_response(decision)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep service quiet unless explicitly tailed through log redirection.
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local RouteLLM-compatible router.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--policy-file", required=True)
    parser.add_argument("--profile", choices=("fast", "strong"), default="fast")
    args = parser.parse_args()

    policy_file = Path(args.policy_file).resolve()
    policy = _load_policy(policy_file)

    server = ThreadingHTTPServer((args.host, args.port), RouteHandler)
    server.policy = policy  # type: ignore[attr-defined]
    server.policy_file = policy_file  # type: ignore[attr-defined]
    server.profile = args.profile  # type: ignore[attr-defined]

    stopper = threading.Event()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        stopper.set()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
