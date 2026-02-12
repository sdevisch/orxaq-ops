# Distributed Coordinator HA Plan (DMN + Execution DAG + Causal DAG)

Last updated: 2026-02-10

## Objective

Make autonomous execution resilient when one coordinator node fails by replacing the single-host control loop with a quorum-backed distributed coordinator set, while preserving deterministic decisioning through DMN-style rules and graph-driven execution.

## Why This Plan

Current `orxaq-ops` runtime already has:
- local process supervision (`manager.py`, watchdog launch agent)
- event mesh primitives (`src/orxaq_autonomy/event_mesh.py`)
- queue-persistent lane execution (`docs/LOCAL_MODEL_NETWORK_RESILIENCE_PLAN.md`)

But control decisions are still operationally concentrated. If the coordinator host is offline, orchestration quality and recovery degrade.

This plan introduces a three-graph control architecture:
1. `DMN graph` for deterministic policy decisions.
2. `Execution DAG` for dependency-safe task scheduling.
3. `Causal DAG` for diagnosis and intervention selection.

## Architecture Overview

### 1) Coordinator quorum (control-plane HA)

Run 3 coordinator candidates on always-on LAN nodes (minimum). Exactly one leader is active at a time.

- Consensus/lease backend options:
  - `etcd` (preferred)
  - PostgreSQL advisory lock + lease rows (fallback)
- Lease key: `orxaq/control/leader`
- Lease fields:
  - `leader_id`
  - `epoch` (monotonic fencing token)
  - `lease_expires_at`

Only the active leader can emit mutating commands (start/stop/scale/retry).

### 2) DMN decision architecture (policy plane)

Adopt DMN-style decision tables for routing/scaling/recovery policy evaluation.

- Inputs (facts):
  - lane heartbeat age
  - queue depth / backlog growth
  - endpoint health/cooldown
  - budget constraints
  - recent failure signatures
  - operator policy flags
- Outputs (decision):
  - action (`hold`, `scale_up`, `scale_down`, `restart_lane`, `reroute`, `isolate_node`)
  - target scope (`lane_id`, owner class, or `all_enabled`)
  - guardrails (max parallel, cooldown window, retry budget)

Implementation direction:
- Keep decision evaluation deterministic and side-effect free.
- Version decision tables (`decision_table_version`) and stamp every command with that version.
- Evaluate in all coordinators; only leader publishes results.

### 3) Execution DAG (workflow plane)

Model task execution as DAGs per lane or objective bundle.

- Node: executable task unit with `task_id`, owner, resources, timeout.
- Edge: strict dependency (`A -> B` means B cannot run until A commits outcome).
- Scheduler behavior:
  - maintain `ready set` from topological frontier
  - claim nodes with lease+epoch stamped lock
  - requeue only on retryable failure class

Resilience behavior:
- If leader fails, new leader reconstructs DAG frontier from durable state and resumes scheduling.
- Workers are idempotent using `(task_id, attempt, epoch)` dedupe keys.

### 4) Causal DAG (diagnosis + intervention plane)

Use causal DAGs to avoid reactive, non-causal remediation loops.

- Nodes represent system variables (examples):
  - endpoint saturation
  - queue age
  - lane failure rate
  - routing fallback rate
  - token budget pressure
- Edges encode domain causality assumptions.

Policy use:
- Before disruptive intervention, run identifiability/adjustment checks when available.
- Prefer actions that target likely causes, not correlated symptoms.
- Record `causal_hypothesis_id` and evidence in every high-impact control action.

## Unified Command/Event Contract

Every mutating coordinator command must include:
- `command_id`
- `leader_epoch`
- `decision_table_version`
- `execution_dag_id`
- `causal_hypothesis_id` (optional but required for disruptive actions)
- `issued_at_utc`

Worker acceptance rule:
- Reject command if `leader_epoch` is older than last-seen epoch for that command stream.

This is the split-brain fence.

## Failover Semantics

### Leader loss

