# Local Model Workhorse Autonomy Plan

## Phase 1: Fleet Foundation

- Add endpoint inventory config for all local LM Studio nodes.
- Probe `/v1/models` on each endpoint and publish health/inventory artifacts.
- Add benchmark harness to measure latency/success by endpoint/model.

## Phase 2: Auto Onboarding

- Detect missing target models per endpoint.
- Add optional automated model sync hook (operator-provided command template).
- Run canary benchmarks for newly added models before routing promotion.

## Phase 3: Routing Hardening

- Use local-first routing order with complexity-aware model preferences.
- Keep deterministic fallback chain: local -> cheap hosted -> premium hosted.
- Reserve premium hosted models for deep review/planning/research workloads.

## Phase 4: Parallel Workload Saturation

- Keep multiple local endpoints active with parallel lane execution.
- Use per-endpoint model capability and health to distribute load.
- Keep queue depth by adding follow-on autonomy tasks and lane backlogs.

## Phase 5: Monitoring + Cost Governance

- Surface local fleet health in monitor/dashboard snapshots.
- Track endpoint-level benchmark trends and routing usage in artifacts.
- Use NPV/cost metrics to tune local-vs-hosted saturation thresholds.

## Immediate Execution Checklist

1. Run `make local-model-fleet-full-cycle`.
2. Ensure at least 3 codex local lanes are running concurrently.
3. Verify fresh request activity on each configured local endpoint.
4. Queue and execute `codex-routellm-npv` local-workhorse tasks.
5. Review monitor/dashboard routing + local fleet summaries.
