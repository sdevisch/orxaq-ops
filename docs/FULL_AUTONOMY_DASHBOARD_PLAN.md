# Full Autonomy Process Health Plan

Last updated: 2026-02-09

## Objective

Run fully autonomous lanes continuously with automatic recovery and a live dashboard that makes lane health, throughput, and risk obvious in seconds.

## Success criteria

- Every configured autonomous PID is checked on a fixed cadence.
- Stopped or unhealthy PIDs are restarted automatically with bounded retries.
- Dashboard always shows per-lane:
  - AI owner and work title,
  - runtime duration,
  - latest health confirmation,
  - commits in last hour,
  - live heartbeat + signal activity,
  - anomaly severity with reason.
- Operators can identify a failing lane and why in less than 30 seconds.

## Scope

In scope:
- Process watchdog state and restart orchestration.
- Collab runtime API for lane health, task/push freshness, and commit velocity.
- Real-time dashboard rendering and attention ordering.
- Deterministic validation and restart-safe behavior.

Out of scope:
- Policy changes to task selection logic.
- New agent providers or model families.

## Execution phases

### Phase 1: Telemetry foundation

- Normalize lane runtime signals from heartbeat/events/conversations.
- Track latest successful `task_done` and `auto_push` signals.
- Track commit counts and commit velocity bins for the last hour.

### Phase 2: Recovery controls

- Keep watchdog checks on fixed interval with restart attempts logged.
- Mark lanes degraded when health, signal freshness, or success freshness drifts.
- Keep restart behavior idempotent and auditable.

### Phase 3: Live dashboard operations

- Show lane table with health, throughput, and liveness details.
- Add live heartbeat indicator and moving signal LEDs/sparklines.
- Add anomaly severity score + message and sort by highest risk first.

### Phase 4: Autonomous runbook loop

- Observe: collect watchdog + lane + conversation snapshots.
- Decide: classify `ok/watch/warn/critical` lanes.
- Act: restart or ensure lanes according to severity and cooldown policy.
- Verify: confirm fresh heartbeat and signal after action.
- Report: emit concise action/result entries for operator review.

## Operating policy

- `critical`: lane offline, repeated errors, or stale health for long window.
- `warn`: lane running but degraded signal/success freshness.
- `watch`: mild drift, requires observation but not immediate restart.
- `ok`: healthy and active.

Restart policy:
- Attempt restart only when lane is `critical` or watchdog marks process down.
- Respect backoff/cooldown to prevent restart thrash.
- Escalate to operator only after repeated failed restarts.

## Validation gates

Run on every implementation cycle:

- `make lint`
- `make test`
- `make version-check`
- `make repo-hygiene`
- `make hosted-controls-check`

## Rollout checklist

1. Deploy telemetry changes.
2. Verify dashboard table fields and anomaly ordering.
3. Simulate stopped PID and verify auto-restart + history logging.
4. Simulate stale lane and verify severity escalates to warn/critical.
5. Keep dashboard auto-refresh and stale-cache fallback behavior intact.
