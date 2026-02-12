# Codex RouteLLM + NPV Objective

Deliver a production-grade routing-economics profile for `orxaq-ops` that:
- routes eligible model work through RouteLLM with deterministic fallback,
- scales agents/subagents only when marginal NPV justifies additional spend,
- preserves existing governance, reliability, and safety constraints.

Scope:
- `src/orxaq_autonomy` routing, scheduling, and telemetry changes.
- Configuration surfaces for RouteLLM policy, budgets, and NPV thresholds.
- Dashboard/status visibility for routing and scaling decisions.
- Independent tests in `orxaq_gemini` for routing and economic gate correctness.

Boundary:
- Keep behavior safe-by-default when RouteLLM is unavailable.
- Do not bypass branch protections, validation gates, or non-interactive constraints.
- Prefer additive, reversible changes with explicit kill-switches.

Execution:
- Work fully autonomously.
- Validate in each owning repository with required gates.
- Commit/push contiguous logical units with evidence-backed summaries.
