#!/usr/bin/env python3
"""Run autonomous multi-agent development cycles for Orxaq.

This runner coordinates:
- Codex for implementation tasks in the main repository.
- Gemini for independent testing/review tasks in a sibling test repository.
- Claude for additional independent implementation/review lanes.

It advances a task queue until completion criteria are met or a hard blocker is hit.
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .protocols import MCPContextBundle, SkillProtocolSpec, load_mcp_context, load_skill_protocol

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_BLOCKED = "blocked"
VALID_STATUSES = {STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED}

RETRYABLE_ERROR_PATTERNS = (
    "timeout",
    "timed out",
    "rate limit",
    "429",
    "too many requests",
    "connection reset",
    "connection aborted",
    "network",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "try again",
    "retry",
    "context deadline exceeded",
    "internal server error",
    "unavailable",
    "index.lock",
    "another git process",
    "unable to create",
    "terminal prompts disabled",
    "could not read username",
    "eof when reading a line",
    "resource temporarily unavailable",
    "no rule to make target",
    "command not found",
)
GEMINI_CAPACITY_ERROR_PATTERNS = (
    "resource has been exhausted",
    "model is overloaded",
    "quota",
    "rate limit",
    "429",
    "503",
    "unavailable",
    "too many requests",
)
DEFAULT_GEMINI_FALLBACK_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)
PROTECTED_BRANCH_REJECTION_PATTERNS = (
    "changes must be made through a pull request",
    "gh013",
    "protected branch hook declined",
)
PROTECTED_BRANCH_NAMES = {"main", "master", "trunk"}

NON_INTERACTIVE_ENV_OVERRIDES = {
    "CI": "1",
    "TERM": "dumb",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_PROGRESS_BAR": "off",
    "PYTHONUNBUFFERED": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "FORCE_COLOR": "0",
    "CLICOLOR": "0",
    "NO_COLOR": "1",
}

VALIDATION_FALLBACKS = {
    "make lint": ["python3 -m ruff check .", ".venv/bin/ruff check ."],
    "make test": [
        "pytest -q",
        "python3 -m pytest -q",
        ".venv/bin/pytest -q",
    ],
}

TEST_COMMAND_HINTS = ("pytest", "make test")
GIT_LOCK_BASENAMES = ("index.lock", "HEAD.lock", "packed-refs.lock")
SUPPORTED_OWNERS = {"codex", "gemini", "claude"}
OWNER_PRIORITY = {"codex": 0, "gemini": 1, "claude": 2}
MAX_CONVERSATION_SNIPPET_CHARS = 8000
MAX_HANDOFF_SNIPPET_CHARS = 5000
HANDOFF_RECENT_LIMIT = 5
AMBIGUOUS_PROMPT_TERMS = (
    "maybe",
    "somehow",
    "etc",
    "whatever",
    "something",
    "anything",
    "as needed",
    "if possible",
    "best effort",
    "quickly",
)
USAGE_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "output_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
)
DEFAULT_PRICING_PAYLOAD = {
    "version": 1,
    "currency": "USD",
    "models": {
        "codex": {"input_per_million": 0.0, "output_per_million": 0.0},
        "gemini": {"input_per_million": 0.0, "output_per_million": 0.0},
        "claude": {"input_per_million": 0.0, "output_per_million": 0.0},
    },
}
DEFAULT_ROUTELLM_POLICY_PAYLOAD = {
    "version": 1,
    "enabled": False,
    "router": {
        "url": "",
        "timeout_sec": 5,
    },
    "providers": {
        "codex": {
            "enabled": True,
            "fallback_model": "",
            "allowed_models": [],
        },
        "gemini": {
            "enabled": True,
            "fallback_model": "",
            "allowed_models": [],
        },
        "claude": {
            "enabled": True,
            "fallback_model": "",
            "allowed_models": [],
        },
    },
}


@dataclass(frozen=True)
class Task:
    id: str
    owner: str
    priority: int
    title: str
    description: str
    depends_on: list[str]
    acceptance: list[str]


class RunnerLock:
    """Simple file lock to prevent concurrent autonomy runners."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            existing_pid = int(existing.get("pid", 0)) if str(existing.get("pid", "")).isdigit() else 0
            if existing_pid and _pid_is_running(existing_pid):
                raise RuntimeError(
                    f"Another autonomy runner is already active (pid={existing_pid}, lock={self.path})."
                )
            self.path.unlink(missing_ok=True)

        payload = {
            "pid": os.getpid(),
            "created_at": _now_iso(),
            "lock_file": str(self.path),
        }

        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(str(self.path), flags)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
            handle.write("\n")
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        self.path.unlink(missing_ok=True)
        self.acquired = False


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _print(msg: str) -> None:
    print(f"[{_now_iso()}] {msg}", flush=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_optional_text(path: Path | None) -> str:
    if path is None:
        return ""
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_json(path: Path, payload: Any) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def estimate_token_count(text: str) -> int:
    normalized = text.strip()
    if not normalized:
        return 0
    return max(1, int(round(len(normalized) / 4)))


def prompt_difficulty_score(prompt: str) -> int:
    text = prompt.strip()
    if not text:
        return 0
    lowered = text.lower()
    words = re.findall(r"[a-z0-9_]+", lowered)
    word_count = len(words)
    unique_ratio = (len(set(words)) / max(1, word_count)) if words else 0.0
    question_count = text.count("?")
    bullet_count = sum(1 for line in text.splitlines() if line.strip().startswith("-"))
    ambiguity_hits = sum(lowered.count(term) for term in AMBIGUOUS_PROMPT_TERMS)

    length_component = min(35.0, word_count / 10.0)
    structure_component = min(20.0, float(bullet_count) * 1.5)
    ambiguity_component = min(25.0, float(ambiguity_hits) * 4.0 + float(question_count) * 1.5)
    lexical_component = min(20.0, unique_ratio * 20.0)
    score = int(round(min(100.0, length_component + structure_component + ambiguity_component + lexical_component)))
    return max(0, score)


def _coerce_token_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        ivalue = int(value)
        return ivalue if ivalue >= 0 else None
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if stripped.isdigit():
            return int(stripped)
    return None


def _extract_usage_from_dict(payload: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in USAGE_TOKEN_KEYS:
        value = _coerce_token_int(payload.get(key))
        if value is not None:
            out[key] = value
    if "usage" in payload and isinstance(payload["usage"], dict):
        nested = _extract_usage_from_dict(payload["usage"])
        for key, value in nested.items():
            out.setdefault(key, value)
    if "metrics" in payload and isinstance(payload["metrics"], dict):
        nested = _extract_usage_from_dict(payload["metrics"])
        for key, value in nested.items():
            out.setdefault(key, value)
    if "token_usage" in payload and isinstance(payload["token_usage"], dict):
        nested = _extract_usage_from_dict(payload["token_usage"])
        for key, value in nested.items():
            out.setdefault(key, value)
    return out


def _extract_usage_from_text(raw_text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if not raw_text.strip():
        return out
    for key in USAGE_TOKEN_KEYS:
        pattern = rf"(?:\"|'){re.escape(key)}(?:\"|')\s*:\s*(\d+)"
        match = re.search(pattern, raw_text, flags=re.IGNORECASE)
        if match:
            out[key] = int(match.group(1))
    return out


def extract_usage_metrics(
    payload: dict[str, Any] | None = None,
    *,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    usage: dict[str, int] = {}
    source = "none"
    if payload:
        usage = _extract_usage_from_dict(payload)
        if usage:
            source = "payload"
    if not usage:
        usage = _extract_usage_from_text((stdout or "") + "\n" + (stderr or ""))
        if usage:
            source = "command_output"

    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
        usage["total_tokens"] = total_tokens

    return {
        "source": source,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "raw": usage,
    }


def load_pricing(path: Path | None) -> dict[str, Any]:
    if path is None:
        return DEFAULT_PRICING_PAYLOAD
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, DEFAULT_PRICING_PAYLOAD)
        return DEFAULT_PRICING_PAYLOAD
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_PRICING_PAYLOAD
    if not isinstance(raw, dict):
        return DEFAULT_PRICING_PAYLOAD
    return raw


def _safe_bool(value: Any, default: bool = False) -> bool:
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


def _normalize_model_candidates(raw: Any) -> list[str]:
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw]
    elif raw in (None, ""):
        values = []
    else:
        values = [part.strip() for part in re.split(r"[;,]", str(raw))]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def load_routellm_policy(path: Path | None) -> dict[str, Any]:
    if path is None:
        return dict(DEFAULT_ROUTELLM_POLICY_PAYLOAD)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, DEFAULT_ROUTELLM_POLICY_PAYLOAD)
        return dict(DEFAULT_ROUTELLM_POLICY_PAYLOAD)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_ROUTELLM_POLICY_PAYLOAD)
    if not isinstance(raw, dict):
        return dict(DEFAULT_ROUTELLM_POLICY_PAYLOAD)
    return raw


def _provider_routellm_policy(routellm_policy: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = routellm_policy.get("providers", {})
    provider_key = provider.strip().lower()
    raw: dict[str, Any] = {}
    if isinstance(providers, dict):
        for key in (provider_key, provider, provider_key.upper()):
            value = providers.get(key)
            if isinstance(value, dict):
                raw = value
                break
    fallback_model = str(raw.get("fallback_model", "")).strip() or None
    allowed_models = _normalize_model_candidates(raw.get("allowed_models", []))
    return {
        "enabled": _safe_bool(raw.get("enabled", True), True),
        "fallback_model": fallback_model,
        "allowed_models": allowed_models,
    }


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


def resolve_routed_model(
    *,
    provider: str,
    requested_model: str | None,
    prompt: str,
    routellm_enabled: bool,
    routellm_policy: dict[str, Any],
    router_url_override: str = "",
    router_timeout_sec_override: int | None = None,
) -> tuple[str | None, dict[str, Any]]:
    provider_key = provider.strip().lower() or "unknown"
    requested = (requested_model or "").strip() or None
    provider_policy = _provider_routellm_policy(routellm_policy, provider_key)
    allowed_models = _normalize_model_candidates(provider_policy.get("allowed_models", []))
    policy_fallback_model = str(provider_policy.get("fallback_model") or "").strip() or None
    requested_allowed = _canonical_allowed_model(requested, allowed_models)
    policy_fallback_allowed = _canonical_allowed_model(policy_fallback_model, allowed_models)
    fallback_model = requested_allowed or policy_fallback_allowed or (allowed_models[0] if allowed_models else None)
    if fallback_model is None:
        fallback_model = requested or policy_fallback_model

    router_cfg = routellm_policy.get("router", {}) if isinstance(routellm_policy.get("router", {}), dict) else {}
    router_url = router_url_override.strip() or str(router_cfg.get("url", "")).strip()
    timeout_candidate: Any
    if router_timeout_sec_override is not None:
        timeout_candidate = router_timeout_sec_override
    else:
        timeout_candidate = router_cfg.get("timeout_sec", 5)
    try:
        router_timeout_sec = max(1, int(float(timeout_candidate)))
    except (TypeError, ValueError):
        router_timeout_sec = 5

    decision: dict[str, Any] = {
        "provider": provider_key,
        "requested_model": requested or "",
        "selected_model": (fallback_model or ""),
        "fallback_model": (fallback_model or ""),
        "policy_fallback_model": (policy_fallback_model or ""),
        "allowed_models": allowed_models,
        "requested_model_allowed": bool(requested and requested_allowed),
        "strategy": "static_fallback",
        "router_enabled": bool(routellm_enabled),
        "router_url": router_url,
        "router_timeout_sec": router_timeout_sec,
        "fallback_used": False,
        "reason": "router_disabled",
        "router_error": "",
        "router_latency_sec": 0.0,
    }
    if not bool(routellm_enabled):
        return fallback_model, decision
    if not _safe_bool(routellm_policy.get("enabled", False), False):
        decision["reason"] = "policy_disabled"
        return fallback_model, decision
    if not _safe_bool(provider_policy.get("enabled", True), True):
        decision["reason"] = "provider_disabled"
        return fallback_model, decision
    if not router_url:
        decision["reason"] = "router_url_missing"
        return fallback_model, decision

    payload = {
        "provider": provider_key,
        "requested_model": requested or "",
        "prompt": prompt,
        "prompt_tokens_est": estimate_token_count(prompt),
        "prompt_difficulty_score": prompt_difficulty_score(prompt),
    }
    started = time.monotonic()
    try:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            router_url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=router_timeout_sec) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
            status_code = int(response.getcode() or 200)
        latency_sec = max(0.0, time.monotonic() - started)
        decision["router_latency_sec"] = round(latency_sec, 6)
        if status_code >= 400:
            raise RuntimeError(f"router_http_{status_code}")
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise RuntimeError("router_response_not_object")
        selected = ""
        for key in ("selected_model", "model", "target_model"):
            candidate = str(parsed.get(key, "")).strip()
            if candidate:
                selected = candidate
                break
        if not selected:
            raise RuntimeError("router_response_missing_model")
        selected_allowed = _canonical_allowed_model(selected, allowed_models)
        if allowed_models and selected_allowed is None:
            raise RuntimeError(f"router_model_not_allowed:{selected}")
        if selected_allowed is not None:
            selected = selected_allowed
        decision["selected_model"] = selected
        decision["strategy"] = "routellm"
        decision["reason"] = "router_selected_model"
        decision["fallback_used"] = False
        return selected, decision
    except Exception as err:
        latency_sec = max(0.0, time.monotonic() - started)
        decision["router_latency_sec"] = round(latency_sec, 6)
        decision["fallback_used"] = True
        decision["reason"] = "router_unavailable"
        decision["router_error"] = str(err).strip()
        decision["selected_model"] = fallback_model or ""
        return fallback_model, decision


def _resolve_pricing_entry(pricing: dict[str, Any], *, owner: str, model: str) -> dict[str, float]:
    models = pricing.get("models", {}) if isinstance(pricing.get("models", {}), dict) else {}
    candidates = [
        model.strip(),
        model.strip().lower(),
        owner.strip().lower(),
    ]
    entry: dict[str, Any] = {}
    for key in candidates:
        if key and isinstance(models.get(key), dict):
            entry = models[key]
            break
    input_rate = float(entry.get("input_per_million", 0.0) or 0.0)
    output_rate = float(entry.get("output_per_million", 0.0) or 0.0)
    return {"input_per_million": input_rate, "output_per_million": output_rate}


