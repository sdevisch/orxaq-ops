# Local + Hosted Model Network Resilience Plan

## Objective
Build a resilient hybrid autonomy runtime where local network models, hosted models, and Codex lanes keep working under endpoint failures, coordinator outages, and network topology changes while preserving safety, queue integrity, and issue/branch hygiene.

## Constraints
- Non-interactive execution only.
- No destructive git recovery.
- Deterministic queue semantics (idempotent claim state, append-safe ingestion).
- Security-first defaults (input validation, bounded parsing, explicit ownership checks).

## Failure Modes Addressed
1. Local endpoints not receiving prompts consistently.
2. Context windows underutilized (token caps lower than endpoint capability).
3. Compute under-saturation (parallel limits too conservative).
4. Fragile behavior when network topology changes.
5. Coordinator/supervisor offline stalls direct task dispatch.
6. Lanes idle when direct prompts stop even though backlog exists.

## Implemented Controls

### 1) Runner endpoint resilience
- Health-aware endpoint selection using fleet probe status.
- Inflight-aware endpoint load balancing (least-loaded candidate preference).
- Per-endpoint failure cooldown with exponential backoff.
- Local model selection still respects model compatibility and deterministic fallback order.

### 2) Context and capacity maximization
- Dynamic context default utilization increased to 95% (`ORXAQ_LOCAL_OPENAI_CONTEXT_FRACTION=0.95` default).
- Lane env propagation now defaults to 95% context fraction when endpoint context is discovered.
- Fleet capability scan expanded:
  - higher endpoint `max_parallel` defaults in config,
  - deeper context token probe steps.

### 3) Durable task queueing (coordinator-outage resilience)
- Added lane runner queue ingestion:
  - `--task-queue-file`
  - `--task-queue-state-file`
- Supports JSON array/object and NDJSON queue formats.
- Claimed-task dedupe persists in queue state.
- Queue modes run persistently: runner waits for new queued work instead of exiting when known tasks are done.

### 4) Always-work-backlog behavior
- Local idle guard now supports backlog recycling when no direct runnable work exists.
- Reopens done/blocked backlog tasks with bounded controls and delay.
- Prevents starvation windows where healthy local capacity sits idle.

### 5) Queue-aware lane startup
- Idle guard inspects per-lane queue depth and starts lanes when unclaimed queue work exists.
- This removes dependence on coordinator availability for lane activation.

### 6) Process supervision hardening
- Watchdog now also monitors/restarts autonomy supervisor process in addition to idle guard and remote heartbeat daemons.

## Security and Safety Controls
- Queue input is schema-normalized and bounded (id length, text truncation safeguards).
- Unsupported owners are rejected; owner inference is explicit and constrained.
- Claimed queue state is compacted with max entry bounds (`ORXAQ_TASK_QUEUE_MAX_CLAIMED`).
- Endpoint failure data is in-memory operational state only; no sensitive secrets emitted.
- Existing git and non-interactive safeguards remain in place.

## Parallelism Strategy (Local + Hosted)
- Local lanes:
  - endpoint-aware parallel limits continue to be applied by manager,
  - runner now avoids repeatedly hammering unhealthy endpoints.
- Hosted lanes:
  - unchanged fallback pathways stay active for Gemini/Claude/Codex as policy allows,
  - local-only lanes still stay local-only where configured.

## Operational Runbook
1. Refresh fleet data:
   - `make local-model-fleet-full-cycle`
2. Start/ensure runtime:
   - `make start`
   - `make lanes-ensure`
3. Start self-healing daemons:
   - `make local-idle-guard-start`
   - `make local-model-watchdog-start`
4. Monitor:
   - `make lanes-status`
   - `make monitor`
   - inspect `artifacts/autonomy/local_models/*`

## Queue Contract
- Queue producer writes task items to each lane queue file (`task_queue_file`).
- Runner ingests new unclaimed items and updates queue claim state file.
- Lanes remain alive (queue-persistent mode) and pick new items when coordinator is offline.

## Success Metrics
- `local_running_after > 0` when healthy endpoints exist.
- Non-zero queue drain rate while coordinator is down.
- Reduced repeated endpoint failures due to cooldown balancing.
- Increased context-token usage aligned to discovered endpoint capacities.
- Higher concurrent lane utilization without exceeding endpoint limits.

## Rollback
- Disable queue persistence by setting `ORXAQ_TASK_QUEUE_PERSISTENT_MODE=0`.
- Disable backlog recycle in `config/local_model_idle_guard.json`.
- Restore prior capacity settings in `config/local_model_fleet.json`.
- Revert manager/runner queue args if strict static tasks-only mode is needed.
