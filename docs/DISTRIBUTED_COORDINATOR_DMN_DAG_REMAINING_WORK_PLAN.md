# Distributed Coordinator HA Remaining Work Plan

Last updated: 2026-02-10

## Scope Baseline

Already complete:
- Phase 0 command fencing foundations:
  - command metadata (`command_id`, `leader_epoch`, decision/DAG/causal fields)
  - stale-epoch rejection in manager command consumer
  - durable command outcome log
- Initial leader lease plumbing:
  - local lease file (`leader_lease.json`)
  - epoch propagation to `scaling.decision.requested`
  - follower command suppression at `decision.made -> command.requested`

Remaining work below focuses on distributed production readiness, DMN policy extraction, DAG scheduling, and causal intervention governance.

## Workstreams

## 1) Lease Backend Hardening (Phase 1 completion)

Goals:
- Replace local-file lease with backend abstraction.
- Support quorum-safe production mode while keeping local fallback mode.

Deliverables:
- `leader_lease` backend interface:
  - `file` backend (existing behavior)
  - `etcd` backend (preferred)
  - optional `postgres` backend
- Configuration contract:
  - `ORXAQ_AUTONOMY_LEADER_LEASE_BACKEND`
  - backend-specific connection/env vars
- Retry + timeout policy for lease renew/acquire.
- Explicit observer mode when lease backend unavailable.

Acceptance:
- Only one leader can issue mutating commands under normal operation.
- Follower nodes remain read-only and continue telemetry export.

## 2) Manager Leadership Integration

Goals:
- Enforce leader role consistently in all mutating control paths.

Deliverables:
- Leader gate in manager actuation paths (`ensure/start/stop lane` actions from mesh commands).
- Epoch included in manager-emitted scaling and lane control events.
- Unified command status transitions:
  - `applied`
  - `noop`
  - `rejected_*`
  - `actuation_failed`

Acceptance:
- Stale/split-brain commands are never applied.
- Command log supports replay/audit of every command decision.

## 3) DMN Decision Engine Extraction (Phase 2)

Goals:
- Move scaling/routing/restart policy from ad-hoc logic into versioned decision tables.

Deliverables:
- `dmn_engine.py` with deterministic table evaluation.
- Versioned decision tables in config (JSON/YAML).
- Explain output:
  - matched rule ids
  - normalized inputs hash
  - decision table version
- Adapter from current event payloads to DMN facts.

Acceptance:
- Existing behavior preserved under baseline decision table.
- Decision trace is emitted on every `decision.made`.

## 4) Execution DAG Scheduler (Phase 3)

Goals:
- Transition from linear queue pull to dependency-aware DAG frontier scheduling.

Deliverables:
- `dag_scheduler.py`:
  - DAG ingest/validate
  - frontier computation
  - node claim/retry state updates
- Durable DAG state model:
  - `pending|ready|running|success|failed|blocked`
- Idempotent worker claim contract:
  - `(task_id, attempt, leader_epoch)` dedupe

Acceptance:
- Leader failover recovers active DAG state without duplicate side effects.
- DAG execution continues from latest persisted frontier.

## 5) Causal DAG Intervention Gate (Phase 4)

Goals:
- Prevent disruptive interventions based purely on correlation.

Deliverables:
- `causal_decision_bridge.py` for high-impact controls.
- Causal hypothesis metadata required for disruptive actions:
  - `restart_many`, `isolate_node`, aggressive scale-down.
- Emit causal evidence summary into command records.

Acceptance:
- Disruptive controls require causal metadata (advisory then enforced mode).
- Dashboard can show hypothesis/evidence/outcome for interventions.

## 6) Observability + Dashboard

Goals:
- Make control-plane decisions and failover behavior operator-visible.

Deliverables:
- Dashboard additions:
  - leader identity + epoch
  - lease age/ttl
  - command outcomes by status
  - DMN decision traces and causal tags
- CLI status endpoints for lease + command fence state.

Acceptance:
- Operator can determine leader/follower roles and command fence health in < 30s.

## 7) Reliability Validation and Chaos

Goals:
- Verify target behavior under coordinator failures/partitions.

Deliverables:
- Automated chaos scripts/tests for:
  - leader process kill
  - lease expiry takeover
  - stale epoch replay injection
  - delayed command delivery
- SLO assertions:
  - failover RTO <= 30s
  - split-brain accepted commands = 0
  - duplicate side-effect rate bounded and idempotent

Acceptance:
- CI or scheduled validation run emits pass/fail report with evidence artifacts.

## Execution Order

1. Lease backend abstraction (`file` + `etcd`) and leader state API.
2. Manager-wide mutating-path leader gate + epoch propagation.
3. DMN engine + parity table for current scaling behavior.
4. DAG scheduler with replay-safe state transitions.
5. Causal intervention gate and enforcement modes.
6. Dashboard/CLI observability.
7. Chaos harness and SLO gating.

## Completion Criteria

- All mutating control events are leader-gated and epoch-fenced.
- DMN tables own policy logic with explainability.
- DAG frontier scheduling survives leader failover.
- Causal metadata gates disruptive actions.
- Validation suite covers failure and split-brain scenarios.
