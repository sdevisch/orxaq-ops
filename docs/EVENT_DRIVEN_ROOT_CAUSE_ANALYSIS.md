# Event-Driven Root Cause Analysis

## Scope

Repository: `/Users/sdevisch/dev/orxaq-ops`  
Issue: [#14](https://github.com/Orxaq/orxaq-ops/issues/14)

## Current Issues (Observed)

1. Monitoring, scheduling, and recovery depend on periodic polling loops (`ensure`, heartbeat polling, monitor loop) instead of event notifications.
2. Runtime control is supervisor-centric, which creates an implicit single control point even when lanes are separate.
3. Cross-node coordination is lane-oriented but not represented as a shared event stream with idempotent replay semantics.
4. GitHub is used for source control and CI but not as an explicit coordination ledger for inter-agent handoff events.
5. Router and scheduler decisions are mostly computed inline, which limits traceability and replay across independent nodes.

## Root Causes

1. **Control Plane Coupling**
   Runtime lifecycle, health checks, and lane orchestration are coupled in manager-centric control flow, so recovery logic is loop-bound.

2. **Lack of Shared Event Contract**
   There is no canonical event envelope for monitoring/scheduling/routing decisions, causing procedural orchestration rather than event processing.

3. **No GitHub-Ledger Abstraction**
   Coordination artifacts exist, but there was no explicit, file-based event ledger model intended for pull/push replication through GitHub.

4. **Insufficient Idempotency Primitives**
   Without event IDs, cursors, and seen-sets as first-class runtime state, safe replay and independent node convergence were limited.

## Corrective Direction

1. Introduce a first-class event mesh module with:
   - canonical event envelope
   - local append-only event log
   - deterministic dispatch cursors
   - idempotent event processing
2. Add GitHub-ledger coordination primitives:
   - outbox export/import via tracked files
   - node manifests for capability discovery
3. Keep existing manager flows functional while progressively migrating features to event handlers.
4. Make each node fully operable standalone:
   - local processing works without remote sync
   - GitHub sync is additive for inter-node convergence, not required for local autonomy
