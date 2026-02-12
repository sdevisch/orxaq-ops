# Event-Driven Redesign Plan (Decentralized + GitHub Coordination)

## Objective

Redesign `orxaq-ops` to support real-time event-driven monitoring, scheduling, and routing while preserving GitHub as the coordination backbone and removing central runtime dependencies.

## Architecture Targets

1. Every node (for example `.86`, local workstation, CI runner) can:
   - ingest local/remote events
   - make local scheduling/routing decisions
   - continue operating when disconnected
2. GitHub remains the cross-node convergence medium via git-backed event ledgers.
3. No node depends on one always-on central scheduler/router service.

## Event Mesh Model

### Event Envelope

- `event_id` (deterministic hash)
- `timestamp` (UTC ISO-8601)
- `topic` (`monitoring`, `scheduling`, `routing`, `coordination`, etc.)
- `event_type` (for example `task.enqueued`, `route.requested`)
- `node_id` (emitter identity)
- `causation_id` (optional upstream event)
- `source` (`runtime`, `mesh-dispatch`, `cli`, etc.)
- `payload` (event-specific object)

### Event Storage

- Local append-only stream:
  - `artifacts/autonomy/event_mesh/events.ndjson`
- Local processing state:
  - `state/event_mesh/dispatch_cursor.json`
  - `state/event_mesh/dispatch_seen.json`
  - `state/event_mesh/export_seen.json`

### GitHub Coordination Ledger

- Shared ledger path:
  - `state/github_coordination/event_mesh/outbox/<node_id>/<event_id>.json`
- Node capability manifests:
  - `state/github_coordination/event_mesh/nodes/<node_id>.json`

Nodes export local events to outbox files and import remote events by reading the outboxes after git pull.

## Migration Strategy

### Phase 0 (Now): Stabilize and Baseline

- Keep existing manager/runner behavior intact.
- Add event mesh primitives and CLI operations.
- Establish event contract and file layout.

### Phase 1: Dual-Write Core Signals

- Existing monitoring/scheduling/routing actions emit mesh events in parallel.
- Keep legacy loops as compatibility fallback while validating event consistency.

### Phase 2: Event-Driven Executors

- Replace loop-triggered operations with event consumers:
  - heartbeat changes -> schedule ticks
  - task queued -> route requested
  - route selected -> task dispatch attempted

### Phase 3: Full Decentralized Operation

- Nodes operate independently with eventual convergence through GitHub ledger sync.
- Supervisor loops become optional watchdogs, not central dependencies.

## Operational Flow (Target)

1. `mesh-import`: pull remote events from GitHub-ledger files.
2. `mesh-dispatch`: process unseen events and emit follow-up events.
3. `mesh-export`: publish new local events back to ledger.
4. `git add/commit/push`: share coordination state.
5. Peer nodes `git pull` and repeat.

## Reliability Controls

1. Deterministic event IDs for dedupe.
2. Seen-sets and cursor checkpoints for idempotent replay.
3. Local-first operation with offline continuity.
4. Causation links for traceable decision chains.

## Security/Governance Notes

1. No secrets in event payloads.
2. Coordination files are auditable and code-reviewed via normal GitHub workflow.
3. Node manifests disclose capabilities, not credentials.

## Implementation in This Change

1. New module: `src/orxaq_autonomy/event_mesh.py`.
2. New CLI commands:
   - `mesh-init`
   - `mesh-publish`
   - `mesh-dispatch`
   - `mesh-import`
   - `mesh-export`
   - `mesh-sync`
   - `mesh-status`
3. Tests:
   - `tests/test_event_mesh.py`
   - CLI coverage additions in `tests/test_autonomy_cli.py`
   - manager mesh-emission coverage additions in `tests/test_autonomy_manager.py`
4. Phase-1 dual-write bridge:
   - manager and lane lifecycle paths now emit mesh events (`supervisor.*`, `lanes.*`, `lane.*`) while preserving existing loop-based runtime behavior.
5. Event-first routing/scaling handlers in mesh dispatcher:
   - `routing.route.requested -> routing.route.selected|routing.route.blocked`
   - `scheduling.lanes.*.summary -> scaling.decision.requested -> scaling.decision.made`
6. Event-command actuator bridge:
   - `scaling.decision.made -> scaling.command.requested`
   - manager consumes command events and performs lane start/stop actions with policy-based lane selection when target lane is unspecified.

## Planned Next Steps

1. Move route/scaling decisions into explicit `routing.*` event handlers.
2. Add automated GitHub-sync workflow command (safe commit batching + pull/rebase retry).
3. Add per-topic schema validation and compatibility versioning.
