"""LM Studio local inference client for offline-first swarm routing.

Provides model discovery, health checks, chat completions, and capability
classification for LM Studio models running on the local network.
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
class LMStudioModel:
    """A model loaded in LM Studio."""
    id: str
    owned_by: str = "organization_owner"
    # Inferred capability based on model ID patterns
    capability: str = "general"  # coding, reasoning, general, embedding, small
    # Size class inferred from model name
    size_class: str = "unknown"  # small (<=7B), medium (8-32B), large (33-70B), xlarge (>70B)


@dataclass
class LMStudioStatus:
    """Health and model inventory from LM Studio."""
    url: str
    reachable: bool
    latency_ms: float | None
    models: list[LMStudioModel]
    error: str = ""
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["model_count"] = len(self.models)
        return d


@dataclass
class ChatMessage:
    role: str  # system, user, assistant
    content: str


@dataclass
class ChatCompletion:
    """Response from LM Studio chat completion."""
    model: str
    content: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Model capability patterns — classify by ID substring matching
_CODING_PATTERNS = ("coder", "codellama", "code-", "starcoder", "deepseek-coder", "codestral")
_REASONING_PATTERNS = ("deepseek-r1", "o1", "o3", "qwen3-thinking", "reflection")
_EMBEDDING_PATTERNS = ("embed", "embedding", "nomic-embed", "bge-", "e5-")
_SMALL_PATTERNS = ("1b", "1.2b", "1.5b", "2b", "3b", "4b")

def _classify_capability(model_id: str) -> str:
    lower = model_id.lower()
    for pat in _EMBEDDING_PATTERNS:
        if pat in lower:
            return "embedding"
    for pat in _CODING_PATTERNS:
        if pat in lower:
            return "coding"
    for pat in _REASONING_PATTERNS:
        if pat in lower:
            return "reasoning"
    for pat in _SMALL_PATTERNS:
        if lower.endswith(pat) or f"-{pat}" in lower or f"/{pat}" in lower:
            return "small"
    return "general"

def _classify_size(model_id: str) -> str:
    lower = model_id.lower()
    # Look for size indicators like 70b, 32b, 8b, etc.
    import re
    match = re.search(r"(\d+)b", lower)
    if match:
        size = int(match.group(1))
        if size <= 7:
            return "small"
        elif size <= 32:
            return "medium"
        elif size <= 70:
            return "large"
        else:
            return "xlarge"
    return "unknown"


class LMStudioClient:
    """Client for LM Studio's OpenAI-compatible API."""

    def __init__(self, base_url: str = "http://localhost:1234", timeout_sec: int = 10):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec

    @property
    def base_url(self) -> str:
        return self._base_url

    def health_check(self) -> LMStudioStatus:
        """Quick health check — ping /v1/models and return status + model inventory."""
        url = f"{self._base_url}/v1/models"
        started = time.monotonic()
        try:
            req = urllib_request.Request(url, method="GET", headers={"User-Agent": "orxaq-swarm/lmstudio"})
            with urllib_request.urlopen(req, timeout=min(3, self._timeout)) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            latency = round((time.monotonic() - started) * 1000.0, 3)
            payload = json.loads(raw)
            models = []
            for m in payload.get("data", []):
                mid = str(m.get("id", ""))
                if not mid:
                    continue
                models.append(LMStudioModel(
                    id=mid,
                    owned_by=str(m.get("owned_by", "organization_owner")),
                    capability=_classify_capability(mid),
                    size_class=_classify_size(mid),
                ))
            return LMStudioStatus(
                url=self._base_url,
                reachable=True,
                latency_ms=latency,
                models=models,
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            return LMStudioStatus(
                url=self._base_url,
                reachable=False,
                latency_ms=None,
                models=[],
                error=str(exc)[:300],
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            return LMStudioStatus(
                url=self._base_url,
                reachable=False,
                latency_ms=None,
                models=[],
                error=str(exc)[:300],
                checked_at=datetime.now(timezone.utc).isoformat(),
            )

    def list_models(self) -> list[LMStudioModel]:
        """Return loaded models. Empty list if unreachable."""
        status = self.health_check()
        return status.models

    def best_model_for(self, capability: str = "coding") -> str | None:
        """Find the best loaded model for a given capability.

        Prefers larger models. Returns model ID or None.
        """
        models = self.list_models()
        # Filter by capability
        matching = [m for m in models if m.capability == capability]
        if not matching:
            # Fall back to general models
            matching = [m for m in models if m.capability == "general"]
        if not matching:
            # Fall back to any non-embedding model
            matching = [m for m in models if m.capability != "embedding"]
        if not matching:
            return None
        # Prefer larger models
        size_order = {"xlarge": 0, "large": 1, "medium": 2, "small": 3, "unknown": 4}
        matching.sort(key=lambda m: size_order.get(m.size_class, 5))
        return matching[0].id

    def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream: bool = False,
    ) -> ChatCompletion:
        """Send a chat completion request to LM Studio.

        If model is None, uses the first available model.
        Raises RuntimeError on failure.
        """
        if model is None:
            models = self.list_models()
            non_embed = [m for m in models if m.capability != "embedding"]
            if not non_embed:
                raise RuntimeError("No non-embedding models loaded in LM Studio")
            model = non_embed[0].id

        url = f"{self._base_url}/v1/chat/completions"
        body = json.dumps({
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,  # Always non-streaming for simplicity
        }).encode("utf-8")

        req = urllib_request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "orxaq-swarm/lmstudio",
            },
        )

        started = time.monotonic()
        try:
            with urllib_request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            latency = round((time.monotonic() - started) * 1000.0, 3)
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"LM Studio request failed: {exc}") from exc

        payload = json.loads(raw)
        choices = payload.get("choices", [])
        if not choices:
            raise RuntimeError("LM Studio returned no choices")

        choice = choices[0]
        message = choice.get("message", {})
        usage = payload.get("usage", {})

        return ChatCompletion(
            model=payload.get("model", model),
            content=str(message.get("content", "")),
            finish_reason=str(choice.get("finish_reason", "stop")),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            total_tokens=int(usage.get("total_tokens", 0)),
            latency_ms=latency,
        )


# Module-level convenience functions

_default_client: LMStudioClient | None = None

def get_client(base_url: str = "http://localhost:1234") -> LMStudioClient:
    """Get or create the default LM Studio client."""
    global _default_client
    if _default_client is None or _default_client.base_url != base_url:
        _default_client = LMStudioClient(base_url=base_url)
    return _default_client

def discover_models(base_url: str = "http://localhost:1234") -> LMStudioStatus:
    """Discover models from LM Studio. Returns status with model inventory."""
    return get_client(base_url).health_check()

def is_available(base_url: str = "http://localhost:1234") -> bool:
    """Quick check: is LM Studio running and reachable?"""
    return get_client(base_url).health_check().reachable
