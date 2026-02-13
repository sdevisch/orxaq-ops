"""RouteLLM adapter with deterministic fallback for autonomy routing.

Provides an optional/configurable RouteLLM integration path for the autonomy
manager and runner. When RouteLLM is unavailable or disabled, falls back to
deterministic lane-based routing using the existing routing policy.

Zero external hard dependencies -- RouteLLM is imported lazily and the adapter
degrades gracefully when the library is absent.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RouteLLMConfig:
    """Configuration for the RouteLLM adapter."""

    enabled: bool = False
    router_model: str = "mf"  # matrix factorization router
    strong_model: str = "claude-sonnet-4-5-20250514"
    weak_model: str = "gpt-4o-mini"
    threshold: float = 0.5
    timeout_sec: int = 5
    # Kill switch: set to True to disable RouteLLM entirely at runtime
    kill_switch: bool = False

    @classmethod
    def from_env(cls) -> "RouteLLMConfig":
        """Build config from environment variables."""
        return cls(
            enabled=os.environ.get("ORXAQ_ROUTELLM_ENABLED", "").lower() in ("1", "true", "yes"),
            router_model=os.environ.get("ORXAQ_ROUTELLM_ROUTER_MODEL", "mf"),
            strong_model=os.environ.get("ORXAQ_ROUTELLM_STRONG_MODEL", "claude-sonnet-4-5-20250514"),
            weak_model=os.environ.get("ORXAQ_ROUTELLM_WEAK_MODEL", "gpt-4o-mini"),
            threshold=float(os.environ.get("ORXAQ_ROUTELLM_THRESHOLD", "0.5")),
            timeout_sec=int(os.environ.get("ORXAQ_ROUTELLM_TIMEOUT_SEC", "5")),
            kill_switch=os.environ.get("ORXAQ_ROUTELLM_KILL_SWITCH", "").lower()
            in ("1", "true", "yes"),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RouteLLMConfig":
        return cls(
            enabled=bool(payload.get("enabled", False)),
            router_model=str(payload.get("router_model", "mf")),
            strong_model=str(payload.get("strong_model", "claude-sonnet-4-5-20250514")),
            weak_model=str(payload.get("weak_model", "gpt-4o-mini")),
            threshold=float(payload.get("threshold", 0.5)),
            timeout_sec=int(payload.get("timeout_sec", 5)),
            kill_switch=bool(payload.get("kill_switch", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RoutingResult:
    """Result of a routing decision."""

    source: str  # "routellm" or "deterministic_fallback"
    selected_model: str
    reason: str
    latency_ms: float | None = None
    error: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Deterministic fallback lane mapping: complexity keyword → model
_DETERMINISTIC_LANE_MAP: dict[str, str] = {
    "critical": "claude-sonnet-4-5-20250514",
    "high": "claude-sonnet-4-5-20250514",
    "medium": "gpt-4o-mini",
    "low": "gpt-4o-mini",
}


def _classify_complexity_simple(description: str) -> str:
    """Simple keyword-based complexity for deterministic fallback."""
    lower = description.lower()
    critical_kw = ("consensus", "security audit", "vulnerability", "production deploy", "breaking change")
    high_kw = ("architect", "design", "debug", "investigate", "optimize", "security", "review")
    medium_kw = ("implement", "code", "write", "create", "build", "test", "fix", "refactor")
    if any(kw in lower for kw in critical_kw):
        return "critical"
    if any(kw in lower for kw in high_kw):
        return "high"
    if any(kw in lower for kw in medium_kw):
        return "medium"
    return "low"


def deterministic_route(description: str, config: RouteLLMConfig) -> RoutingResult:
    """Deterministic fallback routing -- no external dependencies required.

    Uses keyword-based complexity classification to pick between the configured
    strong and weak models.
    """
    complexity = _classify_complexity_simple(description)
    if complexity in ("critical", "high"):
        model = config.strong_model
    else:
        model = config.weak_model
    return RoutingResult(
        source="deterministic_fallback",
        selected_model=model,
        reason=f"deterministic route: complexity={complexity}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _routellm_available() -> bool:
    """Check if the routellm library is importable."""
    try:
        import routellm  # type: ignore[import-untyped]  # noqa: F401

        return True
    except ImportError:
        return False


def routellm_route(description: str, config: RouteLLMConfig) -> RoutingResult:
    """Route using the RouteLLM library.

    Falls back to deterministic routing on any error.
    """
    if not _routellm_available():
        result = deterministic_route(description, config)
        result.error = "routellm library not installed"
        result.source = "deterministic_fallback"
        return result

    started = time.monotonic()
    try:
        from routellm.controller import Controller  # type: ignore[import-untyped]

        controller = Controller(
            routers=[config.router_model],
            strong_model=config.strong_model,
            weak_model=config.weak_model,
        )
        response = controller.completion(
            model=f"router-{config.router_model}-{config.threshold}",
            messages=[{"role": "user", "content": description}],
        )
        latency = round((time.monotonic() - started) * 1000.0, 3)
        selected = str(response.model) if hasattr(response, "model") else config.weak_model
        return RoutingResult(
            source="routellm",
            selected_model=selected,
            reason=f"routellm router={config.router_model} threshold={config.threshold}",
            latency_ms=latency,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        latency = round((time.monotonic() - started) * 1000.0, 3)
        fallback = deterministic_route(description, config)
        fallback.error = f"routellm failed: {str(exc)[:300]}"
        fallback.latency_ms = latency
        return fallback


class RouteLLMAdapter:
    """Adapter providing RouteLLM integration with deterministic fallback.

    Usage:
        adapter = RouteLLMAdapter(config=RouteLLMConfig.from_env())
        result = adapter.route("implement a caching layer")
        print(result.selected_model, result.source)

    When disabled or on error, falls back to deterministic lane-based routing.
    """

    def __init__(self, config: RouteLLMConfig | None = None) -> None:
        self._config = config or RouteLLMConfig()
        self._decisions: list[RoutingResult] = []

    @property
    def config(self) -> RouteLLMConfig:
        return self._config

    @property
    def decisions(self) -> list[RoutingResult]:
        return list(self._decisions)

    def is_active(self) -> bool:
        """Return True if RouteLLM routing is enabled and not kill-switched."""
        return self._config.enabled and not self._config.kill_switch

    def route(self, description: str) -> RoutingResult:
        """Route a task description to a model.

        Uses RouteLLM when active, otherwise deterministic fallback.
        """
        if not self.is_active():
            result = deterministic_route(description, self._config)
            if self._config.kill_switch:
                result.reason = f"kill_switch active; {result.reason}"
            elif not self._config.enabled:
                result.reason = f"routellm disabled; {result.reason}"
        else:
            result = routellm_route(description, self._config)
        self._decisions.append(result)
        return result

    def status(self) -> dict[str, Any]:
        return {
            "config": self._config.to_dict(),
            "active": self.is_active(),
            "routellm_available": _routellm_available(),
            "decisions_count": len(self._decisions),
        }
