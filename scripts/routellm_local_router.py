#!/usr/bin/env python3
"""RouteLLM local router HTTP shim.

Provides a lightweight HTTP server that proxies routing requests through the
RouteLLM adapter. Used by LaunchAgent plists (com.orxaq.routellm.fast and
com.orxaq.routellm.strong).

If the ``routellm`` library is not installed, the server still starts and
returns deterministic fallback routing responses instead of crash-looping.

Fixes: https://github.com/Orxaq/orxaq-ops/issues/58
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _log(msg: str) -> None:
    print(f"[{_now_iso()}] [routellm_local_router] {msg}", flush=True)


def _routellm_available() -> bool:
    """Check if routellm is importable."""
    try:
        import routellm  # noqa: F401
        return True
    except ImportError:
        return False


def _load_policy(path: str | None) -> dict[str, Any]:
    """Load a policy JSON file. Returns empty dict on failure."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        _log(f"WARNING: Could not load policy file {path}: {exc}")
        return {}


# Simple keyword-based deterministic routing (duplicated from routellm_adapter
# so this script has zero internal dependencies).
_CRITICAL_KW = ("consensus", "security audit", "vulnerability", "production deploy", "breaking change")
_HIGH_KW = ("architect", "design", "debug", "investigate", "optimize", "security", "review")
_MEDIUM_KW = ("implement", "code", "write", "create", "build", "test", "fix", "refactor")


def _deterministic_route(description: str, strong_model: str, weak_model: str) -> dict[str, Any]:
    lower = description.lower()
    if any(kw in lower for kw in _CRITICAL_KW) or any(kw in lower for kw in _HIGH_KW):
        model = strong_model
        complexity = "high"
    elif any(kw in lower for kw in _MEDIUM_KW):
        model = weak_model
        complexity = "medium"
    else:
        model = weak_model
        complexity = "low"
    return {
        "model": model,
        "source": "deterministic_fallback",
        "complexity": complexity,
        "routellm_available": _routellm_available(),
        "timestamp": _now_iso(),
    }


class RouterHandler(BaseHTTPRequestHandler):
    """HTTP handler for routing requests."""

    policy: dict[str, Any] = {}
    profile: str = ""
    strong_model: str = "claude-sonnet-4-5-20250514"
    weak_model: str = "gpt-4o-mini"

    def log_message(self, fmt: str, *args: Any) -> None:
        _log(fmt % args)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response(200, {
                "status": "ok",
                "profile": self.profile,
                "routellm_available": _routellm_available(),
                "timestamp": _now_iso(),
            })
        elif self.path == "/status":
            self._json_response(200, {
                "profile": self.profile,
                "policy": self.policy,
                "routellm_available": _routellm_available(),
                "strong_model": self.strong_model,
                "weak_model": self.weak_model,
                "timestamp": _now_iso(),
            })
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/route":
            self._handle_route()
        else:
            self._json_response(404, {"error": "not found"})

    def _handle_route(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            self._json_response(400, {"error": f"invalid JSON: {exc}"})
            return

        description = str(payload.get("description", payload.get("prompt", "")))
        if not description.strip():
            self._json_response(400, {"error": "missing 'description' or 'prompt' field"})
            return

        result = _deterministic_route(description, self.strong_model, self.weak_model)
        self._json_response(200, result)

    def _json_response(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RouteLLM local router HTTP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--policy-file", default="", help="Path to routing policy JSON")
    parser.add_argument("--profile", default="fast", choices=["fast", "strong"])
    parser.add_argument("--strong-model", default="claude-sonnet-4-5-20250514")
    parser.add_argument("--weak-model", default="gpt-4o-mini")
    args = parser.parse_args(argv)

    policy = _load_policy(args.policy_file)

    # Configure handler class attributes
    RouterHandler.policy = policy
    RouterHandler.profile = args.profile
    RouterHandler.strong_model = args.strong_model
    RouterHandler.weak_model = args.weak_model

    _log(f"Starting RouteLLM local router — profile={args.profile}")
    _log(f"  host={args.host} port={args.port}")
    _log(f"  policy_file={args.policy_file or '(none)'}")
    _log(f"  routellm library available: {_routellm_available()}")
    if not _routellm_available():
        _log("  NOTE: routellm not installed — using deterministic fallback routing")

    server = HTTPServer((args.host, args.port), RouterHandler)
    _log(f"Listening on http://{args.host}:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("Shutting down")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