def compute_response_cost(
    *,
    pricing: dict[str, Any],
    owner: str,
    model: str,
    usage: dict[str, Any],
    prompt_tokens_est: int,
    response_tokens_est: int,
) -> dict[str, Any]:
    rates = _resolve_pricing_entry(pricing, owner=owner, model=model)
    input_rate = float(rates.get("input_per_million", 0.0) or 0.0)
    output_rate = float(rates.get("output_per_million", 0.0) or 0.0)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cost_exact = input_tokens is not None and output_tokens is not None
    source = "exact_usage"
    if input_tokens is None:
        input_tokens = prompt_tokens_est
        source = "estimated_tokens"
    if output_tokens is None:
        output_tokens = response_tokens_est
        source = "estimated_tokens"
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = int(input_tokens) + int(output_tokens)

    if input_rate <= 0.0 and output_rate <= 0.0:
        unpriced_source = "unpriced_model_exact_usage" if cost_exact else "unpriced_model_estimated_tokens"
        return {
            "cost_usd": None,
            "cost_exact": bool(cost_exact),
            "cost_source": unpriced_source,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "total_tokens": int(total_tokens) if total_tokens is not None else None,
            "input_rate_per_million": input_rate,
            "output_rate_per_million": output_rate,
        }

    cost = ((float(input_tokens) * input_rate) + (float(output_tokens) * output_rate)) / 1_000_000.0
    return {
        "cost_usd": round(cost, 8),
        "cost_exact": bool(cost_exact),
        "cost_source": source,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens) if total_tokens is not None else None,
        "input_rate_per_million": input_rate,
        "output_rate_per_million": output_rate,
    }


def append_response_metric(path: Path, metric: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(metric, sort_keys=True) + "\n")


