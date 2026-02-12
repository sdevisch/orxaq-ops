# Objective: Local Model Workhorse Autonomy

Build and operate a resilient local-first model fleet where networked LM Studio models are the default workhorses for coding/autonomy workloads.

## Outcomes

1. Local models handle the majority of routine execution, implementation, and validation work.
2. Routing is complexity-aware:
- Simple/high-throughput tasks use faster low-cost local models.
- Medium/complex coding tasks use stronger local coder models.
- Only after local saturation should cheaper hosted models be used.
- Premium hosted models are reserved for deep code review, complex planning, and deep research.
3. New local models are continuously discovered, benchmarked, and canary-tested before broad routing promotion.
4. Collaboration, cost optimization, and dashboarding all expose local fleet health/load and model tier decisions.

## Constraints

- Preserve lane isolation and AGENTS.md validation requirements.
- Keep deterministic fallback behavior and robust error handling.
- Do not regress existing RouteLLM/NPV scaling safeguards.
- Prioritize reversible changes with clear runbook/operator controls.

## Success Metrics

- Local endpoint healthy ratio >= 80% over rolling window.
- Local-routing utilization increases while blended cost per million tokens decreases.
- Fallback/error rates remain within operational thresholds.
- New model onboarding cycle (discover -> benchmark -> canary -> eligible) is automated and observable.
