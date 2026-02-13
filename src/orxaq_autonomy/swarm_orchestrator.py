"""Multi-model swarm orchestrator with offline-first routing.

Routes tasks to the best available model based on complexity, network status,
provider health, and cost constraints. Implements automatic failover and
deferred task queuing for network-resilient operation.

Zero external dependencies — uses only Python stdlib.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from . import lmstudio_client, network_status


class TaskComplexity(str, Enum):
    """Task complexity classification for routing decisions."""
    LOW = "low"           # File exploration, grep, simple edits — L0 local small
    MEDIUM = "medium"     # Code implementation, unit tests — L0/L1 local
    HIGH = "high"         # Architecture decisions, complex debugging — L1 local or L2 cloud
    CRITICAL = "critical" # Security review, multi-model consensus — L2/L3 cloud required


class RoutingTier(str, Enum):
    """Model routing tiers aligned with lane system."""
    L0_LOCAL_SMALL = "L0"     # LM Studio small models (<=7B)
    L1_LOCAL_STRONG = "L1"    # LM Studio large models (32B-70B+)
    L2_CLOUD_STANDARD = "L2"  # Cloud standard (Sonnet, GPT-4o, Gemini Pro)
    L3_CLOUD_PREMIUM = "L3"   # Cloud premium (Opus, GPT-5, o3)


@dataclass
class RoutingDecision:
    """A routing decision for a task."""
    task_id: str
    task_description: str
    complexity: str
    network_status: str  # online/degraded/offline
    lmstudio_available: bool
    selected_tier: str
    selected_provider: str
    selected_model: str
    reason: str
    fallback_chain: list[str]
    deferred: bool = False
    timestamp: str = ""
    cost_estimate_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CostTracker:
    """Track cumulative costs per provider."""
    costs: dict[str, float] = field(default_factory=dict)
    request_counts: dict[str, int] = field(default_factory=dict)

    def record(self, provider: str, cost_usd: float) -> None:
        self.costs[provider] = self.costs.get(provider, 0.0) + cost_usd
        self.request_counts[provider] = self.request_counts.get(provider, 0) + 1

    def total_usd(self) -> float:
        return sum(self.costs.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_usd": round(self.total_usd(), 6),
            "by_provider": {k: round(v, 6) for k, v in sorted(self.costs.items())},
            "request_counts": dict(sorted(self.request_counts.items())),
        }


@dataclass
class DeferredTask:
    """A task deferred for later execution when network improves."""
    task_id: str
    task_description: str
    complexity: str
    required_tier: str
    queued_at: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Complexity classification heuristics
_COMPLEXITY_KEYWORDS: dict[str, list[str]] = {
    TaskComplexity.LOW: [
        "grep", "find", "list", "read", "search", "explore", "count",
        "check", "verify", "status", "health",
    ],
    TaskComplexity.MEDIUM: [
        "implement", "code", "write", "create", "add", "build", "test",
        "fix", "update", "modify", "edit", "refactor",
    ],
    TaskComplexity.HIGH: [
        "architect", "design", "debug", "investigate", "analyze", "optimize",
        "performance", "security", "review", "complex",
    ],
    TaskComplexity.CRITICAL: [
        "consensus", "multi-model", "security audit", "vulnerability",
        "production deploy", "data migration", "breaking change",
    ],
}


def classify_complexity(description: str) -> TaskComplexity:
    """Classify task complexity from its description."""
    lower = description.lower()
    # Check from most complex to least
    for level in [TaskComplexity.CRITICAL, TaskComplexity.HIGH, TaskComplexity.MEDIUM]:
        keywords = _COMPLEXITY_KEYWORDS[level]
        if any(kw in lower for kw in keywords):
            return level
    return TaskComplexity.LOW


# Routing table: complexity × network_status → preferred tier + fallbacks
_ROUTING_TABLE: dict[tuple[str, str], tuple[str, list[str]]] = {
    # LOW complexity
    (TaskComplexity.LOW, network_status.NetworkStatus.ONLINE): (
        RoutingTier.L0_LOCAL_SMALL, [RoutingTier.L1_LOCAL_STRONG, RoutingTier.L2_CLOUD_STANDARD]),
    (TaskComplexity.LOW, network_status.NetworkStatus.DEGRADED): (
        RoutingTier.L0_LOCAL_SMALL, [RoutingTier.L1_LOCAL_STRONG]),
    (TaskComplexity.LOW, network_status.NetworkStatus.OFFLINE): (
        RoutingTier.L0_LOCAL_SMALL, [RoutingTier.L1_LOCAL_STRONG]),

    # MEDIUM complexity
    (TaskComplexity.MEDIUM, network_status.NetworkStatus.ONLINE): (
        RoutingTier.L1_LOCAL_STRONG, [RoutingTier.L0_LOCAL_SMALL, RoutingTier.L2_CLOUD_STANDARD]),
    (TaskComplexity.MEDIUM, network_status.NetworkStatus.DEGRADED): (
        RoutingTier.L1_LOCAL_STRONG, [RoutingTier.L0_LOCAL_SMALL]),
    (TaskComplexity.MEDIUM, network_status.NetworkStatus.OFFLINE): (
        RoutingTier.L1_LOCAL_STRONG, [RoutingTier.L0_LOCAL_SMALL]),

    # HIGH complexity
    (TaskComplexity.HIGH, network_status.NetworkStatus.ONLINE): (
        RoutingTier.L2_CLOUD_STANDARD, [RoutingTier.L1_LOCAL_STRONG, RoutingTier.L3_CLOUD_PREMIUM]),
    (TaskComplexity.HIGH, network_status.NetworkStatus.DEGRADED): (
        RoutingTier.L1_LOCAL_STRONG, [RoutingTier.L2_CLOUD_STANDARD]),
    (TaskComplexity.HIGH, network_status.NetworkStatus.OFFLINE): (
        RoutingTier.L1_LOCAL_STRONG, [RoutingTier.L0_LOCAL_SMALL]),

    # CRITICAL complexity
    (TaskComplexity.CRITICAL, network_status.NetworkStatus.ONLINE): (
        RoutingTier.L3_CLOUD_PREMIUM, [RoutingTier.L2_CLOUD_STANDARD, RoutingTier.L1_LOCAL_STRONG]),
    (TaskComplexity.CRITICAL, network_status.NetworkStatus.DEGRADED): (
        RoutingTier.L2_CLOUD_STANDARD, [RoutingTier.L1_LOCAL_STRONG]),
    (TaskComplexity.CRITICAL, network_status.NetworkStatus.OFFLINE): (
        RoutingTier.L1_LOCAL_STRONG, []),  # May need to defer
}

# Tier → capability mapping for LM Studio model selection
_TIER_CAPABILITY: dict[str, str] = {
    RoutingTier.L0_LOCAL_SMALL: "small",
    RoutingTier.L1_LOCAL_STRONG: "coding",  # Prefer coding models for implementation
}

# Cost estimates per 1K tokens (input/output) by tier
_COST_PER_1K: dict[str, tuple[float, float]] = {
    RoutingTier.L0_LOCAL_SMALL: (0.0, 0.0),
    RoutingTier.L1_LOCAL_STRONG: (0.0, 0.0),
    RoutingTier.L2_CLOUD_STANDARD: (0.003, 0.015),
    RoutingTier.L3_CLOUD_PREMIUM: (0.005, 0.025),
}


class SwarmOrchestrator:
    """Multi-model swarm orchestrator with offline-first routing."""

    def __init__(
        self,
        *,
        lmstudio_url: str = "http://localhost:1234",
        artifacts_dir: Path | None = None,
        budget_limit_usd: float = 15.0,
        network_cache_ttl_sec: int = 60,
    ):
        self._lm_client = lmstudio_client.LMStudioClient(base_url=lmstudio_url)
        self._network = network_status.NetworkProbe(
            lmstudio_url=lmstudio_url,
            cache_ttl_sec=network_cache_ttl_sec,
        )
        self._artifacts = artifacts_dir
        self._budget_limit = budget_limit_usd
        self._costs = CostTracker()
        self._deferred: list[DeferredTask] = []
        self._decision_count = 0

    @property
    def costs(self) -> CostTracker:
        return self._costs

    @property
    def deferred_tasks(self) -> list[DeferredTask]:
        return list(self._deferred)

    def _resolve_local_model(self, tier: str, task_description: str) -> str | None:
        """Find the best LM Studio model for a routing tier."""
        if tier not in (RoutingTier.L0_LOCAL_SMALL, RoutingTier.L1_LOCAL_STRONG):
            return None
        # Determine desired capability from task
        lower = task_description.lower()
        if any(kw in lower for kw in ("code", "implement", "fix", "test", "build", "refactor")):
            capability = "coding"
        elif any(kw in lower for kw in ("reason", "analyze", "debug", "investigate", "think")):
            capability = "reasoning"
        else:
            capability = _TIER_CAPABILITY.get(tier, "general")
        return self._lm_client.best_model_for(capability)

    def _resolve_cloud_provider(self, tier: str) -> tuple[str, str]:
        """Resolve cloud provider and model for a tier. Returns (provider, model)."""
        if tier == RoutingTier.L2_CLOUD_STANDARD:
            return ("anthropic", "claude-sonnet-4-5-20250929")
        elif tier == RoutingTier.L3_CLOUD_PREMIUM:
            return ("anthropic", "claude-opus-4-6")
        return ("unknown", "unknown")

    def route(self, *, task_id: str, description: str, complexity: TaskComplexity | str | None = None) -> RoutingDecision:
        """Route a task to the best available model.

        Considers network status, model availability, cost budget, and task complexity.
        Returns a RoutingDecision with the selected provider and model.
        """
        self._decision_count += 1

        # Classify complexity if not provided
        if complexity is None:
            complexity = classify_complexity(description)
        elif isinstance(complexity, str):
            complexity = TaskComplexity(complexity)

        # Check network and LM Studio status
        net = self._network.check()
        lm_status = self._lm_client.health_check()

        # Look up routing table
        key = (complexity.value, net.status)
        preferred_tier, fallbacks = _ROUTING_TABLE.get(
            key, (RoutingTier.L1_LOCAL_STRONG, [RoutingTier.L0_LOCAL_SMALL])
        )

        # Build candidate chain: preferred + fallbacks
        candidate_chain = [preferred_tier] + fallbacks
        all_tiers_str = [str(t) for t in candidate_chain]

        selected_tier = None
        selected_provider = ""
        selected_model = ""
        reason = ""

        for tier in candidate_chain:
            tier_str = str(tier)
            # Local tiers
            if tier in (RoutingTier.L0_LOCAL_SMALL, RoutingTier.L1_LOCAL_STRONG):
                if lm_status.reachable:
                    model = self._resolve_local_model(tier_str, description)
                    if model:
                        selected_tier = tier_str
                        selected_provider = "lmstudio-local"
                        selected_model = model
                        reason = f"Local model available ({net.status})"
                        break
            # Cloud tiers
            elif tier in (RoutingTier.L2_CLOUD_STANDARD, RoutingTier.L3_CLOUD_PREMIUM):
                if net.status != network_status.NetworkStatus.OFFLINE:
                    # Check budget
                    if self._costs.total_usd() < self._budget_limit:
                        provider, model = self._resolve_cloud_provider(tier_str)
                        selected_tier = tier_str
                        selected_provider = provider
                        selected_model = model
                        reason = f"Cloud model selected ({net.status}, budget OK)"
                        break
                    else:
                        reason = "Budget limit reached, trying next tier"

        # If nothing was selected, we need to defer
        deferred = False
        if selected_tier is None:
            if lm_status.reachable:
                # Last resort: use any local model
                models = lm_status.models
                non_embed = [m for m in models if m.capability != "embedding"]
                if non_embed:
                    selected_tier = RoutingTier.L1_LOCAL_STRONG
                    selected_provider = "lmstudio-local"
                    selected_model = non_embed[0].id
                    reason = "Fallback to any available local model"
                else:
                    deferred = True
                    reason = "No suitable models available anywhere"
            else:
                deferred = True
                reason = "LM Studio offline and cloud unreachable"

        if deferred:
            self._deferred.append(DeferredTask(
                task_id=task_id,
                task_description=description,
                complexity=complexity.value,
                required_tier=str(preferred_tier),
                queued_at=datetime.now(timezone.utc).isoformat(),
                reason=reason,
            ))

        # Estimate cost
        cost_rates = _COST_PER_1K.get(selected_tier or "", (0.0, 0.0))
        est_tokens = 2.0  # Assume ~2K tokens average
        cost_estimate = (cost_rates[0] + cost_rates[1]) * est_tokens

        decision = RoutingDecision(
            task_id=task_id,
            task_description=description[:200],
            complexity=complexity.value,
            network_status=net.status,
            lmstudio_available=lm_status.reachable,
            selected_tier=selected_tier or "none",
            selected_provider=selected_provider or "none",
            selected_model=selected_model or "none",
            reason=reason,
            fallback_chain=all_tiers_str,
            deferred=deferred,
            timestamp=datetime.now(timezone.utc).isoformat(),
            cost_estimate_usd=round(cost_estimate, 6),
        )

        # Log decision to audit trail
        self._log_decision(decision)

        return decision

    def _log_decision(self, decision: RoutingDecision) -> None:
        """Append routing decision to NDJSON audit log."""
        if not self._artifacts:
            return
        log_path = self._artifacts / "autonomy" / "swarm_routing_decisions.ndjson"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(decision.to_dict(), sort_keys=True) + "\n")
        except OSError:
            pass  # Best-effort logging

    def process_deferred(self) -> list[RoutingDecision]:
        """Re-attempt routing for deferred tasks. Call when network improves."""
        if not self._deferred:
            return []
        net = self._network.check(force=True)
        if net.is_offline:
            return []
        results: list[RoutingDecision] = []
        remaining: list[DeferredTask] = []
        for task in self._deferred:
            decision = self.route(
                task_id=task.task_id,
                description=task.task_description,
                complexity=task.complexity,
            )
            if decision.deferred:
                remaining.append(task)
            else:
                results.append(decision)
        self._deferred = remaining
        return results

    def status_snapshot(self) -> dict[str, Any]:
        """Return full orchestrator status as a dict."""
        net = self._network.check()
        lm = self._lm_client.health_check()
        return {
            "network": net.to_dict(),
            "lmstudio": lm.to_dict(),
            "costs": self._costs.to_dict(),
            "deferred_count": len(self._deferred),
            "deferred_tasks": [t.to_dict() for t in self._deferred],
            "decisions_made": self._decision_count,
            "budget_limit_usd": self._budget_limit,
            "budget_remaining_usd": round(max(0, self._budget_limit - self._costs.total_usd()), 6),
        }


# Module-level convenience

_orchestrator: SwarmOrchestrator | None = None


def get_orchestrator(**kwargs: Any) -> SwarmOrchestrator:
    """Get or create the default orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = SwarmOrchestrator(**kwargs)
    return _orchestrator


def route_task(task_id: str, description: str) -> RoutingDecision:
    """Route a task using the default orchestrator."""
    return get_orchestrator().route(task_id=task_id, description=description)
