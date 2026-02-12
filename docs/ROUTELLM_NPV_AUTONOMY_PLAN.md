# RouteLLM + NPV Autonomy Plan

Last updated: 2026-02-09

## Objective

Add RouteLLM-based model routing to the Orxaq autonomy control-plane and scale agent/subagent capacity only when expected marginal NPV is positive and above policy threshold.

## Why this profile

- Improve throughput on high-value work by assigning stronger models only when justified.
- Reduce average delivery cost by routing lower-risk tasks to lower-cost models.
- Prevent uncontrolled parallelism by requiring economic justification before scale-up.

## Scope

In scope:
- RouteLLM integration points in `orxaq-ops` manager/runner.
- NPV policy for routing and parallel-capacity decisions.
- Dashboard/status telemetry for route and scale decisions.
- Multi-agent task orchestration updates and independent tests.

Out of scope (for first rollout):
- Permanent always-on auto-scaling without stop-loss.
- Removing static model fallback paths.

## Target architecture

1. Routing gateway layer
- Route eligible model requests through RouteLLM endpoint.
- Preserve deterministic owner/model fallback when router is unavailable.

2. Economic policy layer
- Compute expected marginal NPV for:
  - choosing stronger vs weaker model,
  - launching additional lane/subagent capacity.
- Use policy thresholds and budget ceilings as hard gates.

3. Capacity control layer
- Apply scale-up only when:
  - marginal NPV >= minimum threshold,
  - current spend is within budget,
  - reliability and quality metrics are healthy.
- Scale down or freeze when stop-loss conditions trigger.

4. Observability layer
- Emit routing and scaling decision events with rationale.
- Track realized cost, latency, and validation outcomes against predicted value.

## NPV policy baseline

Use a configurable approximation:

`marginal_npv_usd = expected_value_uplift_usd - expected_incremental_cost_usd - uncertainty_penalty_usd`

For multi-day value realization:

`npv_usd = sum_t((value_t - cost_t) / (1 + r)^(t/365))`

Where:
- `r` is annual discount rate.
- `t` is days from decision time.

Scale-up rule:
- Launch extra agent/subagent capacity only if `marginal_npv_usd >= min_npv_threshold_usd`.
- Respect:
  - `daily_budget_usd`,
  - `max_parallel_agents`,
  - `max_subagents_per_agent`.

## Phased rollout

### Phase 0: Baseline instrumentation
- Fill real model pricing in `config/pricing.json`.
- Confirm exact token/cost coverage in metrics summary.
- Capture baseline throughput/cost for comparison.

### Phase 1: RouteLLM routing integration
- Add router configuration and health checks.
- Add fallback routing and deterministic degradation behavior.
- Validate with adversarial tests (timeouts, malformed responses, overload).

### Phase 2: NPV gate for model and capacity choices
- Implement policy inputs and thresholds.
- Add decision logging and dashboard counters.
- Validate no scale-up on negative/uncertain marginal NPV.

### Phase 3: Controlled lane/subagent expansion
- Enable additional capacity decisions behind policy.
- Add stop-loss rollback on budget or quality regressions.
- Run sustained autonomy cycle and compare against baseline KPIs.

## Task queue and objective files

- Tasks: `config/lanes/codex_routellm_npv_tasks.json`
- Objective: `config/objectives/codex_routellm_npv.md`
- Prompt: `config/prompts/codex_routellm_npv_prompt.md`
- Context template: `config/mcp_context.routellm_npv.example.json`

## Full-autonomy launch profiles

Supervisor profile (uses `orxaq-autonomy`):

```bash
make routellm-preflight
make routellm-bootstrap
make routellm-start
make routellm-status
```

Codex isolated worktree profile (uses codex-autonomy operator):

```bash
make routellm-full-auto-discover
make routellm-full-auto-prepare
make routellm-full-auto-dry-run
# then:
make routellm-full-auto-run
```

## Stop-loss and rollback

Trigger rollback or freeze capacity if any condition holds:
- Daily spend exceeds policy budget.
- Validation pass rate regresses below accepted baseline.
- Router health is unstable and fallback rate exceeds threshold.
- Mean cycle time increases without corresponding value uplift.

Rollback actions:
- Disable RouteLLM path and return to static owner/model selection.
- Disable dynamic scale-up and cap at baseline lane counts.
- Continue with existing deterministic autonomy flow.