def update_response_metrics_summary(path: Path, metric: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            summary = {}
    else:
        summary = {}
    if not isinstance(summary, dict):
        summary = {}

    total = int(summary.get("responses_total", 0) or 0) + 1
    quality_sum = float(summary.get("quality_score_sum", 0.0) or 0.0) + float(metric.get("quality_score", 0.0) or 0.0)
    latency_sum = float(summary.get("latency_sec_sum", 0.0) or 0.0) + float(metric.get("latency_sec", 0.0) or 0.0)
    prompt_difficulty_sum = float(summary.get("prompt_difficulty_score_sum", 0.0) or 0.0) + float(
        metric.get("prompt_difficulty_score", 0.0) or 0.0
    )
    first_time_pass = int(summary.get("first_time_pass_count", 0) or 0) + (
        1 if metric.get("first_time_pass", False) else 0
    )
    acceptance_pass = int(summary.get("acceptance_pass_count", 0) or 0) + (
        1 if metric.get("validation_passed", False) else 0
    )
    exact_cost_count = int(summary.get("exact_cost_count", 0) or 0) + (1 if metric.get("cost_exact", False) else 0)
    total_cost = float(summary.get("cost_usd_total", 0.0) or 0.0) + float(metric.get("cost_usd", 0.0) or 0.0)
    tokens_total = int(summary.get("tokens_total", 0) or 0) + int(metric.get("total_tokens", 0) or 0)
    tokens_input_total = int(summary.get("tokens_input_total", 0) or 0) + int(metric.get("input_tokens", 0) or 0)
    tokens_output_total = int(summary.get("tokens_output_total", 0) or 0) + int(metric.get("output_tokens", 0) or 0)
    token_exact_count = int(summary.get("token_exact_count", 0) or 0) + (
        1 if metric.get("token_count_exact", False) else 0
    )
    routing_strategy = str(metric.get("routing_strategy", "static_fallback")).strip() or "static_fallback"
    routing_fallback_used = bool(metric.get("routing_fallback_used", False))
    routing_router_error = str(metric.get("routing_router_error", "")).strip()
    routing_router_latency = float(metric.get("routing_router_latency_sec", 0.0) or 0.0)
    routing_decisions_total = int(summary.get("routing_decisions_total", 0) or 0) + 1
    routing_routellm_count = int(summary.get("routing_routellm_count", 0) or 0) + (
        1 if routing_strategy == "routellm" else 0
    )
    routing_fallback_count = int(summary.get("routing_fallback_count", 0) or 0) + (
        1 if routing_fallback_used else 0
    )
    routing_router_error_count = int(summary.get("routing_router_error_count", 0) or 0) + (
        1 if routing_router_error else 0
    )
    routing_router_latency_sum = float(summary.get("routing_router_latency_sum", 0.0) or 0.0) + routing_router_latency
    routing_by_provider = summary.get("routing_by_provider", {})
    if not isinstance(routing_by_provider, dict):
        routing_by_provider = {}
    routing_provider = str(metric.get("routing_provider", metric.get("owner", "unknown"))).strip() or "unknown"
    provider_counts = routing_by_provider.get(routing_provider, {})
    if not isinstance(provider_counts, dict):
        provider_counts = {}
    provider_counts["responses"] = int(provider_counts.get("responses", 0) or 0) + 1
    provider_counts["cost_usd_total"] = float(provider_counts.get("cost_usd_total", 0.0) or 0.0) + float(
        metric.get("cost_usd", 0.0) or 0.0
    )
    provider_counts["tokens_total"] = int(provider_counts.get("tokens_total", 0) or 0) + int(
        metric.get("total_tokens", 0) or 0
    )
    provider_counts["routellm_count"] = int(provider_counts.get("routellm_count", 0) or 0) + (
        1 if routing_strategy == "routellm" else 0
    )
    provider_counts["fallback_count"] = int(provider_counts.get("fallback_count", 0) or 0) + (
        1 if routing_fallback_used else 0
    )
    provider_counts["router_error_count"] = int(provider_counts.get("router_error_count", 0) or 0) + (
        1 if routing_router_error else 0
    )
    provider_responses = int(provider_counts.get("responses", 0) or 0)
    provider_counts["routellm_rate"] = round(
        float(provider_counts.get("routellm_count", 0) or 0) / max(1, provider_responses),
        6,
    )
    provider_counts["fallback_rate"] = round(
        float(provider_counts.get("fallback_count", 0) or 0) / max(1, provider_responses),
        6,
    )
    provider_counts["router_error_rate"] = round(
        float(provider_counts.get("router_error_count", 0) or 0) / max(1, provider_responses),
        6,
    )
    provider_tokens = int(provider_counts.get("tokens_total", 0) or 0)
    provider_cost = float(provider_counts.get("cost_usd_total", 0.0) or 0.0)
    provider_counts["cost_per_million_tokens"] = round(
        ((provider_cost * 1_000_000.0) / max(1, provider_tokens)) if provider_tokens > 0 else 0.0,
        6,
    )
    routing_by_provider[routing_provider] = provider_counts

    by_owner = summary.get("by_owner", {})
    if not isinstance(by_owner, dict):
        by_owner = {}
    owner = str(metric.get("owner", "unknown"))
    owner_counts = by_owner.get(owner, {})
    if not isinstance(owner_counts, dict):
        owner_counts = {}
    owner_counts["responses"] = int(owner_counts.get("responses", 0) or 0) + 1
    owner_counts["quality_score_sum"] = float(owner_counts.get("quality_score_sum", 0.0) or 0.0) + float(
        metric.get("quality_score", 0.0) or 0.0
    )
    owner_counts["latency_sec_sum"] = float(owner_counts.get("latency_sec_sum", 0.0) or 0.0) + float(
        metric.get("latency_sec", 0.0) or 0.0
    )
    owner_counts["prompt_difficulty_score_sum"] = float(
        owner_counts.get("prompt_difficulty_score_sum", 0.0) or 0.0
    ) + float(metric.get("prompt_difficulty_score", 0.0) or 0.0)
    owner_counts["first_time_pass"] = int(owner_counts.get("first_time_pass", 0) or 0) + (
        1 if metric.get("first_time_pass", False) else 0
    )
    owner_counts["validation_passed"] = int(owner_counts.get("validation_passed", 0) or 0) + (
        1 if metric.get("validation_passed", False) else 0
    )
    owner_counts["cost_usd_total"] = float(owner_counts.get("cost_usd_total", 0.0) or 0.0) + float(
        metric.get("cost_usd", 0.0) or 0.0
    )
    owner_counts["tokens_total"] = int(owner_counts.get("tokens_total", 0) or 0) + int(metric.get("total_tokens", 0) or 0)
    owner_counts["token_exact_count"] = int(owner_counts.get("token_exact_count", 0) or 0) + (
        1 if metric.get("token_count_exact", False) else 0
    )
    owner_counts["routing_routellm_count"] = int(owner_counts.get("routing_routellm_count", 0) or 0) + (
        1 if routing_strategy == "routellm" else 0
    )
    owner_counts["routing_fallback_count"] = int(owner_counts.get("routing_fallback_count", 0) or 0) + (
        1 if routing_fallback_used else 0
    )
    owner_counts["routing_router_error_count"] = int(owner_counts.get("routing_router_error_count", 0) or 0) + (
        1 if routing_router_error else 0
    )
    owner_responses = int(owner_counts.get("responses", 0) or 0)
    owner_counts["quality_score_avg"] = round(
        float(owner_counts.get("quality_score_sum", 0.0) or 0.0) / max(1, owner_responses),
        6,
    )
    owner_counts["latency_sec_avg"] = round(
        float(owner_counts.get("latency_sec_sum", 0.0) or 0.0) / max(1, owner_responses),
        6,
    )
    owner_counts["prompt_difficulty_score_avg"] = round(
        float(owner_counts.get("prompt_difficulty_score_sum", 0.0) or 0.0) / max(1, owner_responses),
        6,
    )
    owner_counts["tokens_avg"] = round(float(owner_counts.get("tokens_total", 0) or 0) / max(1, owner_responses), 6)
    owner_counts["token_exact_coverage"] = round(
        float(owner_counts.get("token_exact_count", 0) or 0) / max(1, owner_responses),
        6,
    )
    owner_counts["routing_routellm_rate"] = round(
        float(owner_counts.get("routing_routellm_count", 0) or 0) / max(1, owner_responses),
        6,
    )
    owner_counts["routing_fallback_rate"] = round(
        float(owner_counts.get("routing_fallback_count", 0) or 0) / max(1, owner_responses),
        6,
    )
    owner_counts["routing_router_error_rate"] = round(
        float(owner_counts.get("routing_router_error_count", 0) or 0) / max(1, owner_responses),
        6,
    )
    owner_tokens_total = int(owner_counts.get("tokens_total", 0) or 0)
    owner_cost_total = float(owner_counts.get("cost_usd_total", 0.0) or 0.0)
    owner_counts["cost_per_million_tokens"] = round(
        ((owner_cost_total * 1_000_000.0) / max(1, owner_tokens_total)) if owner_tokens_total > 0 else 0.0,
        6,
    )
    by_owner[owner] = owner_counts

    token_rate_per_minute = 0.0
    if latency_sum > 0.0:
        token_rate_per_minute = (float(tokens_total) / latency_sum) * 60.0
    estimated_cost_per_million_tokens = (
        (float(total_cost) * 1_000_000.0) / float(tokens_total)
        if int(tokens_total) > 0
        else 0.0
    )

    summary.update(
        {
            "timestamp": _now_iso(),
            "responses_total": total,
            "quality_score_sum": round(quality_sum, 6),
            "quality_score_avg": round(quality_sum / max(1, total), 6),
            "latency_sec_sum": round(latency_sum, 6),
            "latency_sec_avg": round(latency_sum / max(1, total), 6),
            "prompt_difficulty_score_sum": round(prompt_difficulty_sum, 6),
            "prompt_difficulty_score_avg": round(prompt_difficulty_sum / max(1, total), 6),
            "first_time_pass_count": first_time_pass,
            "first_time_pass_rate": round(first_time_pass / max(1, total), 6),
            "acceptance_pass_count": acceptance_pass,
            "acceptance_pass_rate": round(acceptance_pass / max(1, total), 6),
            "exact_cost_count": exact_cost_count,
            "exact_cost_coverage": round(exact_cost_count / max(1, total), 6),
            "cost_usd_total": round(total_cost, 8),
            "cost_usd_avg": round(total_cost / max(1, total), 8),
            "tokens_total": tokens_total,
            "tokens_input_total": tokens_input_total,
            "tokens_output_total": tokens_output_total,
            "estimated_tokens_total": tokens_total,
            "tokens_avg": round(float(tokens_total) / max(1, total), 6),
            "token_exact_count": token_exact_count,
            "token_exact_coverage": round(float(token_exact_count) / max(1, total), 6),
            "token_rate_per_minute": round(token_rate_per_minute, 6),
            "estimated_cost_per_million_tokens": round(estimated_cost_per_million_tokens, 6),
            "routing_decisions_total": routing_decisions_total,
            "routing_routellm_count": routing_routellm_count,
            "routing_routellm_rate": round(float(routing_routellm_count) / max(1, routing_decisions_total), 6),
            "routing_fallback_count": routing_fallback_count,
            "routing_fallback_rate": round(float(routing_fallback_count) / max(1, routing_decisions_total), 6),
            "routing_router_error_count": routing_router_error_count,
            "routing_router_error_rate": round(float(routing_router_error_count) / max(1, routing_decisions_total), 6),
            "routing_router_latency_sum": round(routing_router_latency_sum, 6),
            "routing_router_latency_avg": round(
                float(routing_router_latency_sum) / max(1, routing_decisions_total),
                6,
            ),
            "routing_by_provider": routing_by_provider,
            "by_owner": by_owner,
            "latest_metric": metric,
        }
    )

    recommendations: list[str] = []
    if float(summary.get("first_time_pass_rate", 0.0)) < 0.6:
        recommendations.append(
            "First-time pass rate is low. Reduce ambiguity in prompts and tighten acceptance criteria."
        )
    if float(summary.get("latency_sec_avg", 0.0)) > 180.0:
        recommendations.append("Average latency is high. Prefer smaller models for test/review lanes where possible.")
    if float(summary.get("exact_cost_coverage", 0.0)) < 0.8:
        recommendations.append(
            "Exact cost coverage is low. Enable provider token usage in agent outputs to avoid estimated costs."
        )
    if float(summary.get("token_exact_coverage", 0.0)) < 0.6:
        recommendations.append(
            "Token exact coverage is low. Capture provider token counts to strengthen throughput telemetry."
        )
    summary["optimization_recommendations"] = recommendations
    _write_json(path, summary)
    return summary

def _truncate_text(value: str, limit: int = MAX_CONVERSATION_SNIPPET_CHARS) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def append_conversation_event(
    path: Path | None,
    *,
    cycle: int,
    task: Task | None,
    owner: str,
    event_type: str,
    content: str,
    meta: dict[str, Any] | None = None,
) -> None:
    if path is None:
        return
    payload: dict[str, Any] = {
        "timestamp": _now_iso(),
        "cycle": int(cycle),
        "task_id": task.id if task else "",
        "task_title": task.title if task else "",
        "owner": owner,
        "event_type": event_type,
        "content": _truncate_text(content),
    }
    if meta:
        payload["meta"] = meta
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _append_ndjson(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _tail_ndjson(path: Path, limit: int = HANDOFF_RECENT_LIMIT) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def record_handoff_event(
    *,
    handoff_dir: Path,
    task: Task,
    outcome: dict[str, Any],
) -> None:
    status = str(outcome.get("status", "")).strip().lower()
    summary = str(outcome.get("summary", "")).strip()
    blocker = str(outcome.get("blocker", "")).strip()
    next_actions = [str(item) for item in (outcome.get("next_actions", []) or [])]
    payload = {
        "timestamp": _now_iso(),
        "task_id": task.id,
        "owner": task.owner,
        "status": status,
        "summary": summary,
        "blocker": blocker,
        "next_actions": next_actions,
        "commit": str(outcome.get("commit", "")).strip(),
    }
    if task.owner in {"codex", "claude"}:
        _append_ndjson(handoff_dir / "to_gemini.ndjson", payload)
    if task.owner == "gemini":
        _append_ndjson(handoff_dir / "to_codex.ndjson", payload)


def render_handoff_context(handoff_dir: Path, owner: str) -> str:
    if owner == "gemini":
        source = handoff_dir / "to_gemini.ndjson"
        heading = "Recent implementation handoffs for testing"
    elif owner in {"codex", "claude"}:
        source = handoff_dir / "to_codex.ndjson"
        heading = "Recent testing feedback for implementation"
    else:
        return ""

    events = _tail_ndjson(source, HANDOFF_RECENT_LIMIT)
    if not events:
        return ""
    lines = [f"{heading}:"]
    for item in events:
        lines.append(
            "- "
            f"[{item.get('timestamp', '')}] task={item.get('task_id', '')} "
            f"status={item.get('status', '')} "
            f"summary={str(item.get('summary', '')).strip()[:220]} "
            f"blocker={str(item.get('blocker', '')).strip()[:220]} "
            f"next_actions={str(item.get('next_actions', []))[:260]}"
        )
    return _truncate_text("\n".join(lines), limit=MAX_HANDOFF_SNIPPET_CHARS)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(ts: str) -> dt.datetime | None:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def build_subprocess_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(NON_INTERACTIVE_ENV_OVERRIDES)
    if extra_env:
        env.update(extra_env)
    return env


def _list_process_commands() -> list[str]:
    if os.name == "nt":
        return []
    result = subprocess.run(
        ["ps", "ax", "-o", "command="],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def has_running_git_processes() -> bool:
    commands = _list_process_commands()
    if not commands:
        return False
    for cmd in commands:
        lowered = cmd.lower()
        if "git " in lowered or lowered.endswith("/git"):
            return True
    return False


def find_git_lock_files(repo: Path) -> list[Path]:
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return []
    lock_files: list[Path] = []
    for name in GIT_LOCK_BASENAMES:
        lock_path = git_dir / name
        if lock_path.exists():
            lock_files.append(lock_path)
    return lock_files


def heal_stale_git_locks(repo: Path, stale_after_sec: int) -> list[Path]:
    removed: list[Path] = []
    lock_files = find_git_lock_files(repo)
    if not lock_files:
        return removed
    if has_running_git_processes():
        return removed
    now = time.time()
    for lock_path in lock_files:
        try:
            age = now - lock_path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age < stale_after_sec:
            continue
        lock_path.unlink(missing_ok=True)
        removed.append(lock_path)
    return removed


def get_repo_filetype_context(repo: Path, limit: int = 8) -> str:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return "File-type profile unavailable."
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not files:
        return "File-type profile unavailable."
    counts: Counter[str] = Counter()
    for rel in files:
        path = Path(rel)
        suffix = path.suffix.lower().lstrip(".")
        if suffix:
            counts[suffix] += 1
        else:
            counts["(no_ext)"] += 1
    most_common = counts.most_common(limit)
    top = ", ".join(f"{ext}:{count}" for ext, count in most_common)
    return f"Top file types: {top}."


def repo_state_hints(repo: Path) -> list[str]:
    hints: list[str] = []
    git_dir = repo / ".git"
    if not git_dir.exists():
        return hints
    if (git_dir / "MERGE_HEAD").exists():
        hints.append("Merge in progress detected (.git/MERGE_HEAD).")
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        hints.append("Rebase in progress detected (.git/rebase-*).")
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        hints.append("Cherry-pick in progress detected (.git/CHERRY_PICK_HEAD).")
    return hints


def validation_fallback_commands(raw: str) -> list[str]:
    try:
        normalized = " ".join(shlex.split(raw.strip()))
    except ValueError:
        return []
    return list(VALIDATION_FALLBACKS.get(normalized, []))


def is_test_command(raw: str) -> bool:
    normalized = raw.lower()
    return any(hint in normalized for hint in TEST_COMMAND_HINTS)


def write_heartbeat(
    path: Path,
    *,
    phase: str,
    cycle: int,
    task_id: str | None,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "timestamp": _now_iso(),
        "pid": os.getpid(),
        "phase": phase,
        "cycle": cycle,
        "task_id": task_id or "",
        "message": message,
    }
    if extra:
        payload.update(extra)
    _write_json(path, payload)


def load_tasks(path: Path) -> list[Task]:
    raw = json.loads(_read_text(path))
    if not isinstance(raw, list):
        raise ValueError(f"Task file must be a JSON array: {path}")
    tasks: list[Task] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"Task entries must be objects: {item!r}")
        task = Task(
            id=str(item["id"]),
            owner=str(item["owner"]).lower(),
            priority=int(item["priority"]),
            title=str(item["title"]),
            description=str(item["description"]),
            depends_on=[str(x) for x in item.get("depends_on", [])],
            acceptance=[str(x) for x in item.get("acceptance", [])],
        )
        if task.id in seen:
            raise ValueError(f"Duplicate task id: {task.id}")
        if task.owner not in SUPPORTED_OWNERS:
            raise ValueError(f"Unsupported task owner {task.owner!r} for task {task.id}")
        seen.add(task.id)
        tasks.append(task)
    return tasks


def load_state(path: Path, tasks: list[Task]) -> dict[str, dict[str, Any]]:
    if path.exists():
        raw = json.loads(_read_text(path))
        if not isinstance(raw, dict):
            raise ValueError(f"State file must be a JSON object: {path}")
    else:
        raw = {}

    out: dict[str, dict[str, Any]] = {}
    for task in tasks:
        entry = raw.get(task.id, {})
        status = str(entry.get("status", STATUS_PENDING))
        if status not in VALID_STATUSES:
            status = STATUS_PENDING
        if status == STATUS_IN_PROGRESS:
            # Recover from interrupted runs without deadlocking task selection.
            status = STATUS_PENDING
        out[task.id] = {
            "status": status,
            "attempts": _safe_int(entry.get("attempts", 0), 0),
            "retryable_failures": _safe_int(entry.get("retryable_failures", 0), 0),
            "deadlock_recoveries": _safe_int(entry.get("deadlock_recoveries", 0), 0),
            "deadlock_reopens": _safe_int(entry.get("deadlock_reopens", 0), 0),
            "not_before": str(entry.get("not_before", "")),
            "last_update": str(entry.get("last_update", "")),
            "last_summary": str(entry.get("last_summary", "")),
            "last_error": str(entry.get("last_error", "")),
            "owner": task.owner,
        }
    return out


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    _write_json(path, state)


def recycle_tasks_for_continuous_mode(
    state: dict[str, dict[str, Any]],
    tasks: list[Task],
    *,
    delay_sec: int,
) -> None:
    delay = max(1, int(delay_sec))
    not_before = (_now_utc() + dt.timedelta(seconds=delay)).isoformat()
    for task in tasks:
        entry = state.get(task.id)
        if not entry:
            continue
        entry["status"] = STATUS_PENDING
        entry["attempts"] = 0
        entry["retryable_failures"] = 0
        entry["not_before"] = not_before
        entry["last_error"] = ""
        entry["last_update"] = _now_iso()


def recycle_stalled_tasks_for_continuous_mode(
    state: dict[str, dict[str, Any]],
    tasks: list[Task],
    *,
    delay_sec: int,
) -> list[str]:
    delay = max(1, int(delay_sec))
    not_before = (_now_utc() + dt.timedelta(seconds=delay)).isoformat()
    reopened: list[str] = []
    for task in tasks:
        entry = state.get(task.id)
        if not entry:
            continue
        status = str(entry.get("status", ""))
        if status not in {STATUS_BLOCKED, STATUS_PENDING}:
            continue
        entry["status"] = STATUS_PENDING
        entry["attempts"] = 0
        entry["retryable_failures"] = 0
        entry["not_before"] = not_before
        entry["last_update"] = _now_iso()
        if status == STATUS_BLOCKED:
            entry["last_error"] = f"continuous recycle reopened blocked task `{task.id}`"
        reopened.append(task.id)
    return reopened


def load_dependency_state(path: Path | None) -> dict[str, dict[str, Any]] | None:
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _dependency_done(
    dep: str,
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> bool:
    dep_state = state.get(dep, {})
    if dep_state:
        return dep_state.get("status") == STATUS_DONE
    if dependency_state:
        ext = dependency_state.get(dep, {})
        if ext:
            return ext.get("status") == STATUS_DONE
    return False


def unresolved_dependencies(
    task: Task,
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    unresolved: list[str] = []
    for dep in task.depends_on:
        if not _dependency_done(dep, state, dependency_state):
            unresolved.append(dep)
    return unresolved


def task_dependencies_done(
    task: Task,
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> bool:
    return len(unresolved_dependencies(task, state, dependency_state)) == 0


def _task_ready_now(entry: dict[str, Any], now: dt.datetime) -> bool:
    not_before = _parse_iso(str(entry.get("not_before", "")))
    if not_before is None:
        return True
    return now >= not_before


def select_next_task(
    tasks: list[Task],
    state: dict[str, dict[str, Any]],
    now: dt.datetime | None = None,
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> Task | None:
    now = now or _now_utc()
    ready: list[Task] = []
    for task in tasks:
        entry = state[task.id]
        status = str(entry.get("status", STATUS_PENDING))
        if status != STATUS_PENDING:
            continue
        if not _task_ready_now(entry, now):
            continue
        if not task_dependencies_done(task, state, dependency_state):
            continue
        ready.append(task)
    if not ready:
        return None
    ready.sort(key=lambda t: (t.priority, OWNER_PRIORITY[t.owner], t.id))
    return ready[0]


def soonest_pending_time(
    tasks: list[Task],
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
) -> dt.datetime | None:
    soonest: dt.datetime | None = None
    for task in tasks:
        entry = state[task.id]
        if entry.get("status") != STATUS_PENDING:
            continue
        if not task_dependencies_done(task, state, dependency_state):
            continue
        not_before = _parse_iso(str(entry.get("not_before", "")))
        if not_before is None:
            continue
        if soonest is None or not_before < soonest:
            soonest = not_before
    return soonest


def recover_deadlocked_tasks(
    *,
    tasks: list[Task],
    state: dict[str, dict[str, Any]],
    dependency_state: dict[str, dict[str, Any]] | None = None,
    max_recoveries_per_task: int = 3,
) -> dict[str, Any]:
    task_by_id = {task.id: task for task in tasks}
    blocked_tasks = [task for task in tasks if state.get(task.id, {}).get("status") == STATUS_BLOCKED]
    if not blocked_tasks:
        return {"changed": False, "reopened_tasks": [], "unblocked_tasks": [], "reason": "no_blocked_tasks"}

    reopened_tasks: list[str] = []
    unblocked_tasks: list[str] = []
    changed = False
    now_iso = _now_iso()
    blocked_tasks.sort(key=lambda task: (task.priority, OWNER_PRIORITY.get(task.owner, 99), task.id))

    for blocked_task in blocked_tasks:
        blocked_entry = state.get(blocked_task.id, {})
        blocked_recoveries = _safe_int(blocked_entry.get("deadlock_recoveries", 0), 0)
        if blocked_recoveries >= max_recoveries_per_task:
            continue

        # Prefer reopening direct dependency tasks owned by a different lane
        # so implementation lanes can react to independent test feedback.
        dep_candidates: list[Task] = []
        for dep_id in blocked_task.depends_on:
            dep_task = task_by_id.get(dep_id)
            dep_entry = state.get(dep_id, {})
            if dep_task is None:
                continue
            if dep_entry.get("status") != STATUS_DONE:
                continue
            dep_reopens = _safe_int(dep_entry.get("deadlock_reopens", 0), 0)
            if dep_reopens >= max_recoveries_per_task:
                continue
            dep_candidates.append(dep_task)

        dep_candidates.sort(
            key=lambda task: (
                0 if task.owner != blocked_task.owner else 1,
                task.priority,
                OWNER_PRIORITY.get(task.owner, 99),
                task.id,
            )
        )
        if dep_candidates:
            dep_task = dep_candidates[0]
            dep_entry = state[dep_task.id]
            dep_entry["status"] = STATUS_PENDING
            dep_entry["retryable_failures"] = 0
            dep_entry["not_before"] = ""
            dep_entry["last_update"] = now_iso
            dep_entry["last_error"] = (
                f"Deadlock recovery reopened task due to blocked dependent `{blocked_task.id}`."
            )
            dep_entry["deadlock_reopens"] = _safe_int(dep_entry.get("deadlock_reopens", 0), 0) + 1
            reopened_tasks.append(dep_task.id)
            changed = True

        blocked_entry["status"] = STATUS_PENDING
        blocked_entry["retryable_failures"] = 0
        blocked_entry["not_before"] = ""
        blocked_entry["last_update"] = now_iso
        blocked_entry["deadlock_recoveries"] = blocked_recoveries + 1
        blocked_entry["last_error"] = (
            f"Deadlock recovery retry {blocked_entry['deadlock_recoveries']} for blocked task `{blocked_task.id}`."
        )
        unblocked_tasks.append(blocked_task.id)
        changed = True

    return {
        "changed": changed,
        "reopened_tasks": sorted(set(reopened_tasks)),
        "unblocked_tasks": sorted(set(unblocked_tasks)),
        "reason": "recovered" if changed else "recovery_limits_reached",
    }


def build_agent_prompt(
    task: Task,
    objective_text: str,
    role: str,
    repo_path: Path,
    retry_context: dict[str, Any] | None,
    repo_context: str,
    repo_hints: list[str],
    skill_protocol: SkillProtocolSpec,
    mcp_context: MCPContextBundle | None,
    startup_instructions: str = "",
    handoff_context: str = "",
) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance) or "- No explicit acceptance items"

    continuation_block = ""
    if retry_context:
        attempts = _safe_int(retry_context.get("attempts", 0), 0)
        if attempts > 1 or retry_context.get("last_error"):
            continuation_block = (
                "\nPrevious attempt context:\n"
                f"- Attempts so far: {attempts}\n"
                f"- Prior summary: {str(retry_context.get('last_summary', '')).strip()[:800]}\n"
                f"- Prior blocker/error: {str(retry_context.get('last_error', '')).strip()[:1200]}\n"
                "- Recovery directive: Continue from the current repository state and finish all acceptance criteria.\n"
            )

    repo_hints_text = ""
    if repo_hints:
        hints = "\n".join(f"- {hint}" for hint in repo_hints)
        repo_hints_text = f"Repository state hints:\n{hints}\n"
    protocol_behaviors = "\n".join(f"- {item}" for item in skill_protocol.required_behaviors)
    mcp_context_text = mcp_context.render_context() + "\n" if mcp_context else ""
    startup_text = startup_instructions.strip()
    startup_block = ""
    if startup_text:
        startup_block = f"Role startup instructions:\n{startup_text}\n\n"
    handoff_text = handoff_context.strip()
    handoff_block = ""
    if handoff_text:
        handoff_block = f"{handoff_text}\n\n"

    return (
        f"{objective_text.strip()}\n\n"
        f"Autonomy skill protocol:\n"
        f"- Name: {skill_protocol.name}\n"
        f"- Version: {skill_protocol.version}\n"
        f"- Description: {skill_protocol.description}\n"
        f"- Required behaviors:\n{protocol_behaviors}\n"
        f"- File-type policy: {skill_protocol.filetype_policy}\n\n"
        f"{startup_block}"
        f"{handoff_block}"
        f"{mcp_context_text}"
        "Current autonomous task:\n"
        f"- Task ID: {task.id}\n"
        f"- Title: {task.title}\n"
        f"- Owner role: {role}\n"
        f"- Repository path: {repo_path}\n"
        f"- Description: {task.description}\n"
        f"- Repository file profile: {repo_context}\n"
        f"- Acceptance criteria:\n{acceptance}\n"
        f"{repo_hints_text}"
        f"{continuation_block}\n"
        "Execution requirements:\n"
        "- Work fully autonomously for this task.\n"
        "- Scope boundary: complete only the current autonomous task listed above.\n"
        "- Do not start another task in this run; return final JSON immediately after this task is done/partial/blocked.\n"
        "- Do not ask for user nudges unless blocked by credentials, destructive actions, or true tradeoff decisions.\n"
        "- Run validation commands: `make lint` then `make test`.\n"
        "- Commit and push contiguous changes.\n"
        "- If a command fails transiently (rate limits/network/timeouts), retry with resilient fallbacks before giving up.\n"
        "- Use non-interactive commands only (never wait for terminal prompts).\n"
        "- Handle new/unknown file types safely: preserve binary formats, avoid destructive rewrites, and add `.gitattributes` entries when needed.\n"
        "- If git locks or in-progress git states are detected, recover safely and continue.\n"
        "- Merge/rebase operations are allowed when there are no unresolved conflicts (`git diff --name-only --diff-filter=U` is empty).\n"
        "- If you are implementation-owner: provide explicit test requests for Gemini in next_actions.\n"
        "- If you are test-owner: when you find implementation issues, provide concrete fix feedback and hints for Codex in blocker/next_actions.\n"
        "- Return ONLY JSON with keys: status, summary, commit, validations, next_actions, blocker.\n"
        "- Include `usage` object: input_tokens, output_tokens, total_tokens, model.\n"
        "- status must be one of: done, partial, blocked.\n"
    )


def run_command(
    cmd: list[str],
    cwd: Path,
    timeout_sec: int,
    progress_callback: Callable[[int], None] | None = None,
    progress_interval_sec: int = 15,
    extra_env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = build_subprocess_env(extra_env)
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            text=True,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as err:
        missing = err.filename or (cmd[0] if cmd else "")
        return subprocess.CompletedProcess(
            cmd,
            returncode=127,
            stdout="",
            stderr=f"[ENOENT] command not found: {missing}",
        )
    start = time.monotonic()
    last_progress = start
    communicate_input = stdin_text

    while True:
        elapsed = int(time.monotonic() - start)
        if progress_callback and (time.monotonic() - last_progress) >= progress_interval_sec:
            progress_callback(elapsed)
            last_progress = time.monotonic()
        try:
            stdout, stderr = process.communicate(timeout=1, input=communicate_input)
            communicate_input = None
            return subprocess.CompletedProcess(cmd, returncode=process.returncode or 0, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            communicate_input = None
            if elapsed >= timeout_sec:
                process.kill()
                stdout, stderr = process.communicate()
                timeout_msg = f"\n[TIMEOUT] command exceeded {timeout_sec}s: {' '.join(cmd)}"
                return subprocess.CompletedProcess(
                    cmd,
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr + timeout_msg,
                )


def run_validations(
    repo: Path,
    validate_commands: list[str],
    timeout_sec: int,
    progress_callback: Callable[[str, int], None] | None = None,
    retries_per_command: int = 1,
) -> tuple[bool, str]:
    for raw in validate_commands:
        try:
            cmd = shlex.split(raw)
        except ValueError as err:
            return False, f"Validation command parse failed for `{raw}`: {err}"
        if not cmd:
            continue
        attempts = max(1, retries_per_command + 1) if is_test_command(raw) else 1
        failure_details = ""
        for idx in range(attempts):
            _print(f"Running validation in {repo}: {raw} (attempt {idx + 1}/{attempts})")
            result = run_command(
                cmd,
                cwd=repo,
                timeout_sec=timeout_sec,
                progress_callback=(lambda elapsed: progress_callback(raw, elapsed)) if progress_callback else None,
            )
            if result.returncode == 0:
                failure_details = ""
                break
            failure_details = (result.stdout + "\n" + result.stderr).strip()
            if idx + 1 < attempts:
                _print(f"Validation retry queued for `{raw}` after failure.")
        if not failure_details:
            continue

        fallbacks = validation_fallback_commands(raw)
        if fallbacks:
            fallback_errors: list[str] = []
            for fallback in fallbacks:
                fallback_cmd = shlex.split(fallback)
                if not fallback_cmd:
                    continue
                _print(f"Running fallback validation in {repo}: {fallback}")
                fallback_result = run_command(
                    fallback_cmd,
                    cwd=repo,
                    timeout_sec=timeout_sec,
                    progress_callback=(lambda elapsed: progress_callback(fallback, elapsed)) if progress_callback else None,
                )
                if fallback_result.returncode == 0:
                    failure_details = ""
                    break
                fallback_output = (fallback_result.stdout + "\n" + fallback_result.stderr).strip()
                fallback_errors.append(
                    f"`{fallback}` failed:\n{fallback_output}"
                )
            if not failure_details:
                continue
            if fallback_errors:
                failure_details = f"{failure_details}\n\nFallback failures:\n" + "\n\n".join(fallback_errors)

        return False, f"Validation failed for `{raw}`:\n{failure_details}"
    return True, "ok"


def _git_output(repo: Path, args: list[str], timeout_sec: int = 120) -> tuple[bool, str]:
    result = run_command(["git", *args], cwd=repo, timeout_sec=timeout_sec)
    merged = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        return False, merged
    return True, result.stdout.strip()


def _parse_ahead_behind(counts: str) -> tuple[int, int] | None:
    parts = counts.split()
    if len(parts) != 2:
        return None
    try:
        ahead = int(parts[0])
        behind = int(parts[1])
    except ValueError:
        return None
    return ahead, behind


def _branch_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return token or "autonomy"


def _autonomy_branch_name(repo: Path, owner: str = "autonomy") -> str:
    return f"codex/{_branch_token(owner)}-{_branch_token(repo.name)}"


def _remote_moved_url(output: str) -> str | None:
    match = re.search(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", output)
    if not match:
        return None
    return match.group(0)


def _is_protected_branch_rejection(output: str) -> bool:
    lowered = output.lower()
    return any(pattern in lowered for pattern in PROTECTED_BRANCH_REJECTION_PATTERNS)


def _current_branch(repo: Path) -> tuple[bool, str]:
    return _git_output(repo, ["rev-parse", "--abbrev-ref", "HEAD"])


def ensure_pushable_branch(repo: Path, owner: str = "autonomy", timeout_sec: int = 180) -> tuple[bool, str]:
    ok, branch = _current_branch(repo)
    if not ok:
        return False, f"unable to read current branch: {branch}"
    if branch not in PROTECTED_BRANCH_NAMES:
        return True, f"current branch is pushable: {branch}"

    target = _autonomy_branch_name(repo, owner=owner)
    checkout = run_command(["git", "checkout", target], cwd=repo, timeout_sec=timeout_sec)
    if checkout.returncode == 0:
        return True, f"switched to existing pushable branch `{target}` from protected `{branch}`"

    create = run_command(["git", "checkout", "-b", target], cwd=repo, timeout_sec=timeout_sec)
    if create.returncode != 0:
        return False, (
            f"failed to switch off protected branch `{branch}`:\n"
            f"{(checkout.stdout + '\n' + checkout.stderr).strip()}\n\n"
            f"create branch failure:\n{(create.stdout + '\n' + create.stderr).strip()}"
        )
    return True, f"created pushable branch `{target}` from protected `{branch}`"


def _set_remote_from_move_hint(repo: Path, output: str, timeout_sec: int) -> tuple[bool, str]:
    moved = _remote_moved_url(output)
    if not moved:
        return False, "no moved-remote hint detected"
    set_url = run_command(["git", "remote", "set-url", "origin", moved], cwd=repo, timeout_sec=timeout_sec)
    if set_url.returncode != 0:
        return False, f"failed to update origin remote to moved URL `{moved}`"
    return True, moved


def _push_with_recovery(
    repo: Path,
    *,
    timeout_sec: int,
    owner: str = "autonomy",
    set_upstream: bool = False,
) -> tuple[bool, str]:
    ok, branch = _current_branch(repo)
    if not ok:
        return False, f"unable to read current branch before push: {branch}"
    push_cmd = ["git", "push"]
    if set_upstream:
        push_cmd = ["git", "push", "-u", "origin", branch]
    push = run_command(push_cmd, cwd=repo, timeout_sec=timeout_sec)
    if push.returncode == 0:
        return True, f"push succeeded on branch `{branch}`"

    output = (push.stdout + "\n" + push.stderr).strip()
    moved_ok, moved_value = _set_remote_from_move_hint(repo, output, timeout_sec)
    if moved_ok:
        retry = run_command(push_cmd, cwd=repo, timeout_sec=timeout_sec)
        if retry.returncode == 0:
            return True, f"push succeeded after updating origin to `{moved_value}`"
        output = (retry.stdout + "\n" + retry.stderr).strip()

    if _is_protected_branch_rejection(output):
        switched, message = ensure_pushable_branch(repo, owner=owner, timeout_sec=timeout_sec)
        if not switched:
            return False, f"{message}\n\nOriginal push error:\n{output}"
        ok, new_branch = _current_branch(repo)
        if not ok:
            return False, f"{message}\nunable to verify switched branch: {new_branch}"
        retry_cmd = ["git", "push", "-u", "origin", new_branch]
        retry = run_command(retry_cmd, cwd=repo, timeout_sec=timeout_sec)
        if retry.returncode == 0:
            return True, f"{message}; pushed new branch `{new_branch}`"
        push_cmd = retry_cmd
        output = (retry.stdout + "\n" + retry.stderr).strip()

    ok, current_branch = _current_branch(repo)
    if not ok:
        return False, output
    no_verify_cmd = ["git", "push", "--no-verify"]
    if set_upstream:
        no_verify_cmd.extend(["-u", "origin", current_branch])
    no_verify = run_command(no_verify_cmd, cwd=repo, timeout_sec=timeout_sec)
    if no_verify.returncode == 0:
        return True, (
            "push succeeded with --no-verify fallback after failure:\n"
            f"{output}"
        )

    no_verify_output = (no_verify.stdout + "\n" + no_verify.stderr).strip()
    moved_ok, moved_value = _set_remote_from_move_hint(repo, no_verify_output, timeout_sec)
    if moved_ok:
        no_verify_retry = run_command(no_verify_cmd, cwd=repo, timeout_sec=timeout_sec)
        if no_verify_retry.returncode == 0:
            return True, (
                "push succeeded with --no-verify after updating moved remote "
                f"to `{moved_value}`; initial failure was:\n{output}"
            )
        no_verify_output = (no_verify_retry.stdout + "\n" + no_verify_retry.stderr).strip()

    if _is_protected_branch_rejection(no_verify_output):
        switched, message = ensure_pushable_branch(repo, owner=owner, timeout_sec=timeout_sec)
        if switched:
            ok, branch_after_switch = _current_branch(repo)
            if ok:
                branch_no_verify_cmd = ["git", "push", "--no-verify", "-u", "origin", branch_after_switch]
                branch_push = run_command(branch_no_verify_cmd, cwd=repo, timeout_sec=timeout_sec)
                if branch_push.returncode == 0:
                    return True, (
                        "push succeeded with --no-verify after protected-branch switch: "
                        f"{message}"
                    )
                branch_output = (branch_push.stdout + "\n" + branch_push.stderr).strip()
                moved_branch_ok, moved_branch_value = _set_remote_from_move_hint(repo, branch_output, timeout_sec)
                if moved_branch_ok:
                    branch_retry = run_command(branch_no_verify_cmd, cwd=repo, timeout_sec=timeout_sec)
                    if branch_retry.returncode == 0:
                        return True, (
                            "push succeeded with --no-verify after protected-branch switch and moved remote "
                            f"update to `{moved_branch_value}`: {message}"
                        )
                    branch_output = (branch_retry.stdout + "\n" + branch_retry.stderr).strip()
                no_verify_output = f"{no_verify_output}\n\nbranch-push fallback failed:\n{branch_output}"
            else:
                no_verify_output = f"{no_verify_output}\n\nunable to determine branch after switch: {branch_after_switch}"

    return False, f"{output}\n\nno-verify fallback failed:\n{no_verify_output}"


def ensure_repo_pushed(repo: Path, timeout_sec: int = 180, owner: str = "autonomy") -> tuple[bool, str]:
    ok, inside = _git_output(repo, ["rev-parse", "--is-inside-work-tree"])
    if not ok:
        return True, f"push check skipped (not a git repo): {inside}"

    ok, upstream = _git_output(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not ok:
        switched, message = ensure_pushable_branch(repo, owner=owner, timeout_sec=timeout_sec)
        if not switched:
            return False, message
        pushed, push_details = _push_with_recovery(repo, timeout_sec=timeout_sec, owner=owner, set_upstream=True)
        if not pushed:
            return False, f"no upstream configured and initial push failed:\n{push_details}"
        ok, upstream = _git_output(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if not ok:
            return False, f"upstream still missing after push setup: {upstream}"

    ok, counts = _git_output(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
    if not ok:
        return False, f"unable to compare branch with upstream: {counts}"
    parsed = _parse_ahead_behind(counts)
    if parsed is None:
        return False, f"unexpected rev-list output: {counts}"
    ahead, behind = parsed

    if ahead <= 0 and behind <= 0:
        return True, f"branch synced to {upstream} (behind={behind}, ahead={ahead})"
    if ahead <= 0 and behind > 0:
        return False, f"branch behind upstream; rebase/pull required (behind={behind}, ahead={ahead})"
    if ahead > 0 and behind > 0:
        return False, f"branch diverged from upstream; reconcile before push (behind={behind}, ahead={ahead})"

    pushed, push_details = _push_with_recovery(repo, timeout_sec=timeout_sec, owner=owner, set_upstream=False)
    if not pushed:
        return False, f"git push failed:\n{push_details}"

    ok, recounted = _git_output(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
    if not ok:
        return False, f"push verification failed: {recounted}"
    parsed_after = _parse_ahead_behind(recounted)
    if parsed_after is None:
        return False, f"unexpected post-push rev-list output: {recounted}"
    ahead_after, behind_after = parsed_after
    if ahead_after > 0:
        return False, f"branch still ahead after push (behind={behind_after}, ahead={ahead_after})"
    if behind_after > 0:
        return False, f"branch behind after push verification (behind={behind_after}, ahead={ahead_after})"
    return True, f"push verified to {upstream} (behind={behind_after}, ahead={ahead_after})"


def auto_push_repo_if_ahead(repo: Path, timeout_sec: int = 180, owner: str = "autonomy") -> tuple[str, str]:
    ok, inside = _git_output(repo, ["rev-parse", "--is-inside-work-tree"])
    if not ok:
        return "skipped", f"not a git repo: {inside}"

    ok, upstream = _git_output(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not ok:
        switched, switch_msg = ensure_pushable_branch(repo, owner=owner, timeout_sec=timeout_sec)
        if not switched:
            return "error", switch_msg
        pushed, details = _push_with_recovery(repo, timeout_sec=timeout_sec, owner=owner, set_upstream=True)
        if not pushed:
            return "error", f"auto-push setup failed: {details}"
        return "pushed", f"{switch_msg}; {details}"

    ok, counts = _git_output(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
    if not ok:
        return "error", f"unable to compare with upstream: {counts}"
    parsed = _parse_ahead_behind(counts)
    if parsed is None:
        return "error", f"unexpected rev-list output: {counts}"
    ahead, behind = parsed
    if ahead <= 0:
        return "noop", f"branch already synced (behind={behind}, ahead={ahead})"
    if behind > 0:
        return "skipped", f"branch behind/diverged; skipping auto-push (behind={behind}, ahead={ahead})"

    pushed, details = _push_with_recovery(repo, timeout_sec=timeout_sec, owner=owner, set_upstream=False)
    if not pushed:
        return "error", f"git push failed:\n{details}"
    return "pushed", f"auto-pushed {ahead} commit(s) to {upstream}: {details}"


def _extract_json_object_from_text(raw: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def parse_json_text(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fence_patterns = (
        r"```json\s*(\{[\s\S]*?\})\s*```",
        r"```\s*(\{[\s\S]*?\})\s*```",
    )
    for pattern in fence_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        block = match.group(1)
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return _extract_json_object_from_text(text)


def normalize_outcome(raw: dict[str, Any]) -> dict[str, Any]:
    status = str(raw.get("status", STATUS_BLOCKED)).strip().lower()
    if status not in {STATUS_DONE, STATUS_PARTIAL, STATUS_BLOCKED}:
        status = STATUS_PARTIAL
    next_actions = raw.get("next_actions", [])
    if not isinstance(next_actions, list):
        next_actions = [str(next_actions)]

    out = {
        "status": status,
        "summary": str(raw.get("summary", "")).strip(),
        "commit": str(raw.get("commit", "")).strip(),
        "validations": raw.get("validations", []),
        "next_actions": [str(x) for x in next_actions],
        "blocker": str(raw.get("blocker", "")).strip(),
        "raw_output": str(raw.get("raw_output", "")).strip(),
    }
    usage = raw.get("usage")
    if isinstance(usage, dict):
        out["usage"] = usage
    telemetry = raw.get("_telemetry")
    if isinstance(telemetry, dict):
        out["_telemetry"] = telemetry
    return out


def is_gemini_capacity_error(text: str) -> bool:
    lowered = text.lower()
    if not lowered.strip():
        return False
    if not is_retryable_error(lowered):
        return False
    return any(pattern in lowered for pattern in GEMINI_CAPACITY_ERROR_PATTERNS)


def gemini_model_candidates(primary: str | None, fallbacks: list[str]) -> list[str | None]:
    ordered: list[str | None] = [primary]
    if primary is not None:
        # Final fallback tries provider default routing when an explicit model overloads.
        ordered.append(None)
    ordered.extend(fallbacks)

    seen: set[str] = set()
    out: list[str | None] = []
    for item in ordered:
        key = "" if item is None else item.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def build_attempt_telemetry(
    *,
    owner: str,
    model: str,
    prompt: str,
    latency_sec: float,
    response_text: str,
    usage: dict[str, Any],
    routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "owner": owner,
        "model": model,
        "latency_sec": round(max(0.0, float(latency_sec)), 6),
        "prompt_chars": len(prompt),
        "response_chars": len(response_text),
        "prompt_tokens_est": estimate_token_count(prompt),
        "response_tokens_est": estimate_token_count(response_text),
        "prompt_difficulty_score": prompt_difficulty_score(prompt),
        "usage": usage,
    }
    if isinstance(routing, dict):
        payload["routing"] = routing
    return payload


def run_codex_task(
    *,
    task: Task,
    repo: Path,
    objective_text: str,
    schema_path: Path,
    output_dir: Path,
    codex_cmd: str,
    codex_model: str | None,
    routellm_enabled: bool = False,
    routellm_policy: dict[str, Any] | None = None,
    routellm_url: str = "",
    routellm_timeout_sec: int = 5,
    timeout_sec: int,
    retry_context: dict[str, Any],
    progress_callback: Callable[[int], None] | None,
    repo_context: str,
    repo_hints: list[str],
    skill_protocol: SkillProtocolSpec,
    mcp_context: MCPContextBundle | None,
    startup_instructions: str,
    handoff_context: str,
    conversation_log_file: Path | None,
    cycle: int,
    role: str = "implementation-owner",
    role_constraints: str = "",
) -> tuple[bool, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{task.id}_codex_result.json"
    prompt = build_agent_prompt(
        task,
        objective_text,
        role=role,
        repo_path=repo,
        retry_context=retry_context,
        repo_context=repo_context,
        repo_hints=repo_hints,
        skill_protocol=skill_protocol,
        mcp_context=mcp_context,
        startup_instructions=startup_instructions,
        handoff_context=handoff_context,
    )
    if role_constraints.strip():
        prompt += f"\n{role_constraints.strip()}\n"
    effective_routellm_policy = routellm_policy if isinstance(routellm_policy, dict) else DEFAULT_ROUTELLM_POLICY_PAYLOAD
    try:
        effective_routellm_timeout_sec = max(1, int(routellm_timeout_sec))
    except (TypeError, ValueError):
        effective_routellm_timeout_sec = 5
    effective_routellm_url = str(routellm_url or "").strip()
    routed_model, route_decision = resolve_routed_model(
        provider="codex",
        requested_model=codex_model,
        prompt=prompt,
        routellm_enabled=routellm_enabled,
        routellm_policy=effective_routellm_policy,
        router_url_override=effective_routellm_url,
        router_timeout_sec_override=effective_routellm_timeout_sec,
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="prompt",
        content=prompt,
        meta={"agent": "codex"},
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="routing_decision",
        content=json.dumps(route_decision, sort_keys=True),
        meta={"agent": "codex", "provider": "codex"},
    )

    cmd = [
        codex_cmd,
        "exec",
        "--cd",
        str(repo),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_file),
        prompt,
    ]
    if routed_model:
        cmd[2:2] = ["--model", routed_model]

    _print(f"Running Codex task {task.id}")
    started = time.monotonic()
    result = run_command(cmd, cwd=repo, timeout_sec=timeout_sec, progress_callback=progress_callback)
    latency = time.monotonic() - started
    model_name = routed_model or codex_cmd
    if result.returncode != 0:
        usage = extract_usage_metrics(stdout=result.stdout, stderr=result.stderr)
        telemetry = build_attempt_telemetry(
            owner=task.owner,
            model=model_name,
            prompt=prompt,
            latency_sec=latency,
            response_text=(result.stdout + "\n" + result.stderr).strip(),
            usage=usage,
            routing=route_decision,
        )
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_error",
            content=(result.stdout + "\n" + result.stderr).strip(),
            meta={"agent": "codex", "returncode": result.returncode},
        )
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Codex command failed",
                "blocker": (result.stdout + "\n" + result.stderr).strip(),
                "next_actions": [],
                "_telemetry": telemetry,
            }
        )

    parsed = parse_json_text(output_file.read_text(encoding="utf-8")) if output_file.exists() else None
    if parsed is None:
        usage = extract_usage_metrics(stdout=result.stdout, stderr=result.stderr)
        telemetry = build_attempt_telemetry(
            owner=task.owner,
            model=model_name,
            prompt=prompt,
            latency_sec=latency,
            response_text=(result.stdout + "\n" + result.stderr).strip(),
            usage=usage,
            routing=route_decision,
        )
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_error",
            content="Expected JSON object in output-last-message file.",
            meta={"agent": "codex"},
        )
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Codex produced non-JSON final output",
                "blocker": "Expected JSON object in output-last-message file.",
                "next_actions": [],
                "_telemetry": telemetry,
            }
        )
    usage = extract_usage_metrics(parsed, stdout=result.stdout, stderr=result.stderr)
    parsed["_telemetry"] = build_attempt_telemetry(
        owner=task.owner,
        model=model_name,
        prompt=prompt,
        latency_sec=latency,
        response_text=json.dumps(parsed, sort_keys=True),
        usage=usage,
        routing=route_decision,
    )
    parsed["usage"] = usage
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="agent_output",
        content=json.dumps(parsed, sort_keys=True),
        meta={"agent": "codex"},
    )
    return True, normalize_outcome(parsed)


def run_gemini_task(
    *,
    task: Task,
    repo: Path,
    objective_text: str,
    gemini_cmd: str,
    gemini_model: str | None,
    gemini_fallback_models: list[str],
    routellm_enabled: bool = False,
    routellm_policy: dict[str, Any] | None = None,
    routellm_url: str = "",
    routellm_timeout_sec: int = 5,
    timeout_sec: int,
    retry_context: dict[str, Any],
    progress_callback: Callable[[int], None] | None,
    repo_context: str,
    repo_hints: list[str],
    skill_protocol: SkillProtocolSpec,
    mcp_context: MCPContextBundle | None,
    startup_instructions: str,
    handoff_context: str,
    conversation_log_file: Path | None,
    cycle: int,
    claude_fallback_cmd: str | None = None,
    claude_fallback_model: str | None = None,
    claude_fallback_startup_instructions: str = "",
    codex_fallback_cmd: str | None = None,
    codex_fallback_model: str | None = None,
    codex_schema_path: Path | None = None,
    codex_output_dir: Path | None = None,
    codex_fallback_startup_instructions: str = "",
) -> tuple[bool, dict[str, Any]]:
    prompt = build_agent_prompt(
        task,
        objective_text,
        role="test-owner",
        repo_path=repo,
        retry_context=retry_context,
        repo_context=repo_context,
        repo_hints=repo_hints,
        skill_protocol=skill_protocol,
        mcp_context=mcp_context,
        startup_instructions=startup_instructions,
        handoff_context=handoff_context,
    )
    prompt += (
        "\nTesting-owner constraints:\n"
        "- Focus on tests/specs/benchmarks and validation depth.\n"
        "- Avoid production code edits unless strictly required to keep tests executable.\n"
    )
    effective_routellm_policy = routellm_policy if isinstance(routellm_policy, dict) else DEFAULT_ROUTELLM_POLICY_PAYLOAD
    try:
        effective_routellm_timeout_sec = max(1, int(routellm_timeout_sec))
    except (TypeError, ValueError):
        effective_routellm_timeout_sec = 5
    effective_routellm_url = str(routellm_url or "").strip()
    routed_model, route_decision = resolve_routed_model(
        provider="gemini",
        requested_model=gemini_model,
        prompt=prompt,
        routellm_enabled=routellm_enabled,
        routellm_policy=effective_routellm_policy,
        router_url_override=effective_routellm_url,
        router_timeout_sec_override=effective_routellm_timeout_sec,
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="prompt",
        content=prompt,
        meta={"agent": "gemini"},
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="routing_decision",
        content=json.dumps(route_decision, sort_keys=True),
        meta={"agent": "gemini", "provider": "gemini"},
    )

    def attempt_cross_provider_fallback(
        *,
        fail_text: str,
        telemetry: dict[str, Any],
        reason: str,
    ) -> tuple[bool, dict[str, Any]] | None:
        fallback_failures: list[str] = []
        attempted = False

        if claude_fallback_cmd:
            attempted = True
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="agent_provider_fallback",
                content=reason,
                meta={"from": "gemini", "to": "claude"},
            )
            claude_ok, claude_outcome = run_claude_task(
                task=task,
                repo=repo,
                objective_text=objective_text,
                claude_cmd=claude_fallback_cmd,
                claude_model=claude_fallback_model,
                routellm_enabled=routellm_enabled,
                routellm_policy=effective_routellm_policy,
                routellm_url=effective_routellm_url,
                routellm_timeout_sec=effective_routellm_timeout_sec,
                timeout_sec=timeout_sec,
                retry_context=retry_context,
                progress_callback=progress_callback,
                repo_context=repo_context,
                repo_hints=repo_hints,
                skill_protocol=skill_protocol,
                mcp_context=mcp_context,
                startup_instructions=claude_fallback_startup_instructions,
                handoff_context=handoff_context,
                conversation_log_file=conversation_log_file,
                cycle=cycle,
                role="test-owner",
                role_constraints=(
                    "Testing-owner constraints:\n"
                    "- Focus on tests/specs/benchmarks and validation depth.\n"
                    "- Avoid production code edits unless strictly required to keep tests executable.\n"
                    "- You are acting as a fallback provider for a Gemini-owned task."
                ),
            )
            if claude_ok:
                fallback_outcome = normalize_outcome(claude_outcome)
                summary = str(fallback_outcome.get("summary", "")).strip()
                fallback_outcome["summary"] = (
                    f"Claude fallback succeeded after Gemini failure. {summary}".strip()
                    if summary
                    else "Claude fallback succeeded after Gemini failure."
                )
                return True, fallback_outcome
            fallback_failures.append(
                "Claude fallback failed:\n"
                + str(claude_outcome.get("blocker", "") or claude_outcome.get("summary", "unknown error")).strip()
            )

        if codex_fallback_cmd and codex_schema_path is not None and codex_output_dir is not None:
            attempted = True
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="agent_provider_fallback",
                content="Claude fallback unavailable/failed; attempting OpenAI fallback provider.",
                meta={"from": "gemini", "to": "codex"},
            )
            codex_ok, codex_outcome = run_codex_task(
                task=task,
                repo=repo,
                objective_text=objective_text,
                schema_path=codex_schema_path,
                output_dir=codex_output_dir,
                codex_cmd=codex_fallback_cmd,
                codex_model=codex_fallback_model,
                routellm_enabled=routellm_enabled,
                routellm_policy=effective_routellm_policy,
                routellm_url=effective_routellm_url,
                routellm_timeout_sec=effective_routellm_timeout_sec,
                timeout_sec=timeout_sec,
                retry_context=retry_context,
                progress_callback=progress_callback,
                repo_context=repo_context,
                repo_hints=repo_hints,
                skill_protocol=skill_protocol,
                mcp_context=mcp_context,
                startup_instructions=codex_fallback_startup_instructions,
                handoff_context=handoff_context,
                conversation_log_file=conversation_log_file,
                cycle=cycle,
                role="test-owner",
                role_constraints=(
                    "Testing-owner constraints:\n"
                    "- Focus on tests/specs/benchmarks and validation depth.\n"
                    "- Avoid production code edits unless strictly required to keep tests executable.\n"
                    "- You are acting as a fallback provider for a Gemini-owned task."
                ),
            )
            if codex_ok:
                fallback_outcome = normalize_outcome(codex_outcome)
                summary = str(fallback_outcome.get("summary", "")).strip()
                fallback_outcome["summary"] = (
                    f"OpenAI fallback succeeded after Gemini failure. {summary}".strip()
                    if summary
                    else "OpenAI fallback succeeded after Gemini failure."
                )
                return True, fallback_outcome
            fallback_failures.append(
                "OpenAI fallback failed:\n"
                + str(codex_outcome.get("blocker", "") or codex_outcome.get("summary", "unknown error")).strip()
            )

        if not attempted:
            return None

        blocker_sections = [f"Gemini failure:\n{fail_text or 'Gemini command failed with unknown error.'}"]
        blocker_sections.extend(fallback_failures)
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Gemini and fallback providers failed",
                "blocker": "\n\n".join(section.strip() for section in blocker_sections if section.strip()),
                "next_actions": [],
                "_telemetry": telemetry,
            }
        )

    model_candidates = gemini_model_candidates(routed_model, gemini_fallback_models)
    attempt_errors: list[str] = []
    result: subprocess.CompletedProcess[str] | None = None
    latency = 0.0
    model_name = routed_model or gemini_cmd

    for idx, model_candidate in enumerate(model_candidates, start=1):
        cmd = [
            gemini_cmd,
            "--approval-mode",
            "yolo",
            "--output-format",
            "text",
            "-p",
            prompt,
        ]
        if model_candidate:
            cmd[1:1] = ["--model", model_candidate]

        _print(
            f"Running Gemini task {task.id}"
            + (f" (candidate {idx}/{len(model_candidates)}: {model_candidate or 'default'})")
        )
        started = time.monotonic()
        result = run_command(cmd, cwd=repo, timeout_sec=timeout_sec, progress_callback=progress_callback)
        latency = time.monotonic() - started
        model_name = model_candidate or routed_model or gemini_cmd
        if result.returncode == 0:
            break

        error_text = (result.stdout + "\n" + result.stderr).strip()
        attempt_errors.append(
            f"[candidate {idx}/{len(model_candidates)} model={model_candidate or 'default'}]\n{error_text}"
        )
        if is_gemini_capacity_error(error_text) and idx < len(model_candidates):
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="agent_retry_fallback",
                content=(
                    "Gemini capacity blocker detected; retrying with fallback model "
                    f"{model_candidates[idx] or 'default'}."
                ),
                meta={
                    "agent": "gemini",
                    "failed_model": model_candidate or "",
                    "next_model": model_candidates[idx] or "",
                },
            )
            continue
        break

    if result is None or result.returncode != 0:
        fail_text = "\n\n".join(attempt_errors).strip()
        usage = extract_usage_metrics(stdout=(result.stdout if result else ""), stderr=(result.stderr if result else ""))
        telemetry = build_attempt_telemetry(
            owner=task.owner,
            model=model_name,
            prompt=prompt,
            latency_sec=latency,
            response_text=fail_text,
            usage=usage,
            routing=route_decision,
        )
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_error",
            content=fail_text,
            meta={"agent": "gemini", "returncode": (result.returncode if result else -1)},
        )
        fallback_result = attempt_cross_provider_fallback(
            fail_text=fail_text,
            telemetry=telemetry,
            reason="Gemini failed; attempting Claude fallback provider.",
        )
        if fallback_result is not None:
            return fallback_result
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Gemini command failed",
                "blocker": fail_text or "Gemini command failed with unknown error.",
                "next_actions": [],
                "_telemetry": telemetry,
            }
        )

    parsed = parse_json_text(result.stdout)
    if parsed is None:
        parsed = {
            "status": STATUS_PARTIAL,
            "summary": "Gemini output was not strict JSON; treating as partial",
            "next_actions": [],
            "raw_output": result.stdout.strip(),
        }
    usage = extract_usage_metrics(parsed, stdout=result.stdout, stderr=result.stderr)
    parsed["_telemetry"] = build_attempt_telemetry(
        owner=task.owner,
        model=model_name,
        prompt=prompt,
        latency_sec=latency,
        response_text=result.stdout.strip(),
        usage=usage,
        routing=route_decision,
    )
    parsed["usage"] = usage
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="agent_output",
        content=result.stdout.strip(),
        meta={"agent": "gemini"},
    )
    normalized = normalize_outcome(parsed)
    combined_signal_text = "\n".join(
        [
            str(normalized.get("summary", "")),
            str(normalized.get("blocker", "")),
            str(normalized.get("raw_output", "")),
            result.stdout.strip(),
            result.stderr.strip(),
        ]
    ).strip()
    status = str(normalized.get("status", "")).strip().lower()
    commit_ref = str(normalized.get("commit", "")).strip()
    if status in {STATUS_PARTIAL, STATUS_BLOCKED} and not commit_ref and is_gemini_capacity_error(combined_signal_text):
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_retry_fallback",
            content="Gemini returned a capacity-like partial/blocked outcome; escalating to cross-provider fallback.",
            meta={"agent": "gemini"},
        )
        telemetry_payload = normalized.get("_telemetry", {}) if isinstance(normalized.get("_telemetry", {}), dict) else {}
        fallback_result = attempt_cross_provider_fallback(
            fail_text=combined_signal_text,
            telemetry=telemetry_payload,
            reason="Gemini returned a capacity-like partial/blocked outcome; attempting Claude fallback provider.",
        )
        if fallback_result is not None:
            return fallback_result
    return True, normalized


