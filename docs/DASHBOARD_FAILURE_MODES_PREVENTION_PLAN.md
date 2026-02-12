# Dashboard Failure Modes And Prevention Plan

## Scope
- Target: `src/orxaq_autonomy/dashboard.py` and dashboard lifecycle paths in `manager.py`/`cli.py`.
- Goal: prevent dashboard outages, bad operator decisions from stale/incorrect data, and noisy failure loops.

## Top Priorities
- `P0`: Availability/data-integrity failures that can hide incidents or block operator control.
- `P1`: Severe degradations (stale or misleading telemetry, restart churn, broken control actions).
- `P2`: Quality/performance/security hardening with lower immediate blast radius.

## 50 Failure Modes With Preventive Controls
1. `P0` Client disconnects trigger `BrokenPipeError` storm and pollute logs.
Control: classify and suppress expected disconnect write errors at response boundary.
2. `P0` JSON serialization failure returns HTML/plaintext fallback and breaks UI parser.
Control: strict JSON-only API responses with guarded error envelope.
3. `P0` Single failing endpoint crashes request handler thread.
Control: per-endpoint safe wrappers (`_safe_*`) with bounded fallback payloads.
4. `P0` `/api/monitor` latency spikes block refresh loop.
Control: timeout budgets and stale-cache fallback with freshness marker.
5. `P0` Port bind race leaves dashboard unavailable after restart.
Control: bind-with-scan and explicit startup failure status.
6. `P0` PID/meta drift causes false “running” state.
Control: validate PID liveness and build id before declaring healthy.
7. `P0` Monitor snapshot schema drift breaks frontend rendering.
Control: contract tests for required keys/defaults and safe coercion.
8. `P0` Lane action endpoint executes invalid action silently.
Control: strict action enum validation and HTTP status mapping.
9. `P0` Stale lane cache shown as live health.
Control: include source freshness timestamps + stale badges in all panes.
10. `P0` Conversation source mismatch hides lane-specific errors.
Control: lane-id strict filtering and source-health rollup.
11. `P1` Missing files (`state`, `monitor`, `routing`) crash endpoint handlers.
Control: “file missing” recoverable payload + diagnostics source error.
12. `P1` Large log files cause memory spikes on read.
Control: bounded tail reads and hard line caps.
13. `P1` Query params with huge integers degrade server.
Control: bounded int parser with min/max clamping.
14. `P1` Boolean query parsing ambiguity yields wrong behavior.
Control: normalized truthy/falsey parser with defaults.
15. `P1` Lane filter case mismatch hides real lane data.
Control: case-insensitive lane-id normalization.
16. `P1` Unknown lane in filter returns empty silently.
Control: explicit “unknown lane” feedback in payload.
17. `P1` Partial conversation source reported as full success.
Control: source state model (`ok|degraded|unreported`) + fallback counts.
18. `P1` Dashboard refresh overlapping requests cause thundering herd.
Control: client-side fetch timeout and debounced refresh cadence.
19. `P1` Slow watchdog history parse blocks UI.
Control: cap events and tail-only ndjson parsing.
20. `P1` Lane action HTTP status mapping inconsistent with payload status.
Control: centralized `_lane_action_http_status` with tests.
21. `P1` Dead lane shown healthy due stale heartbeat interpretation.
Control: strict heartbeat age thresholds and explicit health confirmation age.
22. `P1` Commit/activity metrics fail when git unavailable.
Control: resilient git command wrappers with sentinel values.
23. `P1` Distributed todo parsing error wipes panel.
Control: parse guard + minimal empty-task fallback snapshot.
24. `P1` Routing decision feed parse errors collapse routing panel.
Control: malformed-line skip with source_error counter.
25. `P1` Dashboard process restarts into bad cwd and writes to `/artifacts`.
Control: absolute artifacts paths in env + startup cwd validation.
26. `P1` Supervisor/watchdog duplicate processes race on pid/lock files.
Control: single-owner lock and restart cooldown with duplicate kill policy.
27. `P1` Keepalive/watchdog relaunch stale build unexpectedly.
Control: build-id pin and explicit process ownership tags.
28. `P1` Endpoint returns 500 without actionable reason.
Control: structured error envelope (`ok=false,error,source`).
29. `P1` Mixed Python versions produce divergent behavior.
Control: runtime banner in status snapshot (`python_version`, executable path).
30. `P1` Dashboard status false-positive when runner died recently.
Control: heartbeat recency + pid liveness + last event recency synthesis.
31. `P2` XSS risk from unescaped text in UI.
Control: enforce `escapeHtml` for all rendered dynamic content.
32. `P2` Large lane table render causes UI jank.
Control: row cap + filter-first render path.
33. `P2` Metrics math divide-by-zero yields NaN in UI.
Control: safe denominators and numeric coercion helpers.
34. `P2` Routing cost metrics drift due missing pricing entries.
Control: default pricing sentinel and explicit “unknown model cost”.
35. `P2` Unicode/encoding decode errors on logs.
Control: utf-8 decode with replacement and source error counter.
36. `P2` Path traversal attempt through query/path handling.
Control: no user-controlled filesystem paths in API handlers.
37. `P2` CSRF-like accidental lane actions from local browser extensions.
Control: require explicit action parameter + optional local token gate.
38. `P2` High-frequency logs bloat disk.
Control: structured logging levels + rotation recommendations.
39. `P2` Timezone confusion in operator decisions.
Control: include UTC and local timestamps consistently.
40. `P2` Routing panel interprets stale data as fresh.
Control: per-source freshness age in summary.
41. `P2` Watchdog state from wrong location chosen.
Control: deterministic path precedence and path shown in payload.
42. `P2` Conversation rollup double-counts cross-lane events.
Control: lane-id keyed rollup and dedupe by event identity.
43. `P2` Invalid lane entries corrupt owner counts.
Control: normalize lane entries and skip invalid with warnings.
44. `P2` Git command hangs stall endpoint.
Control: subprocess timeout for git-derived metrics.
45. `P2` Dashboard HTML changes break scripts/tests silently.
Control: high-signal HTML contract tests for critical ids/labels.
46. `P2` API endpoint growth creates undocumented behavior.
Control: endpoint contract doc + smoke tests per endpoint.
47. `P2` Silent fallback masks chronic source failures.
Control: expose `suppressed_source_errors` counters and alert thresholds.
48. `P2` Lane control actions happen while source unhealthy.
Control: pre-action guard requiring recent lane snapshot.
49. `P2` Excessive retry loops on failing task classes.
Control: cooldown/backoff surfaced in dashboard with actionable recommendations.
50. `P2` Missing regression coverage for disconnect/write failures.
Control: targeted unit tests for disconnect classifier and response guard paths.

## Prioritized Execution Waves
1. Wave 1 (`P0`): response-write disconnect guard, API envelope consistency, stale-cache/freshness surfacing, pid/meta truthfulness checks.
2. Wave 2 (`P1`): snapshot hardening for file/read/parse failures, lane action/status correctness, watchdog/supervisor duplicate-process prevention.
3. Wave 3 (`P2`): performance/security hardening, endpoint contract docs, expanded regression tests and observability.

## Immediate Actions Executed
- Implemented disconnect/write failure guard in dashboard response path.
- Added regression tests for disconnect error classifier.
- Fixed lane validation command wrapper (`env PYTHONPATH=src ...`) to avoid command-not-found execution path.

## Validation Gates
- `pytest -q tests/test_autonomy_dashboard.py`
- `pytest -q tests/test_autonomy_manager.py`
- `python3 -m orxaq_autonomy.cli dashboard-status`