1. Followers detect lease expiry.
2. One follower acquires lease with `epoch = previous + 1`.
3. New leader rebuilds state from durable stores:
   - task queue ledger
   - claim/attempt ledger
   - DAG node status store
4. New leader resumes dispatch with new epoch.
5. Workers reject stale commands from prior epoch.

### Partial network partition

- Quorum side continues.
- Minority side cannot renew lease and is demoted to observer mode.
- Any stale-leader command is fenced at worker by epoch check.

## Storage Model

Use durable append-only logs plus compacted state snapshots.

- `events`: append-only NDJSON/JSONL stream (already aligned with `event_mesh`).
- `commands`: append-only log with epoch stamp.
- `claims`: compacted map keyed by `task_id` and latest attempt.
- `dag_state`: compacted node state (`pending|ready|running|success|failed|blocked`).

Retention policy:
- Keep full logs for audit window.
- Periodically compact to snapshots for fast leader recovery.

## Mapping to Existing `orxaq-ops`

### Existing assets to keep
- `src/orxaq_autonomy/event_mesh.py` event ingestion/export path
- lane queue persistence and claim state
- watchdog/health pipeline and dashboard

### New control-plane additions
1. `src/orxaq_autonomy/leader_lease.py`
   - lease acquire/renew/release
   - monotonic epoch handling
2. `src/orxaq_autonomy/dmn_engine.py`
   - deterministic decision table evaluator
3. `src/orxaq_autonomy/dag_scheduler.py`
   - topological frontier scheduling and replay
4. `src/orxaq_autonomy/causal_decision_bridge.py`
   - policy gate for high-impact actions

### Manager integration points
- `manager.ensure_background` should publish/consume epoch-stamped commands.
- `event_mesh` dispatch should support command topics with epoch fence.
- existing `scaling.decision.*` events become DMN-produced decision events.

## Rollout Phases

### Phase 0: Safety prep
- Add command epoch fields and worker-side fence checks (no behavior change yet).
- Add durable command log and replay test harness.

### Phase 1: HA lease-only coordinator
- Run 3 coordinator replicas.
- Keep current decision logic; only leadership becomes distributed.

### Phase 2: DMN policy extraction
- Move scaling/routing/restart choices into versioned decision tables.
- Add explain traces for each decision (`matched_rules`, `inputs_hash`).

### Phase 3: Execution DAG scheduler
- Migrate queue consumption from linear task pull to DAG frontier scheduling.
- Enable replay from durable DAG state after failover.

### Phase 4: Causal DAG intervention gate
- Require causal hypothesis metadata for disruptive controls.
- Add causal scorecards to dashboard (`hypothesis`, `evidence`, `outcome`).

## Reliability SLO Targets

- Coordinator failover RTO: <= 30 seconds
- Lost command RPO: 0 committed commands
- Duplicate execution rate: < 0.1% of tasks (and all duplicates idempotent)
- Split-brain accepted commands: 0

## Chaos Test Matrix

Run continuously in staging lane set:
1. Kill active leader process.
2. Hard power-off current leader node.
3. Partition one coordinator from quorum.
4. Delay/duplicate command delivery.
5. Corrupt one coordinator local cache and verify replay recovery.

Pass criteria:
- leader election converges
- DAG progress continues
- no stale-epoch command accepted
- causal/DMN logs remain auditable

## Operational Guidance

- Minimum topology: 3 coordinator nodes (or 2 + witness).
- Keep workers stateless aside from idempotency cache and heartbeats.
- Keep decision artifacts (DMN tables, DAG specs, causal graphs) versioned in git and mirrored to runtime artifact store.
- Treat DMN/DAG/causal graph updates as controlled releases with rollback hashes.

## Open Design Decisions

1. Lease backend: `etcd` vs Postgres lease table.
2. DMN runtime source:
   - native `orxaq-ops` evaluator, or
   - shared `orxaq` CDOG decision-table runtime integration.
3. DAG granularity:
   - one DAG per lane objective, or
   - one DAG per distributed todo cycle.
4. Causal gate strictness:
   - advisory mode first, then enforce mode for disruptive actions.