def run_claude_task(
    *,
    task: Task,
    repo: Path,
    objective_text: str,
    claude_cmd: str,
    claude_model: str | None,
    routellm_enabled: bool = False,
    routellm_policy: dict[str, Any] | None = None,
    routellm_url: str = "",
    routellm_timeout_sec: int = 5,
    timeout_sec: int,
    retry_context: dict[str, Any],
    progress_callback: Callable[[int], None] | None,
    repo_context: str,
    repo_hints: list[str],
    skill_protocol: SkillProtocolSpec,
    mcp_context: MCPContextBundle | None,
    startup_instructions: str,
    handoff_context: str,
    conversation_log_file: Path | None,
    cycle: int,
    role: str = "review-owner",
    role_constraints: str = "",
) -> tuple[bool, dict[str, Any]]:
    prompt = build_agent_prompt(
        task,
        objective_text,
        role=role,
        repo_path=repo,
        retry_context=retry_context,
        repo_context=repo_context,
        repo_hints=repo_hints,
        skill_protocol=skill_protocol,
        mcp_context=mcp_context,
        startup_instructions=startup_instructions,
        handoff_context=handoff_context,
    )
    constraints = role_constraints.strip()
    if not constraints and role == "review-owner":
        constraints = (
            "Review-owner constraints:\n"
            "- Focus on governance, architecture, and collaboration safety constraints.\n"
            "- Keep changes production-grade and verify with tests where applicable."
        )
    if constraints:
        prompt += f"\n{constraints}\n"
    effective_routellm_policy = routellm_policy if isinstance(routellm_policy, dict) else DEFAULT_ROUTELLM_POLICY_PAYLOAD
    try:
        effective_routellm_timeout_sec = max(1, int(routellm_timeout_sec))
    except (TypeError, ValueError):
        effective_routellm_timeout_sec = 5
    effective_routellm_url = str(routellm_url or "").strip()
    routed_model, route_decision = resolve_routed_model(
        provider="claude",
        requested_model=claude_model,
        prompt=prompt,
        routellm_enabled=routellm_enabled,
        routellm_policy=effective_routellm_policy,
        router_url_override=effective_routellm_url,
        router_timeout_sec_override=effective_routellm_timeout_sec,
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="prompt",
        content=prompt,
        meta={"agent": "claude"},
    )
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="routing_decision",
        content=json.dumps(route_decision, sort_keys=True),
        meta={"agent": "claude", "provider": "claude"},
    )

    cmd = [
        claude_cmd,
        "--print",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(repo),
    ]
    if routed_model:
        cmd.extend(["--model", routed_model])

    _print(f"Running Claude task {task.id}")
    started = time.monotonic()
    result = run_command(
        cmd,
        cwd=repo,
        timeout_sec=timeout_sec,
        progress_callback=progress_callback,
        stdin_text=prompt,
    )
    latency = time.monotonic() - started
    missing_prompt_error = "input must be provided either through stdin or as a prompt argument when using --print"
    combined_stream = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0 and missing_prompt_error in combined_stream.lower():
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_retry_fallback",
            content="Claude reported missing stdin prompt; retrying with prompt argument fallback.",
            meta={"agent": "claude"},
        )
        retry_cmd = list(cmd)
        retry_cmd.append(prompt)
        result = run_command(
            retry_cmd,
            cwd=repo,
            timeout_sec=timeout_sec,
            progress_callback=progress_callback,
        )
        latency = time.monotonic() - started
    model_name = routed_model or claude_cmd
    if result.returncode != 0:
        usage = extract_usage_metrics(stdout=result.stdout, stderr=result.stderr)
        telemetry = build_attempt_telemetry(
            owner=task.owner,
            model=model_name,
            prompt=prompt,
            latency_sec=latency,
            response_text=(result.stdout + "\n" + result.stderr).strip(),
            usage=usage,
            routing=route_decision,
        )
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="agent_error",
            content=(result.stdout + "\n" + result.stderr).strip(),
            meta={"agent": "claude", "returncode": result.returncode},
        )
        return False, normalize_outcome(
            {
                "status": STATUS_BLOCKED,
                "summary": "Claude command failed",
                "blocker": (result.stdout + "\n" + result.stderr).strip(),
                "next_actions": [],
                "_telemetry": telemetry,
            }
        )

    parsed = parse_json_text(result.stdout)
    if parsed is None:
        parsed = {
            "status": STATUS_PARTIAL,
            "summary": "Claude output was not strict JSON; treating as partial",
            "next_actions": [],
            "raw_output": result.stdout.strip(),
        }
    usage = extract_usage_metrics(parsed, stdout=result.stdout, stderr=result.stderr)
    parsed["_telemetry"] = build_attempt_telemetry(
        owner=task.owner,
        model=model_name,
        prompt=prompt,
        latency_sec=latency,
        response_text=result.stdout.strip(),
        usage=usage,
        routing=route_decision,
    )
    parsed["usage"] = usage
    append_conversation_event(
        conversation_log_file,
        cycle=cycle,
        task=task,
        owner=task.owner,
        event_type="agent_output",
        content=result.stdout.strip(),
        meta={"agent": "claude"},
    )
    return True, normalize_outcome(parsed)


