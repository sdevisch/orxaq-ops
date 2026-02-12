# Dashboard Improvement Plan

## Goals
1. Ensure distributed to-do activity volume is accurate and trustworthy.
2. Keep dashboard refresh responsive under large artifact logs.
3. Improve visual clarity and accessibility for high-signal widgets.
4. Harden API fallback behavior and stale-data annotations.
5. Raise automated test coverage for dashboard activity paths.

## Findings From Review
1. Active-watch totals are truncated to UI list length, causing summary/count drift under larger queues.
2. Routing activity ingest currently reads full JSONL history each refresh and scales poorly with log growth.
3. Activity feed semantics blend source types without explicit source class labels in summary KPIs.
4. Widget contrast and density are strong but need accessibility verification across small-screen breakpoints.
5. Dashboard tests pass broadly but need explicit cases for new distributed-to-do aggregation branches.

## Autonomous Swarm Plan (5 low-level agents)
1. `codex-dashboard-todo-metrics-fix`: normalize totals vs rendered list and coverage math.
2. `codex-dashboard-todo-perf-tail`: optimize activity ingestion using bounded tail reads.
3. `codex-dashboard-ui-accessibility`: tune hero widget spacing/contrast/mobile behavior.
4. `codex-dashboard-api-resilience`: improve stale/partial/error signaling behavior.
5. `codex-dashboard-test-expansion`: add tests for new todo aggregation and widget rendering paths.

## Coordination Rules
1. One lane, one focused concern, minimal overlap.
2. Shared file edits must be small and rebased frequently.
3. Every lane runs validation before handoff.
4. Use handoff artifacts for blockers and unresolved decisions.
