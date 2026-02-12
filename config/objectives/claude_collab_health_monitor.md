# Claude Collaboration Health Monitor

## Objective
Run a continuous Claude-owned observability lane focused on collaboration-system stability, diagnosis, and delegation to lower-cost execution lanes.

## Priority Context
- Tracks top-priority collaboration health request(s) from distributed backlog.
- Focus task: `B0-T4` in `orxaq/ops/backlog/distributed_todo.yaml`.

## Required Outcomes
1. Detect and summarize collaboration degradation:
- lane stoppages, idle/stale behavior, validation bottlenecks, dashboard inconsistencies.
2. Produce concrete diagnosis:
- root cause hypothesis, impacted lanes/swarms, evidence references (file/log/metric).
3. Delegate remediations:
- create/update lower-tier tasks with clear owner, acceptance criteria, and dependency links.
4. Keep observability current:
- ensure dashboard priority tracker reflects open/doing/blocked/done status and live coverage.

## Validation
- `make lint`
- `PYTHONPATH=src python3 -m orxaq_autonomy.cli --root . dashboard-status`
