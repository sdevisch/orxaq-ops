# Local Model Idle Auto-Recovery Plan

## Problem Statement
The local network model fleet can become fully idle even when endpoints are healthy because lane runtime state and pause controls drift out of sync with active demand.

## Root Causes Observed
1. Local-only lanes were stopped and/or manually paused (`paused.flag`), so `lanes-ensure` intentionally skipped them.
2. Most paused flags were stale manual flags rather than active operator intent.
3. Endpoint health could be good while lane activity was zero, so health checks alone did not restore throughput.
4. Recovery controls existed (`lanes-start`, `lanes-ensure`, heartbeat) but were not fused into a single autonomous guard loop.

## Permanent Fix Strategy
1. Add a dedicated local idle guard daemon that continuously:
- refreshes fleet health/capability telemetry,
- detects idle local lanes while endpoints are healthy,
- clears stale manual pause flags for local-only lanes,
- starts local lanes safely,
- runs `ensure` reconciliation and records outcomes.

2. Add self-healing watchdog supervision for:
- local idle guard daemon,
- remote LM Studio heartbeat daemon,
- autonomy supervisor process.

3. Persist detection history for trend and anomaly analysis:
- cycle report JSON,
- guard state JSON with consecutive-idle counters,
- NDJSON event history.

4. Gate auto-recovery with safety policies:
- local-only lane matching,
- endpoint-health floor,
- stale-pause age threshold,
- max starts per cycle,
- optional lane allow/deny lists.

## Recovery Loop (Operational)
1. Probe endpoints (`local_model_fleet.py probe`) on short interval.
2. Run capability scan on slower interval.
3. Snapshot lane states and filter to local-only lanes.
4. If local fleet is idle and endpoint health is sufficient:
- unpause stale manual local lanes,
- start highest-work local lanes first,
- run `lanes-ensure` post-start.
5. Emit anomaly signals if idle persists beyond threshold cycles.

## Success Criteria
1. At least one local-only lane auto-starts when local fleet is idle and endpoints are healthy.
2. Stale manual pauses no longer cause prolonged hidden starvation.
3. Guard and heartbeat daemons are automatically restarted if they die.
4. Consecutive-idle cycles trend to zero during healthy endpoint windows.

## New Controls Added
1. `scripts/local_model_idle_guard.py`
2. `config/local_model_idle_guard.json`
3. `config/local_model_process_watchdog.json`
4. Make targets:
- `local-idle-guard-once`
- `local-idle-guard-start`
- `local-idle-guard-stop`
- `local-idle-guard-status`
- `local-model-watchdog-once`
- `local-model-watchdog-start`
- `local-model-watchdog-stop`
- `local-model-watchdog-status`

## Forward Risk Controls
1. Keep local backlog tasks populated for local-only lanes to avoid terminal no-work starvation.
2. Monitor repeated `persistent_idle_condition` anomalies and escalate to lane/task refresh.
3. Keep capability scan interval less frequent than probe to reduce overhead while preserving dynamic limits.
4. Continue local-first routing, with hosted fallback only after local saturation/unhealthy states.