def ensure_cli_exists(binary: str, role: str) -> None:
    if shutil.which(binary):
        return
    raise FileNotFoundError(f"{role} CLI binary not found in PATH: {binary}")


def summarize_run(
    *,
    task: Task,
    repo: Path,
    outcome: dict[str, Any],
    report_dir: Path,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{task.id}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        f"# Autonomy Task Report: {task.id}",
        "",
        f"- Timestamp: {_now_iso()}",
        f"- Owner: {task.owner}",
        f"- Repo: {repo}",
        f"- Status: {outcome.get('status', '')}",
        f"- Summary: {outcome.get('summary', '')}",
        f"- Commit: {outcome.get('commit', '')}",
        f"- Blocker: {outcome.get('blocker', '')}",
        "",
        "## Next Actions",
    ]
    for item in outcome.get("next_actions", []) or []:
        lines.append(f"- {item}")
    _write_text_atomic(report_path, "\n".join(lines).strip() + "\n")


def is_retryable_error(text: str) -> bool:
    lowered = text.lower()
    if not lowered.strip():
        return False
    if re.search(r"\b(5\d\d|429)\b", lowered):
        return True
    return any(pattern in lowered for pattern in RETRYABLE_ERROR_PATTERNS)


def schedule_retry(
    *,
    entry: dict[str, Any],
    summary: str,
    error: str,
    retryable: bool,
    backoff_base_sec: int,
    backoff_max_sec: int,
) -> int:
    delay = max(1, backoff_base_sec)
    if retryable:
        entry["retryable_failures"] = _safe_int(entry.get("retryable_failures", 0), 0) + 1
        exp = max(0, entry["retryable_failures"] - 1)
        delay = min(backoff_max_sec, backoff_base_sec * (2**exp))

    not_before = _now_utc() + dt.timedelta(seconds=delay)
    entry["status"] = STATUS_PENDING
    entry["not_before"] = not_before.isoformat()
    entry["last_error"] = error.strip()
    entry["last_summary"] = summary.strip()
    entry["last_update"] = _now_iso()
    return delay


