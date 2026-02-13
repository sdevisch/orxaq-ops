"""Swarm Health Gate: scoring, artifacts, and deterministic gating for CI/merge workflows.

Implements Phase 1 deliverables:
- Health scoring from provider connectivity, task state, and budget metrics
- Artifact generation (JSON + Markdown) for CI upload
- Strict deterministic gating logic for merge/PR workflows
- Connectivity report consumable by the swarm-health CLI
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Health Scoring
# ---------------------------------------------------------------------------

# Component weights for the composite health score (0-100)
_WEIGHT_PROVIDERS = 0.30
_WEIGHT_TASKS = 0.30
_WEIGHT_BUDGET = 0.20
_WEIGHT_HEARTBEAT = 0.20


@dataclass(frozen=True)
class ProviderHealthInput:
    """Provider connectivity input for scoring."""

    total: int = 0
    up: int = 0
    required_total: int = 0
    required_up: int = 0


@dataclass(frozen=True)
class TaskHealthInput:
    """Task state input for scoring."""

    total: int = 0
    done: int = 0
    blocked: int = 0
    pending: int = 0


@dataclass(frozen=True)
class BudgetHealthInput:
    """Budget utilization input for scoring."""

    elapsed_sec: int = 0
    max_runtime_sec: int = 0
    tokens_used: int = 0
    max_tokens: int = 0
    cost_usd: float = 0.0
    max_cost_usd: float = 0.0
    violations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HeartbeatHealthInput:
    """Heartbeat liveness input for scoring."""

    age_sec: int = -1
    stale_threshold_sec: int = 300


def _score_providers(inp: ProviderHealthInput) -> float:
    """Score provider connectivity 0-100."""
    if inp.total == 0:
        return 50.0  # No providers configured, neutral score
    base = (inp.up / max(1, inp.total)) * 100.0
    # Penalty for required providers being down
    if inp.required_total > 0 and inp.required_up < inp.required_total:
        penalty = ((inp.required_total - inp.required_up) / inp.required_total) * 50.0
        base = max(0.0, base - penalty)
    return round(base, 2)


def _score_tasks(inp: TaskHealthInput) -> float:
    """Score task progress 0-100."""
    if inp.total == 0:
        return 100.0  # No tasks, healthy
    progress = (inp.done / max(1, inp.total)) * 100.0
    # Penalty for blocked tasks
    if inp.blocked > 0:
        blocked_penalty = (inp.blocked / max(1, inp.total)) * 30.0
        progress = max(0.0, progress - blocked_penalty)
    return round(progress, 2)


def _score_budget(inp: BudgetHealthInput) -> float:
    """Score budget health 0-100."""
    if inp.violations:
        return 0.0
    score = 100.0
    # Runtime utilization
    if inp.max_runtime_sec > 0:
        utilization = inp.elapsed_sec / inp.max_runtime_sec
        if utilization > 0.9:
            score -= 30.0
        elif utilization > 0.7:
            score -= 10.0
    # Token utilization
    if inp.max_tokens > 0:
        token_util = inp.tokens_used / inp.max_tokens
        if token_util > 0.9:
            score -= 30.0
        elif token_util > 0.7:
            score -= 10.0
    # Cost utilization
    if inp.max_cost_usd > 0:
        cost_util = inp.cost_usd / inp.max_cost_usd
        if cost_util > 0.9:
            score -= 30.0
        elif cost_util > 0.7:
            score -= 10.0
    return round(max(0.0, score), 2)


def _score_heartbeat(inp: HeartbeatHealthInput) -> float:
    """Score heartbeat liveness 0-100."""
    if inp.age_sec < 0:
        return 50.0  # No heartbeat, neutral
    if inp.age_sec <= inp.stale_threshold_sec:
        return 100.0
    # Degrade linearly beyond threshold
    over = inp.age_sec - inp.stale_threshold_sec
    penalty = min(100.0, (over / max(1, inp.stale_threshold_sec)) * 100.0)
    return round(max(0.0, 100.0 - penalty), 2)


def compute_health_score(
    *,
    providers: ProviderHealthInput,
    tasks: TaskHealthInput,
    budget: BudgetHealthInput,
    heartbeat: HeartbeatHealthInput,
) -> dict[str, Any]:
    """Compute composite swarm health score.

    Returns a dict with individual component scores and the weighted composite.
    """
    provider_score = _score_providers(providers)
    task_score = _score_tasks(tasks)
    budget_score = _score_budget(budget)
    heartbeat_score = _score_heartbeat(heartbeat)

    composite = round(
        provider_score * _WEIGHT_PROVIDERS
        + task_score * _WEIGHT_TASKS
        + budget_score * _WEIGHT_BUDGET
        + heartbeat_score * _WEIGHT_HEARTBEAT,
        2,
    )

    return {
        "score": composite,
        "components": {
            "providers": {"score": provider_score, "weight": _WEIGHT_PROVIDERS},
            "tasks": {"score": task_score, "weight": _WEIGHT_TASKS},
            "budget": {"score": budget_score, "weight": _WEIGHT_BUDGET},
            "heartbeat": {"score": heartbeat_score, "weight": _WEIGHT_HEARTBEAT},
        },
        "inputs": {
            "providers": asdict(providers),
            "tasks": asdict(tasks),
            "budget": asdict(budget),
            "heartbeat": asdict(heartbeat),
        },
    }


# ---------------------------------------------------------------------------
# Health Gate (deterministic gating)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthGateResult:
    """Result of applying the health gate."""

    passed: bool
    score: float
    min_score: float
    reason: str
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_health_gate(
    score: float,
    *,
    min_score: float = 85.0,
    require_no_budget_violations: bool = True,
    budget_violations: list[str] | None = None,
    require_required_providers_up: bool = True,
    required_providers_down: int = 0,
) -> HealthGateResult:
    """Apply strict deterministic gating logic.

    Returns a HealthGateResult indicating pass/fail and the reason.
    """
    if require_required_providers_up and required_providers_down > 0:
        return HealthGateResult(
            passed=False,
            score=score,
            min_score=min_score,
            reason=f"required providers down: {required_providers_down}",
            timestamp=_now_iso(),
        )
    if require_no_budget_violations and budget_violations:
        return HealthGateResult(
            passed=False,
            score=score,
            min_score=min_score,
            reason=f"budget violations: {', '.join(budget_violations[:3])}",
            timestamp=_now_iso(),
        )
    if score < min_score:
        return HealthGateResult(
            passed=False,
            score=score,
            min_score=min_score,
            reason=f"score {score:.1f} below minimum {min_score:.1f}",
            timestamp=_now_iso(),
        )
    return HealthGateResult(
        passed=True,
        score=score,
        min_score=min_score,
        reason="all gates passed",
        timestamp=_now_iso(),
    )


# ---------------------------------------------------------------------------
# Artifact Generation
# ---------------------------------------------------------------------------


def generate_health_artifacts(
    *,
    health_report: dict[str, Any],
    gate_result: HealthGateResult,
    output_dir: Path,
    connectivity_report: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Generate JSON and Markdown health artifacts for CI upload.

    Returns a dict mapping artifact names to file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON artifact
    json_payload = {
        "schema_version": "swarm-health.v1",
        "generated_at_utc": _now_iso(),
        "health": health_report,
        "gate": gate_result.to_dict(),
    }
    if connectivity_report:
        json_payload["connectivity"] = connectivity_report

    json_path = output_dir / "swarm_health.json"
    json_path.write_text(
        json.dumps(json_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Markdown artifact
    components = health_report.get("components", {})
    md_lines = [
        "# Swarm Health Report",
        "",
        f"- **Composite Score**: {health_report.get('score', 0):.1f} / 100",
        f"- **Gate Passed**: {'yes' if gate_result.passed else 'NO'}",
        f"- **Minimum Required**: {gate_result.min_score:.1f}",
        f"- **Gate Reason**: {gate_result.reason}",
        f"- **Generated**: {_now_iso()}",
        "",
        "## Component Scores",
        "",
    ]
    for name, comp in sorted(components.items()):
        score = comp.get("score", 0)
        weight = comp.get("weight", 0)
        md_lines.append(f"- **{name}**: {score:.1f} (weight: {weight:.0%})")

    md_lines.extend(["", "## Gate Result", ""])
    if gate_result.passed:
        md_lines.append("All health gates passed. Merge is allowed.")
    else:
        md_lines.append(f"Health gate FAILED: {gate_result.reason}")

    md_path = output_dir / "swarm_health.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    return {
        "json": str(json_path),
        "markdown": str(md_path),
    }


# ---------------------------------------------------------------------------
# Connectivity Report Parser
# ---------------------------------------------------------------------------


def parse_connectivity_report(path: Path) -> ProviderHealthInput:
    """Parse a providers-check or router-check JSON into ProviderHealthInput."""
    if not path.exists():
        return ProviderHealthInput()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ProviderHealthInput()
    summary = data.get("summary", {})
    if not isinstance(summary, dict):
        return ProviderHealthInput()
    return ProviderHealthInput(
        total=int(summary.get("provider_total", 0)),
        up=int(summary.get("provider_up", 0)),
        required_total=int(summary.get("required_total", 0)),
        required_up=int(summary.get("required_up", summary.get("required_total", 0)) or 0)
        - int(summary.get("required_down", 0)),
    )


def parse_state_for_task_health(state: dict[str, Any]) -> TaskHealthInput:
    """Parse runner state dict into TaskHealthInput."""
    total = 0
    done = 0
    blocked = 0
    pending = 0
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        total += 1
        status = str(entry.get("status", "")).lower()
        if status == "done":
            done += 1
        elif status == "blocked":
            blocked += 1
        elif status in ("pending", "in_progress"):
            pending += 1
    return TaskHealthInput(total=total, done=done, blocked=blocked, pending=pending)


def parse_budget_for_health(budget: dict[str, Any]) -> BudgetHealthInput:
    """Parse budget report dict into BudgetHealthInput."""
    totals = budget.get("totals", {})
    limits = budget.get("limits", {})
    return BudgetHealthInput(
        elapsed_sec=int(budget.get("elapsed_sec", 0)),
        max_runtime_sec=int(limits.get("max_runtime_sec", 0)),
        tokens_used=int(totals.get("tokens", 0)),
        max_tokens=int(limits.get("max_total_tokens", 0)),
        cost_usd=float(totals.get("cost_usd", 0.0)),
        max_cost_usd=float(limits.get("max_total_cost_usd", 0.0)),
        violations=budget.get("violations", []),
    )


# ---------------------------------------------------------------------------
# Convenience: Run full health gate pipeline
# ---------------------------------------------------------------------------


def run_health_gate(
    *,
    providers_check_path: Path | None = None,
    state: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    heartbeat_age_sec: int = -1,
    heartbeat_stale_threshold_sec: int = 300,
    min_score: float = 85.0,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the full health gate pipeline and optionally write artifacts.

    Returns the gate result dict.
    """
    provider_input = (
        parse_connectivity_report(providers_check_path)
        if providers_check_path
        else ProviderHealthInput()
    )
    task_input = parse_state_for_task_health(state or {})
    budget_input = parse_budget_for_health(budget or {})
    heartbeat_input = HeartbeatHealthInput(
        age_sec=heartbeat_age_sec,
        stale_threshold_sec=heartbeat_stale_threshold_sec,
    )

    health_report = compute_health_score(
        providers=provider_input,
        tasks=task_input,
        budget=budget_input,
        heartbeat=heartbeat_input,
    )

    gate_result = evaluate_health_gate(
        score=health_report["score"],
        min_score=min_score,
        budget_violations=budget_input.violations,
        required_providers_down=max(0, provider_input.required_total - provider_input.required_up),
    )

    artifacts = {}
    if output_dir:
        artifacts = generate_health_artifacts(
            health_report=health_report,
            gate_result=gate_result,
            output_dir=output_dir,
        )

    return {
        "health": health_report,
        "gate": gate_result.to_dict(),
        "artifacts": artifacts,
    }
