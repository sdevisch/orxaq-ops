# State-of-the-Art Routing and Autonomy Plan

Last updated: 2026-02-09

## Goal

Operate a multi-agent autonomy fabric that routes each task to the best model for cost, speed, and quality while keeping spend, reliability, and delivery throughput observable in real time.

## Target outcomes

- Intelligent model routing active for Codex, Gemini, and Claude lanes.
- Routing decisions based on objective scoring (cost/speed/quality), not static model pins.
- Dashboard exposes:
  - estimated token usage,
  - blended estimated cost per 1M tokens,
  - per-provider routing/error rates,
  - per-provider estimated cost per 1M tokens,
  - lane-level routing behavior and fallback/error pressure.
- Full-autonomy loop can execute unattended with deterministic validation gates.

## Architecture

### 1. Router control plane

- Use local RouteLLM-compatible routers (`fast`, `strong`) with shared model catalog metadata:
  - estimated input/output cost per million tokens,
  - speed estimate (tokens/sec),
  - quality score,
  - context limits,
  - local vs hosted origin.
- Expose deterministic endpoints:
  - `GET /health`
  - `POST /route`

### 2. Objective-driven selection

- Resolve objective automatically from task shape:
  - low-risk/test/docs -> `cost_speed`
  - mixed tasks -> `balanced`
  - high complexity/architecture/security -> `quality`
- Compute weighted score per candidate model:
  - normalized cost, speed, quality
  - context penalty near/over context window
  - profile bias (`fast` vs `strong`)
- Preserve safe fallback behavior when provider/policy/router is degraded.

### 3. Economics telemetry

- Aggregate metrics at summary layer:
  - estimated tokens total,
  - total estimated spend,
  - blended estimated cost per 1M tokens,
  - provider-level tokens/spend/cost-per-1M,
  - lane/provider routing fallback and router-error rates.

### 4. Dashboard and operations

- Routing Monitor tab is first-class.
- Keep detection states:
  - `healthy`, `elevated_fallback`, `degraded`, `idle`.
- Add operator controls/runbook to restart lanes and router services quickly.

## Rollout phases

1. Foundation
- Expand policy and pricing catalogs for all accessible model families.
- Enable intelligent routing for active lanes.

2. Economics hardening
- Validate blended cost-per-1M and token estimates against known runs.
- Add alert thresholds for fallback and router error rates.

3. Adaptive policy
- Auto-tune objective weights based on observed acceptance quality and latency.
- Apply guardrails so cost optimization never degrades critical-quality lanes.

4. Continuous autonomy
- Run unattended loops with bounded retries and required validation gates.
- Emit concise machine-readable results after each cycle.

## Validation gates

Run before and after each substantial routing change:

- `make lint`
- `make test`
- `make version-check`
- `make repo-hygiene`
- `make hosted-controls-check`

## Risks and mitigations

- Price estimate drift:
  - maintain pricing as explicit estimates and refresh on schedule.
- Over-optimization to speed:
  - keep quality floor and fallback protections.
- Router outage:
  - deterministic static fallback path with explicit telemetry.

## Full-autonomy command profile

- Discover: `make routing-sota-full-auto-discover`
- Prepare: `make routing-sota-full-auto-prepare`
- Dry run: `make routing-sota-full-auto-dry-run`
- Execute: `make routing-sota-full-auto-run`