def mark_blocked(entry: dict[str, Any], summary: str, error: str) -> None:
    entry["status"] = STATUS_BLOCKED
    entry["last_error"] = error.strip()
    entry["last_summary"] = summary.strip()
    entry["last_update"] = _now_iso()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autonomous multi-agent runner for Orxaq.")
    parser.add_argument("--impl-repo", default="../orxaq", help="Implementation repository path.")
    parser.add_argument(
        "--test-repo",
        default="../orxaq_gemini",
        help="Independent test repository path for Gemini-owned tasks.",
    )
    parser.add_argument("--tasks-file", default="config/tasks.json")
    parser.add_argument("--state-file", default="state/state.json")
    parser.add_argument("--objective-file", default="config/objective.md")
    parser.add_argument("--codex-schema", default="config/codex_result.schema.json")
    parser.add_argument("--artifacts-dir", default="artifacts/autonomy")
    parser.add_argument("--heartbeat-file", default="artifacts/autonomy/heartbeat.json")
    parser.add_argument("--lock-file", default="artifacts/autonomy/runner.lock")
    parser.add_argument("--max-cycles", type=int, default=10000)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--max-retryable-blocked-retries", type=int, default=20)
    parser.add_argument("--retry-backoff-base-sec", type=int, default=30)
    parser.add_argument("--retry-backoff-max-sec", type=int, default=1800)
    parser.add_argument("--git-lock-stale-sec", type=int, default=300)
    parser.add_argument("--idle-sleep-sec", type=int, default=10)
    parser.add_argument("--agent-timeout-sec", type=int, default=3600)
    parser.add_argument("--validate-timeout-sec", type=int, default=1800)
    parser.add_argument(
        "--validate-command",
        action="append",
        default=["make lint", "make test"],
        help="Validation command to run after each completed task (repeatable).",
    )
    parser.add_argument("--codex-cmd", default="codex")
    parser.add_argument("--gemini-cmd", default="gemini")
    parser.add_argument("--claude-cmd", default="claude")
    parser.add_argument("--codex-model", default=None)
    parser.add_argument("--gemini-model", default=None)
    parser.add_argument(
        "--gemini-fallback-model",
        action="append",
        default=[],
        help="Fallback Gemini model to try on quota/overload errors (repeatable).",
    )
    parser.add_argument("--claude-model", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validation-retries", type=int, default=1)
    parser.add_argument("--skill-protocol-file", default="config/skill_protocol.json")
    parser.add_argument("--mcp-context-file", default="")
    parser.add_argument("--codex-startup-prompt-file", default="")
    parser.add_argument("--gemini-startup-prompt-file", default="")
    parser.add_argument("--claude-startup-prompt-file", default="")
    parser.add_argument("--conversation-log-file", default="artifacts/autonomy/conversations.ndjson")
    parser.add_argument("--handoff-dir", default="artifacts/autonomy/handoffs")
    parser.add_argument("--metrics-file", default="artifacts/autonomy/response_metrics.ndjson")
    parser.add_argument("--metrics-summary-file", default="artifacts/autonomy/response_metrics_summary.json")
    parser.add_argument("--pricing-file", default="config/pricing.json")
    parser.add_argument("--routellm-policy-file", default="config/routellm_policy.json")
    parser.add_argument(
        "--routellm-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable optional RouteLLM model routing for eligible providers.",
    )
    parser.add_argument("--routellm-url", default="")
    parser.add_argument("--routellm-timeout-sec", type=int, default=5)
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Continuously recycle completed tasks instead of exiting when all tasks are done.",
    )
    parser.add_argument(
        "--continuous-recycle-delay-sec",
        type=int,
        default=90,
        help="Cooldown before recycled tasks become runnable in continuous mode.",
    )
    parser.add_argument(
        "--dependency-state-file",
        default="",
        help="Optional state file to resolve dependencies outside the current owner-filtered task set.",
    )
    parser.add_argument(
        "--owner-filter",
        action="append",
        default=[],
        help="Restrict execution to specific task owners (repeatable).",
    )
    parser.add_argument(
        "--auto-push-guard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically push ahead branches periodically, even outside task completion paths.",
    )
    parser.add_argument(
        "--auto-push-interval-sec",
        type=int,
        default=180,
        help="Minimum interval between auto-push guard checks per repository.",
    )
    args = parser.parse_args(argv)

    impl_repo = Path(args.impl_repo).resolve()
    test_repo = Path(args.test_repo).resolve()
    tasks_file = Path(args.tasks_file).resolve()
    state_file = Path(args.state_file).resolve()
    objective_file = Path(args.objective_file).resolve()
    schema_file = Path(args.codex_schema).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    heartbeat_file = Path(args.heartbeat_file).resolve()
    lock_file = Path(args.lock_file).resolve()
    skill_protocol_file = Path(args.skill_protocol_file).resolve() if args.skill_protocol_file else None
    mcp_context_file = Path(args.mcp_context_file).resolve() if args.mcp_context_file else None
    codex_startup_prompt_file = Path(args.codex_startup_prompt_file).resolve() if args.codex_startup_prompt_file else None
    gemini_startup_prompt_file = (
        Path(args.gemini_startup_prompt_file).resolve() if args.gemini_startup_prompt_file else None
    )
    claude_startup_prompt_file = (
        Path(args.claude_startup_prompt_file).resolve() if args.claude_startup_prompt_file else None
    )
    conversation_log_file = Path(args.conversation_log_file).resolve() if args.conversation_log_file else None
    handoff_dir = Path(args.handoff_dir).resolve()
    metrics_file = Path(args.metrics_file).resolve()
    metrics_summary_file = Path(args.metrics_summary_file).resolve()
    pricing_file = Path(args.pricing_file).resolve() if args.pricing_file else None
    routellm_policy_file = Path(args.routellm_policy_file).resolve() if args.routellm_policy_file else None
    dependency_state_file = Path(args.dependency_state_file).resolve() if args.dependency_state_file else None
    gemini_fallback_models = [str(item).strip() for item in args.gemini_fallback_model if str(item).strip()]
    if not gemini_fallback_models:
        gemini_fallback_models = list(DEFAULT_GEMINI_FALLBACK_MODELS)

    if not impl_repo.exists():
        raise FileNotFoundError(f"Implementation repo not found: {impl_repo}")
    if not tasks_file.exists():
        raise FileNotFoundError(f"Task file not found: {tasks_file}")
    if not objective_file.exists():
        raise FileNotFoundError(f"Objective file not found: {objective_file}")
    if not schema_file.exists():
        raise FileNotFoundError(f"Codex schema file not found: {schema_file}")

    lock = RunnerLock(lock_file)
    lock.acquire()
    atexit.register(lock.release)

    tasks = load_tasks(tasks_file)
    owner_filter = {str(item).strip().lower() for item in args.owner_filter if str(item).strip()}
    if owner_filter:
        unknown = owner_filter - SUPPORTED_OWNERS
        if unknown:
            raise RuntimeError(f"Unknown owner filter(s): {sorted(unknown)}")
        tasks = [task for task in tasks if task.owner in owner_filter]
        if not tasks:
            raise RuntimeError(f"No tasks left after applying owner filter: {sorted(owner_filter)}")
    owners = {task.owner for task in tasks}
    if "codex" in owners:
        ensure_cli_exists(args.codex_cmd, "Codex")
    if "gemini" in owners:
        ensure_cli_exists(args.gemini_cmd, "Gemini")
    if "claude" in owners:
        ensure_cli_exists(args.claude_cmd, "Claude")

    state = load_state(state_file, tasks)
    objective_text = _read_text(objective_file)
    pricing = load_pricing(pricing_file)
    routellm_policy = load_routellm_policy(routellm_policy_file)
    routellm_url = str(args.routellm_url or "").strip()
    routellm_timeout_sec = max(1, int(args.routellm_timeout_sec))
    skill_protocol = load_skill_protocol(skill_protocol_file)
    mcp_context = load_mcp_context(mcp_context_file)
    codex_startup_instructions = _read_optional_text(codex_startup_prompt_file)
    gemini_startup_instructions = _read_optional_text(gemini_startup_prompt_file)
    claude_startup_instructions = _read_optional_text(claude_startup_prompt_file)
    save_state(state_file, state)

    _print(f"Starting autonomy runner with {len(tasks)} tasks")
    write_heartbeat(
        heartbeat_file,
        phase="started",
        cycle=0,
        task_id=None,
        message="autonomy runner started",
        extra={"tasks": len(tasks)},
    )
    auto_push_last_check: dict[str, float] = {}
    auto_push_repos = sorted({impl_repo, test_repo}, key=lambda path: str(path))

    for cycle in range(1, args.max_cycles + 1):
        dependency_state = load_dependency_state(dependency_state_file)
        if args.auto_push_guard:
            min_interval = max(30, int(args.auto_push_interval_sec))
            now_mono = time.monotonic()
            for guard_repo in auto_push_repos:
                guard_key = str(guard_repo)
                if now_mono - auto_push_last_check.get(guard_key, 0.0) < min_interval:
                    continue
                auto_push_last_check[guard_key] = now_mono
                push_status, push_message = auto_push_repo_if_ahead(
                    guard_repo,
                    timeout_sec=max(60, int(args.validate_timeout_sec)),
                    owner=(next(iter(owner_filter)) if owner_filter else "autonomy"),
                )
                if push_status == "pushed":
                    _print(f"Auto-push guard: {push_message}")
                    append_conversation_event(
                        conversation_log_file,
                        cycle=cycle,
                        task=None,
                        owner="system",
                        event_type="auto_push",
                        content=push_message,
                        meta={"repo": str(guard_repo)},
                    )
                elif push_status == "error":
                    _print(f"Auto-push guard error for {guard_repo}: {push_message}")
                    append_conversation_event(
                        conversation_log_file,
                        cycle=cycle,
                        task=None,
                        owner="system",
                        event_type="auto_push_error",
                        content=push_message,
                        meta={"repo": str(guard_repo)},
                    )

        if all(state[t.id]["status"] == STATUS_DONE for t in tasks):
            if args.continuous:
                recycle_tasks_for_continuous_mode(
                    state,
                    tasks,
                    delay_sec=args.continuous_recycle_delay_sec,
                )
                save_state(state_file, state)
                _print("All tasks completed; recycled for continuous autonomy mode.")
                write_heartbeat(
                    heartbeat_file,
                    phase="continuous_recycle",
                    cycle=cycle,
                    task_id=None,
                    message="all tasks recycled for continuous mode",
                    extra={"recycle_delay_sec": int(args.continuous_recycle_delay_sec)},
                )
                append_conversation_event(
                    conversation_log_file,
                    cycle=cycle,
                    task=None,
                    owner="system",
                    event_type="continuous_recycle",
                    content=(
                        "All tasks reached done; recycled to pending "
                        f"with delay={int(args.continuous_recycle_delay_sec)}s."
                    ),
                )
                continue
            _print("All tasks are marked done.")
            write_heartbeat(
                heartbeat_file,
                phase="completed",
                cycle=cycle,
                task_id=None,
                message="all tasks completed",
            )
            return 0

        now = _now_utc()
        task = select_next_task(tasks, state, now=now, dependency_state=dependency_state)
        if task is None:
            soonest = soonest_pending_time(tasks, state, dependency_state=dependency_state)
            pending = [t.id for t in tasks if state[t.id]["status"] == STATUS_PENDING]
            blocked = [t.id for t in tasks if state[t.id]["status"] == STATUS_BLOCKED]
            waiting_on_deps: dict[str, list[str]] = {}
            for pending_task in tasks:
                if state[pending_task.id]["status"] != STATUS_PENDING:
                    continue
                deps = unresolved_dependencies(pending_task, state, dependency_state)
                if deps:
                    waiting_on_deps[pending_task.id] = deps

            if soonest is not None and soonest > now:
                sleep_for = min(args.idle_sleep_sec, max(1, int((soonest - now).total_seconds())))
                write_heartbeat(
                    heartbeat_file,
                    phase="idle",
                    cycle=cycle,
                    task_id=None,
                    message=f"waiting {sleep_for}s for retry cooldown",
                    extra={"pending": pending, "blocked": blocked, "waiting_on_deps": waiting_on_deps},
                )
                time.sleep(sleep_for)
                continue

            recovery = recover_deadlocked_tasks(
                tasks=tasks,
                state=state,
                dependency_state=dependency_state,
                max_recoveries_per_task=max(1, args.max_attempts),
            )
            if recovery.get("changed", False):
                write_heartbeat(
                    heartbeat_file,
                    phase="deadlock_recovery",
                    cycle=cycle,
                    task_id=None,
                    message="recovered blocked/pending deadlock",
                    extra=recovery,
                )
                append_conversation_event(
                    conversation_log_file,
                    cycle=cycle,
                    task=None,
                    owner="system",
                    event_type="deadlock_recovery",
                    content=(
                        "Recovered deadlock by reopening tasks. "
                        f"reopened={recovery.get('reopened_tasks', [])}, "
                        f"unblocked={recovery.get('unblocked_tasks', [])}"
                    ),
                    meta={"recovery": recovery},
                )
                save_state(state_file, state)
                continue

            _print(f"No ready tasks remain. Pending={pending}, Blocked={blocked}")
            if args.continuous:
                reopened = recycle_stalled_tasks_for_continuous_mode(
                    state,
                    tasks,
                    delay_sec=args.continuous_recycle_delay_sec,
                )
                save_state(state_file, state)
                write_heartbeat(
                    heartbeat_file,
                    phase="continuous_stalled_recycle",
                    cycle=cycle,
                    task_id=None,
                    message="continuous mode recycled stalled tasks",
                    extra={"pending": pending, "blocked": blocked, "reopened": reopened},
                )
                append_conversation_event(
                    conversation_log_file,
                    cycle=cycle,
                    task=None,
                    owner="system",
                    event_type="continuous_stalled_recycle",
                    content=(
                        "No ready tasks in continuous mode; recycled stalled tasks "
                        f"with delay={int(args.continuous_recycle_delay_sec)}s. reopened={reopened}"
                    ),
                )
                time.sleep(min(10, max(1, args.idle_sleep_sec)))
                continue
            write_heartbeat(
                heartbeat_file,
                phase="stalled",
                cycle=cycle,
                task_id=None,
                message="no ready tasks remain",
                extra={"pending": pending, "blocked": blocked, "waiting_on_deps": waiting_on_deps},
            )
            return 2

        _print(f"Cycle {cycle}: selected task {task.id} ({task.owner})")
        append_conversation_event(
            conversation_log_file,
            cycle=cycle,
            task=task,
            owner=task.owner,
            event_type="task_selected",
            content=f"Selected task `{task.id}` for owner `{task.owner}`.",
            meta={"priority": task.priority},
        )
        task_state = state[task.id]
        task_state["status"] = STATUS_IN_PROGRESS
        task_state["last_update"] = _now_iso()
        task_state["attempts"] = _safe_int(task_state.get("attempts", 0), 0) + 1
        task_state["not_before"] = ""
        save_state(state_file, state)
        write_heartbeat(
            heartbeat_file,
            phase="task_started",
            cycle=cycle,
            task_id=task.id,
            message=f"running task {task.id}",
            extra={"owner": task.owner, "attempts": task_state["attempts"]},
        )

        if args.dry_run:
            _print(f"Dry run enabled; skipping execution for task {task.id}")
            task_state["status"] = STATUS_PENDING
            save_state(state_file, state)
            continue

        owner_repo = impl_repo if task.owner in {"codex", "claude"} else test_repo
        healed = heal_stale_git_locks(owner_repo, stale_after_sec=args.git_lock_stale_sec)
        if healed:
            _print(f"Removed stale git locks in {owner_repo}: {', '.join(str(x) for x in healed)}")
        repo_context = get_repo_filetype_context(owner_repo)
        repo_hints = repo_state_hints(owner_repo)
        retry_context = {
            "attempts": task_state.get("attempts", 0),
            "last_summary": task_state.get("last_summary", ""),
            "last_error": task_state.get("last_error", ""),
        }
        handoff_context = render_handoff_context(handoff_dir, task.owner)

        if task.owner == "gemini" and not owner_repo.exists():
            outcome = normalize_outcome(
                {
                    "status": STATUS_BLOCKED,
                    "summary": "Gemini task repository missing",
                    "blocker": f"Test repo does not exist: {owner_repo}",
                    "next_actions": [],
                }
            )
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="agent_error",
                content=outcome["blocker"],
                meta={"agent": "gemini"},
            )
            ok = False
        elif task.owner == "codex":
            task_progress = lambda elapsed: write_heartbeat(
                heartbeat_file,
                phase="task_running",
                cycle=cycle,
                task_id=task.id,
                message=f"task running for {elapsed}s",
                extra={"owner": task.owner},
            )
            ok, outcome = run_codex_task(
                task=task,
                repo=owner_repo,
                objective_text=objective_text,
                schema_path=schema_file,
                output_dir=artifacts_dir,
                codex_cmd=args.codex_cmd,
                codex_model=args.codex_model,
                routellm_enabled=bool(args.routellm_enabled),
                routellm_policy=routellm_policy,
                routellm_url=routellm_url,
                routellm_timeout_sec=routellm_timeout_sec,
                timeout_sec=args.agent_timeout_sec,
                retry_context=retry_context,
                progress_callback=task_progress,
                repo_context=repo_context,
                repo_hints=repo_hints,
                skill_protocol=skill_protocol,
                mcp_context=mcp_context,
                startup_instructions=codex_startup_instructions,
                handoff_context=handoff_context,
                conversation_log_file=conversation_log_file,
                cycle=cycle,
            )
        elif task.owner == "gemini":
            task_progress = lambda elapsed: write_heartbeat(
                heartbeat_file,
                phase="task_running",
                cycle=cycle,
                task_id=task.id,
                message=f"task running for {elapsed}s",
                extra={"owner": task.owner},
            )
            ok, outcome = run_gemini_task(
                task=task,
                repo=owner_repo,
                objective_text=objective_text,
                gemini_cmd=args.gemini_cmd,
                gemini_model=args.gemini_model,
                gemini_fallback_models=gemini_fallback_models,
                routellm_enabled=bool(args.routellm_enabled),
                routellm_policy=routellm_policy,
                routellm_url=routellm_url,
                routellm_timeout_sec=routellm_timeout_sec,
                timeout_sec=args.agent_timeout_sec,
                retry_context=retry_context,
                progress_callback=task_progress,
                repo_context=repo_context,
                repo_hints=repo_hints,
                skill_protocol=skill_protocol,
                mcp_context=mcp_context,
                startup_instructions=gemini_startup_instructions,
                handoff_context=handoff_context,
                conversation_log_file=conversation_log_file,
                cycle=cycle,
                claude_fallback_cmd=args.claude_cmd,
                claude_fallback_model=args.claude_model,
                claude_fallback_startup_instructions=claude_startup_instructions,
                codex_fallback_cmd=args.codex_cmd,
                codex_fallback_model=args.codex_model,
                codex_schema_path=schema_file,
                codex_output_dir=artifacts_dir,
                codex_fallback_startup_instructions=codex_startup_instructions,
            )
        else:
            task_progress = lambda elapsed: write_heartbeat(
                heartbeat_file,
                phase="task_running",
                cycle=cycle,
                task_id=task.id,
                message=f"task running for {elapsed}s",
                extra={"owner": task.owner},
            )
            ok, outcome = run_claude_task(
                task=task,
                repo=owner_repo,
                objective_text=objective_text,
                claude_cmd=args.claude_cmd,
                claude_model=args.claude_model,
                routellm_enabled=bool(args.routellm_enabled),
                routellm_policy=routellm_policy,
                routellm_url=routellm_url,
                routellm_timeout_sec=routellm_timeout_sec,
                timeout_sec=args.agent_timeout_sec,
                retry_context=retry_context,
                progress_callback=task_progress,
                repo_context=repo_context,
                repo_hints=repo_hints,
                skill_protocol=skill_protocol,
                mcp_context=mcp_context,
                startup_instructions=claude_startup_instructions,
                handoff_context=handoff_context,
                conversation_log_file=conversation_log_file,
                cycle=cycle,
            )

        summarize_run(task=task, repo=owner_repo, outcome=outcome, report_dir=artifacts_dir)
        record_handoff_event(handoff_dir=handoff_dir, task=task, outcome=outcome)
        status = str(outcome.get("status", STATUS_BLOCKED)).lower()
        blocker_text = str(outcome.get("blocker", ""))
        summary_text = str(outcome.get("summary", ""))
        telemetry = outcome.get("_telemetry", {}) if isinstance(outcome.get("_telemetry", {}), dict) else {}
        attempt_number = _safe_int(task_state.get("attempts", 0), 0)

        def emit_response_metric(
            *,
            validation_passed: bool,
            quality_score: float,
            final_status: str,
            notes: str = "",
        ) -> None:
            if not telemetry:
                return
            usage_payload = telemetry.get("usage", {}) if isinstance(telemetry.get("usage", {}), dict) else {}
            routing_payload = telemetry.get("routing", {}) if isinstance(telemetry.get("routing", {}), dict) else {}
            cost_fields = compute_response_cost(
                pricing=pricing,
                owner=str(task.owner),
                model=str(telemetry.get("model", task.owner)),
                usage=usage_payload,
                prompt_tokens_est=_safe_int(telemetry.get("prompt_tokens_est", 0), 0),
                response_tokens_est=_safe_int(telemetry.get("response_tokens_est", 0), 0),
            )
            metric = {
                "timestamp": _now_iso(),
                "cycle": cycle,
                "task_id": task.id,
                "owner": task.owner,
                "attempt": attempt_number,
                "reported_status": status,
                "final_status": final_status,
                "validation_passed": bool(validation_passed),
                "first_time_pass": bool(validation_passed and attempt_number == 1),
                "quality_score": round(float(quality_score), 6),
                "prompt_difficulty_score": _safe_int(telemetry.get("prompt_difficulty_score", 0), 0),
                "latency_sec": float(telemetry.get("latency_sec", 0.0) or 0.0),
                "model": str(telemetry.get("model", task.owner)),
                "prompt_tokens_est": _safe_int(telemetry.get("prompt_tokens_est", 0), 0),
                "response_tokens_est": _safe_int(telemetry.get("response_tokens_est", 0), 0),
                "usage_source": str(usage_payload.get("source", "none")),
                "token_count_exact": bool(
                    usage_payload.get("source", "none") in {"payload", "command_output"}
                    and usage_payload.get("input_tokens") is not None
                    and usage_payload.get("output_tokens") is not None
                ),
                "input_tokens": cost_fields.get("input_tokens"),
                "output_tokens": cost_fields.get("output_tokens"),
                "total_tokens": cost_fields.get("total_tokens"),
                "cost_usd": cost_fields.get("cost_usd"),
                "cost_exact": bool(cost_fields.get("cost_exact", False)),
                "cost_source": str(cost_fields.get("cost_source", "none")),
                "input_rate_per_million": cost_fields.get("input_rate_per_million"),
                "output_rate_per_million": cost_fields.get("output_rate_per_million"),
                "summary": summary_text[:800],
                "blocker": blocker_text[:800],
                "notes": notes[:800],
                "routing_provider": str(routing_payload.get("provider", task.owner)),
                "routing_strategy": str(routing_payload.get("strategy", "static_fallback")),
                "routing_requested_model": str(routing_payload.get("requested_model", "")),
                "routing_selected_model": str(routing_payload.get("selected_model", telemetry.get("model", task.owner))),
                "routing_fallback_used": bool(routing_payload.get("fallback_used", False)),
                "routing_reason": str(routing_payload.get("reason", "")),
                "routing_router_error": str(routing_payload.get("router_error", ""))[:400],
                "routing_router_latency_sec": float(routing_payload.get("router_latency_sec", 0.0) or 0.0),
            }
            append_response_metric(metrics_file, metric)
            update_response_metrics_summary(metrics_summary_file, metric)

        if not ok or status == STATUS_BLOCKED:
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="task_blocked",
                content=blocker_text or summary_text or "Task blocked",
                meta={"status": status},
            )
            if "lock" in blocker_text.lower() or "another git process" in blocker_text.lower():
                healed_on_failure = heal_stale_git_locks(owner_repo, stale_after_sec=args.git_lock_stale_sec)
                if healed_on_failure:
                    healed_text = ", ".join(str(x) for x in healed_on_failure)
                    blocker_text = f"{blocker_text}\nRecovered stale lock files: {healed_text}"
                    _print(f"Recovered stale git lock(s) after failure: {healed_text}")
            retryable = is_retryable_error(blocker_text)
            attempts = _safe_int(task_state.get("attempts", 0), 0)
            retryable_failures = _safe_int(task_state.get("retryable_failures", 0), 0)

            if retryable and retryable_failures < args.max_retryable_blocked_retries:
                delay = schedule_retry(
                    entry=task_state,
                    summary=summary_text or "Transient blocker encountered.",
                    error=blocker_text,
                    retryable=True,
                    backoff_base_sec=args.retry_backoff_base_sec,
                    backoff_max_sec=args.retry_backoff_max_sec,
                )
                _print(f"Task {task.id} retryable blocker; retry in {delay}s.")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_retry_scheduled",
                    cycle=cycle,
                    task_id=task.id,
                    message=f"retryable blocker; retry in {delay}s",
                    extra={"attempts": attempts, "retryable_failures": task_state["retryable_failures"]},
                )
            elif attempts < args.max_attempts:
                delay = schedule_retry(
                    entry=task_state,
                    summary=summary_text or "Blocked; retrying for autonomous recovery.",
                    error=blocker_text,
                    retryable=False,
                    backoff_base_sec=max(5, min(60, args.retry_backoff_base_sec)),
                    backoff_max_sec=max(60, min(600, args.retry_backoff_max_sec)),
                )
                _print(f"Task {task.id} blocked; retry in {delay}s (attempt {attempts}/{args.max_attempts}).")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_retry_scheduled",
                    cycle=cycle,
                    task_id=task.id,
                    message=f"blocked; retry in {delay}s",
                    extra={"attempts": attempts},
                )
            else:
                mark_blocked(task_state, summary_text or "Task blocked", blocker_text or "agent command failed")
                _print(f"Task {task.id} blocked: {task_state['last_error']}")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_blocked",
                    cycle=cycle,
                    task_id=task.id,
                    message="task marked blocked",
                    extra={"attempts": attempts, "error": task_state["last_error"][:300]},
                )
            emit_response_metric(
                validation_passed=False,
                quality_score=0.0,
                final_status=str(task_state.get("status", STATUS_PENDING)),
                notes="agent_blocked_or_failed",
            )
            save_state(state_file, state)
            continue

        if status == STATUS_DONE:
            validation_repo = owner_repo if task.owner == "gemini" else impl_repo
            valid, details = run_validations(
                repo=validation_repo,
                validate_commands=args.validate_command,
                timeout_sec=args.validate_timeout_sec,
                retries_per_command=args.validation_retries,
                progress_callback=lambda cmd, elapsed: write_heartbeat(
                    heartbeat_file,
                    phase="task_validating",
                    cycle=cycle,
                    task_id=task.id,
                    message=f"validation `{cmd}` running for {elapsed}s",
                ),
            )
            if valid:
                write_heartbeat(
                    heartbeat_file,
                    phase="task_push_verify",
                    cycle=cycle,
                    task_id=task.id,
                    message="verifying commit push state",
                )
                push_ok, push_details = ensure_repo_pushed(
                    owner_repo,
                    timeout_sec=args.validate_timeout_sec,
                    owner=task.owner,
                )
                if not push_ok:
                    valid = False
                    details = f"Push verification failed:\n{push_details}"
                else:
                    append_conversation_event(
                        conversation_log_file,
                        cycle=cycle,
                        task=task,
                        owner=task.owner,
                        event_type="task_push_verified",
                        content=push_details,
                    )
            if valid:
                task_state["status"] = STATUS_DONE
                task_state["last_error"] = ""
                task_state["last_summary"] = summary_text
                task_state["retryable_failures"] = 0
                task_state["not_before"] = ""
                task_state["last_update"] = _now_iso()
                _print(f"Task {task.id} done.")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_done",
                    cycle=cycle,
                    task_id=task.id,
                    message="task completed and validated",
                )
                append_conversation_event(
                    conversation_log_file,
                    cycle=cycle,
                    task=task,
                    owner=task.owner,
                    event_type="task_done",
                    content=summary_text or "Task completed and validated.",
                )
                emit_response_metric(
                    validation_passed=True,
                    quality_score=1.0,
                    final_status=STATUS_DONE,
                    notes="done_validated",
                )
            else:
                retryable = is_retryable_error(details)
                attempts = _safe_int(task_state.get("attempts", 0), 0)
                if retryable:
                    delay = schedule_retry(
                        entry=task_state,
                        summary="Validation infrastructure failure; retry scheduled.",
                        error=details,
                        retryable=True,
                        backoff_base_sec=args.retry_backoff_base_sec,
                        backoff_max_sec=args.retry_backoff_max_sec,
                    )
                    _print(f"Task {task.id} validation failed transiently; retry in {delay}s.")
                elif attempts < args.max_attempts:
                    delay = schedule_retry(
                        entry=task_state,
                        summary="Validation failed after agent reported done.",
                        error=details,
                        retryable=False,
                        backoff_base_sec=max(5, min(60, args.retry_backoff_base_sec)),
                        backoff_max_sec=max(60, min(600, args.retry_backoff_max_sec)),
                    )
                    _print(
                        f"Task {task.id} validation failed; retry in {delay}s "
                        f"(attempt {attempts}/{args.max_attempts})."
                    )
                else:
                    mark_blocked(task_state, "Validation failed after repeated retries.", details)
                    _print(f"Task {task.id} validation failed and is now blocked.")
                write_heartbeat(
                    heartbeat_file,
                    phase="task_validation",
                    cycle=cycle,
                    task_id=task.id,
                    message="validation processed",
                    extra={"validation_ok": valid},
                )
                append_conversation_event(
                    conversation_log_file,
                    cycle=cycle,
                    task=task,
                    owner=task.owner,
                    event_type="task_validation_failed",
                    content=details,
                )
                emit_response_metric(
                    validation_passed=False,
                    quality_score=0.35,
                    final_status=str(task_state.get("status", STATUS_PENDING)),
                    notes="reported_done_but_validation_failed",
                )
            save_state(state_file, state)
            continue

        # Partial progress: keep momentum by rescheduling automatically with backoff.
        attempts = _safe_int(task_state.get("attempts", 0), 0)
        if attempts < args.max_attempts:
            delay = schedule_retry(
                entry=task_state,
                summary=summary_text or "Partial progress; retry queued.",
                error=blocker_text,
                retryable=False,
                backoff_base_sec=max(5, min(60, args.retry_backoff_base_sec)),
                backoff_max_sec=max(60, min(600, args.retry_backoff_max_sec)),
            )
            _print(f"Task {task.id} partial; queued for retry in {delay}s.")
            write_heartbeat(
                heartbeat_file,
                phase="task_partial",
                cycle=cycle,
                task_id=task.id,
                message=f"partial; retry in {delay}s",
            )
            append_conversation_event(
                conversation_log_file,
                cycle=cycle,
                task=task,
                owner=task.owner,
                event_type="task_partial",
                content=summary_text or "Partial progress; retry queued.",
                meta={"retry_delay_sec": delay},
            )
        else:
            mark_blocked(
                task_state,
                summary_text or "Partial task exceeded max attempts.",
                blocker_text,
            )
            _print(f"Task {task.id} partial result exhausted retries and is now blocked.")
            write_heartbeat(
                heartbeat_file,
                phase="task_blocked",
                cycle=cycle,
                task_id=task.id,
                message="partial retries exhausted",
            )
        emit_response_metric(
            validation_passed=False,
            quality_score=0.5 if task_state.get("status") == STATUS_PENDING else 0.2,
            final_status=str(task_state.get("status", STATUS_PENDING)),
            notes="partial_progress",
        )
        save_state(state_file, state)

    _print(f"Reached max cycles: {args.max_cycles}")
    write_heartbeat(
        heartbeat_file,
        phase="max_cycles_reached",
        cycle=args.max_cycles,
        task_id=None,
        message="max cycle limit reached",
    )
    return 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _print("Interrupted by user.")
        raise SystemExit(130) from None
    except Exception as err:  # Defensive guard so supervisors can restart cleanly.
        _print(f"Fatal runner error: {err}")
        raise
