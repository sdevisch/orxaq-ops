"""Network status detection for offline-first swarm routing.

Checks reachability of cloud API endpoints and local LM Studio to determine
the optimal routing strategy. Results are cached to minimize network overhead.
Zero external dependencies — uses only Python stdlib.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


@dataclass(frozen=True)
class EndpointCheck:
    """Result of checking a single endpoint."""
    name: str
    url: str
    reachable: bool
    latency_ms: float | None
    error: str = ""


# Well-known cloud API endpoints to probe (lightweight GET requests)
CLOUD_ENDPOINTS: list[dict[str, str]] = [
    {"name": "anthropic", "url": "https://api.anthropic.com/v1/models", "method": "GET"},
    {"name": "openai", "url": "https://api.openai.com/v1/models", "method": "GET"},
    {"name": "openrouter", "url": "https://openrouter.ai/api/v1/models", "method": "GET"},
    {"name": "gemini", "url": "https://generativelanguage.googleapis.com/v1beta/models", "method": "GET"},
]

LMSTUDIO_ENDPOINT = {"name": "lmstudio-local", "url": "http://localhost:1234/v1/models", "method": "GET"}


class NetworkStatus:
    """ONLINE, DEGRADED, or OFFLINE classification."""
    ONLINE = "online"         # >=2 cloud endpoints reachable
    DEGRADED = "degraded"     # 1 cloud endpoint reachable
    OFFLINE = "offline"       # 0 cloud endpoints reachable


@dataclass
class NetworkSnapshot:
    """Full network status report."""
    status: str  # NetworkStatus value
    lmstudio_available: bool
    cloud_endpoints_up: int
    cloud_endpoints_total: int
    checks: list[EndpointCheck]
    checked_at: str = ""
    cache_ttl_sec: int = 60

    def to_dict(self) -> dict[str, Any]:
        d = {
            "status": self.status,
            "lmstudio_available": self.lmstudio_available,
            "cloud_endpoints_up": self.cloud_endpoints_up,
            "cloud_endpoints_total": self.cloud_endpoints_total,
            "checked_at": self.checked_at,
            "cache_ttl_sec": self.cache_ttl_sec,
            "checks": [asdict(c) for c in self.checks],
        }
        return d

    @property
    def is_offline(self) -> bool:
        return self.status == NetworkStatus.OFFLINE

    @property
    def prefer_local(self) -> bool:
        """Should we prefer local models? True if offline or degraded."""
        return self.status != NetworkStatus.ONLINE


def _check_endpoint(name: str, url: str, timeout_sec: int = 3) -> EndpointCheck:
    """Probe a single endpoint with a lightweight GET request."""
    req = urllib_request.Request(
        url,
        method="GET",
        headers={"User-Agent": "orxaq-swarm/network-probe"},
    )
    started = time.monotonic()
    try:
        with urllib_request.urlopen(req, timeout=timeout_sec) as resp:
            # Just read a small amount to confirm connectivity
            resp.read(1024)
        latency = round((time.monotonic() - started) * 1000.0, 3)
        return EndpointCheck(name=name, url=url, reachable=True, latency_ms=latency)
    except urllib_error.HTTPError as exc:
        # HTTP errors (401, 403) still mean the endpoint is reachable
        latency = round((time.monotonic() - started) * 1000.0, 3)
        if exc.code in (401, 403, 404, 405):
            return EndpointCheck(name=name, url=url, reachable=True, latency_ms=latency)
        return EndpointCheck(
            name=name, url=url, reachable=False, latency_ms=latency,
            error=f"HTTP {exc.code}",
        )
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        return EndpointCheck(
            name=name, url=url, reachable=False, latency_ms=None,
            error=str(exc)[:200],
        )
    except Exception as exc:  # noqa: BLE001
        return EndpointCheck(
            name=name, url=url, reachable=False, latency_ms=None,
            error=str(exc)[:200],
        )


class NetworkProbe:
    """Cached network status detector.

    Checks are cached for `cache_ttl_sec` seconds to avoid excessive probing.
    """

    def __init__(
        self,
        *,
        lmstudio_url: str = "http://localhost:1234",
        cloud_endpoints: list[dict[str, str]] | None = None,
        timeout_sec: int = 3,
        cache_ttl_sec: int = 60,
    ):
        self._lmstudio_url = lmstudio_url.rstrip("/")
        self._cloud_endpoints = cloud_endpoints or CLOUD_ENDPOINTS
        self._timeout = timeout_sec
        self._cache_ttl = cache_ttl_sec
        self._cached_snapshot: NetworkSnapshot | None = None
        self._cached_at: float = 0.0

    def check(self, *, force: bool = False) -> NetworkSnapshot:
        """Check network status. Uses cache unless force=True or cache expired."""
        now = time.monotonic()
        if not force and self._cached_snapshot and (now - self._cached_at) < self._cache_ttl:
            return self._cached_snapshot

        checks: list[EndpointCheck] = []

        # Check LM Studio
        lm_check = _check_endpoint(
            LMSTUDIO_ENDPOINT["name"],
            f"{self._lmstudio_url}/v1/models",
            timeout_sec=min(2, self._timeout),
        )
        checks.append(lm_check)

        # Check cloud endpoints
        cloud_up = 0
        for ep in self._cloud_endpoints:
            result = _check_endpoint(ep["name"], ep["url"], timeout_sec=self._timeout)
            checks.append(result)
            if result.reachable:
                cloud_up += 1

        cloud_total = len(self._cloud_endpoints)

        # Classify status
        if cloud_up >= 2:
            status = NetworkStatus.ONLINE
        elif cloud_up == 1:
            status = NetworkStatus.DEGRADED
        else:
            status = NetworkStatus.OFFLINE

        snapshot = NetworkSnapshot(
            status=status,
            lmstudio_available=lm_check.reachable,
            cloud_endpoints_up=cloud_up,
            cloud_endpoints_total=cloud_total,
            checks=checks,
            checked_at=datetime.now(timezone.utc).isoformat(),
            cache_ttl_sec=self._cache_ttl,
        )

        self._cached_snapshot = snapshot
        self._cached_at = now
        return snapshot

    def invalidate_cache(self) -> None:
        """Force cache invalidation."""
        self._cached_snapshot = None
        self._cached_at = 0.0


# Module-level convenience

_default_probe: NetworkProbe | None = None


def get_probe(**kwargs: Any) -> NetworkProbe:
    """Get or create the default network probe."""
    global _default_probe
    if _default_probe is None:
        _default_probe = NetworkProbe(**kwargs)
    return _default_probe


def check_network(*, force: bool = False) -> NetworkSnapshot:
    """Check current network status (cached)."""
    return get_probe().check(force=force)


def is_offline() -> bool:
    """Quick check: are we offline?"""
    return check_network().is_offline


def prefer_local() -> bool:
    """Should we prefer local models?"""
    return check_network().prefer_local
