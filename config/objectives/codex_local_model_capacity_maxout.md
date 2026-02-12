# Objective: Local Model Capacity Max-Out

Continuously detect local/remote LM Studio endpoint capabilities and maximize safe concurrency.

## Outcomes
- Endpoint-aware parallel isolation in the autonomy manager (no cross-host throttling collisions).
- Automated capability scan per endpoint:
  - Recommended parallel slots from observed success/latency under burst load.
  - Conservative context-size success estimate.
- Runtime concurrency limits adapt to capability scan output.
- Runner dynamically scales local `max_tokens` per endpoint from fleet capability data.
- Scheduler prioritizes non-backlog tasks first and drains backlog only during idle windows.
- Lane startup injects fleet-aware token maps so every local endpoint gets workload at safe maxima.
- Operational resilience for context-overflow errors (single retry at reduced token budget).

## Acceptance Criteria
- `scripts/local_model_fleet.py full-cycle` emits `capability_scan` in `artifacts/autonomy/local_models/fleet_status.json`.
- `lanes-ensure` treats same model on different endpoint hosts as distinct parallel groups.
- Fleet-derived endpoint limit is reflected in parallel capacity group summaries.
- Lane runtime env includes endpoint token map (`ORXAQ_LOCAL_OPENAI_MAX_TOKENS_BY_ENDPOINT`) when capability data exists.
- Local non-backlog tasks are selected ahead of backlog tasks when both are ready.
- Tests pass for endpoint isolation and fleet limit enforcement.

## Guardrails
- Keep scheduling conservative on errors/timeouts (decrease before increase).
- Do not exceed endpoint configured `max_parallel` even if measured burst looks higher.
- Preserve isolated worktree behavior and non-interactive operation.
- Keep local-first routing active; hosted models are fallback only after local saturation/unhealthy state.
