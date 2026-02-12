# orxaq-ops

[![CI](https://github.com/Orxaq/orxaq-ops/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Orxaq/orxaq-ops/actions/workflows/ci.yml)
[![Release](https://github.com/Orxaq/orxaq-ops/actions/workflows/release-pypi.yml/badge.svg)](https://github.com/Orxaq/orxaq-ops/actions/workflows/release-pypi.yml)

Reusable autonomy control-plane for Orxaq. The autonomy runtime is now a standalone Python package (`orxaq-autonomy`) with protocol-based task execution, optional MCP context ingestion, and cross-platform lifecycle management.

## What Changed

- Autonomy is packaged in `src/orxaq_autonomy` (independent package, reusable outside this repo).
- Runner supports a reusable **skill protocol** (`config/skill_protocol.json`).
- Runner can inject optional **MCP context** (`--mcp-context-file`) into prompts.
- Supervisor/manager is now Python-based and works on macOS + Windows in user space (no admin required).
- IDE launch/open flows are IDE-independent (VS Code, Cursor, PyCharm).

## Layout

- `src/orxaq_autonomy/cli.py` - package CLI (`orxaq-autonomy`).
- `src/orxaq_autonomy/manager.py` - cross-platform supervisor, keepalive, lifecycle.
- `src/orxaq_autonomy/runner.py` - resilient task runner with retries/validation.
- `src/orxaq_autonomy/protocols.py` - skill protocol + MCP context interfaces.
- `src/orxaq_autonomy/ide.py` - workspace generation and IDE launch helpers.
- `skills/orxaq-autonomy-agent/SKILL.md` - reusable skill definition for autonomy workflows.
- `config/skill_protocol.json` - reusable autonomy protocol contract.
- `config/mcp_context.example.json` - sample MCP-style context payload.
- `config/mcp_context.routellm_npv.example.json` - RouteLLM + NPV context template.
- `config/routellm_policy.local-fast.json` - local fast-routing policy template (RouteLLM).
- `config/routellm_policy.local-strong.json` - local strong-routing policy template (RouteLLM).
- `config/litellm_swarm_router.json` - LiteLLM-style router config for OpenAI/Gemini/Claude and LM Studio nodes.
- `config/prompts/codex_impl_prompt.md` - baseline implementation prompt for Codex.
- `config/prompts/codex_routellm_npv_prompt.md` - RouteLLM + NPV autonomy prompt.
- `config/prompts/gemini_test_prompt.md` - baseline independent-test prompt for Gemini.
- `config/prompts/claude_review_prompt.md` - baseline governance/review prompt for Claude.
- `config/lanes.json` - parallel lane plan for Codex/Gemini/Claude with non-overlapping scopes.
- `config/lanes/codex_routellm_npv_tasks.json` - RouteLLM + NPV multi-agent task queue.
- `config/objectives/codex_routellm_npv.md` - RouteLLM + NPV objective.
- `AGENTS.md` - canonical collaboration standard for hybrid human + IDE + API agent workflows.
- `docs/autonomy-halt-mitigation.md` - failure-mode playbook.
- `docs/VSCODE_COLLAB_AUTONOMY_RUNBOOK.md` - end-to-end VS Code + multi-agent operating guide.
- `docs/ROUTELLM_NPV_AUTONOMY_PLAN.md` - rollout plan for routing economics and capacity scaling.
- `docs/release-pypi.md` - trusted-publishing release runbook.
- `scripts/model_router_connectivity.py` - model endpoint connectivity report generator for swarm-health consumption.

Legacy shell scripts remain for compatibility, but `make` now uses the package CLI.

## Setup

```bash
cd /Users/sdevisch/dev/orxaq-ops
cp .env.autonomy.example .env.autonomy
python3 -m pip install -e .
```

Windows PowerShell alternative:

```powershell
cd C:\path\to\orxaq-ops
python -m pip install -e .
```

Set auth and repos in `.env.autonomy`:

- `GEMINI_API_KEY` or `~/.gemini/settings.json`
- `OPENAI_API_KEY` or `codex login`
- `ORXAQ_IMPL_REPO` (default `../orxaq`)
- `ORXAQ_TEST_REPO` (default `../orxaq_gemini`)

Optional reusable context controls:

- `ORXAQ_AUTONOMY_SKILL_PROTOCOL_FILE` (default `config/skill_protocol.json`)
- `ORXAQ_AUTONOMY_MCP_CONTEXT_FILE` (optional MCP-style JSON file)
- `ORXAQ_AUTONOMY_CODEX_PROMPT_FILE` (default `config/prompts/codex_impl_prompt.md`)
- `ORXAQ_AUTONOMY_GEMINI_PROMPT_FILE` (default `config/prompts/gemini_test_prompt.md`)
- `ORXAQ_AUTONOMY_TASKS_FILE` (default `config/tasks.json`)
- `ORXAQ_AUTONOMY_OBJECTIVE_FILE` (default `config/objective.md`)
- `ORXAQ_AUTONOMY_CODEX_CMD` (default `codex`; can be absolute path)
- `ORXAQ_AUTONOMY_GEMINI_CMD` (default `gemini`; can be absolute path)
- `ORXAQ_AUTONOMY_CLAUDE_CMD` (default `claude`; can be absolute path)
- `ORXAQ_AUTONOMY_CONVERSATION_LOG_FILE` (default `artifacts/autonomy/conversations.ndjson`)
- `ORXAQ_AUTONOMY_METRICS_FILE` (default `artifacts/autonomy/response_metrics.ndjson`)
- `ORXAQ_AUTONOMY_METRICS_SUMMARY_FILE` (default `artifacts/autonomy/response_metrics_summary.json`)
- `ORXAQ_AUTONOMY_PRICING_FILE` (default `config/pricing.json`)
- `ORXAQ_AUTONOMY_LANES_FILE` (default `config/lanes.json`)
- `ORXAQ_AUTONOMY_PROCESS_RESTART_COOLDOWN_SEC` (default `30`)
- `ORXAQ_AUTONOMY_PROCESS_STARTUP_GRACE_SEC` (default `8`)
- `ORXAQ_AUTONOMY_PROCESS_WATCHDOG_STATE_FILE` (default `artifacts/autonomy/process_watchdog_state.json`)
- `ORXAQ_AUTONOMY_PROCESS_WATCHDOG_HISTORY_FILE` (default `artifacts/autonomy/process_watchdog_history.ndjson`)
- `ORXAQ_AUTONOMY_FULL_AUTONOMY_REPORT_FILE` (default `artifacts/autonomy/full_autonomy_report.json`)

Lane specs in `config/lanes.json` can override command/model selection per lane:
- `codex_cmd`, `gemini_cmd`, `claude_cmd`
- `codex_model`, `gemini_model`, `claude_model`
- `gemini_fallback_models`

Configure per-model rates in `/Users/sdevisch/dev/orxaq-ops/config/pricing.json` to enable exact response cost tracking.

## Commands

```bash
make preflight
make preflight-autonomy
make bootstrap
make start
make ensure
make status
make monitor
make metrics
make health
make process-watchdog
make full-autonomy
make logs
make model-router-connectivity
make provider-autobootstrap
make cleanup-loop-once
make cleanup-loop-start
make cleanup-loop-status
make cleanup-loop-stop
make dashboard
make dashboard-status
make dashboard-logs
make dashboard-stop
make provider-cost-ingest
make provider-cost-health
make provider-cost-ingest-check
make t1-basic-model-policy-check
make pr-tier-ratio-check
make backend-upgrade-policy-check
make api-interop-policy-check
make backlog-control-once
make backlog-control-start
make backlog-control-status
make backlog-control-stop
make swarm-todo-health-once
make swarm-todo-health-start
make swarm-todo-health-status
make swarm-todo-health-stop
make swarm-todo-health-current-once
make swarm-todo-health-current-start
make swarm-todo-health-current-status
make swarm-todo-health-current-stop
make swarm-health-strict
make swarm-health-operational
make swarm-health-snapshot
make swarm-ready-queue
make swarm-cycle-report
make conversations
make lanes-plan
make lanes-status
make lanes-start
make lanes-ensure
make lanes-stop
make mesh-init
make mesh-status
make mesh-publish
make mesh-dispatch
make mesh-import
make mesh-export
make mesh-sync
make mesh-autonomy-once
make routellm-preflight
make routellm-bootstrap
make routellm-start
make routellm-status
make routellm-full-auto-dry-run
make routellm-full-auto-run
make stop
make install-keepalive
make keepalive-status
make workspace
make open-vscode
make open-cursor
make open-pycharm
make lint
make test
make version-check
make repo-hygiene
make hosted-controls-check
make readiness-check
make readiness-check-autonomy
make bump-patch
make bump-minor
make bump-major
make package
```

Generate the Phase 1 model-router connectivity report:

```bash
python3 scripts/model_router_connectivity.py --config ./config/litellm_swarm_router.json --output ./artifacts/model_connectivity.json
```

Connectivity scoring treats only `required=true` endpoints as strict blockers; optional endpoints are still reported for observability.

Use it from `orxaq` swarm-health (note the connectivity report lives in `orxaq-ops/artifacts`):

```bash
python3 ../orxaq/orxaq_cli.py swarm-health --root ../orxaq --output ../orxaq/artifacts/health.json --strict --connectivity-report ../orxaq-ops/artifacts/model_connectivity.json
```

Preferred convenience targets:

```bash
# active/recent backlog scope (operationally useful)
make swarm-todo-health-current-once

# strict full backlog scan (includes historical worktree backlog files)
make swarm-todo-health-once

# strict swarm-health with correctly wired connectivity report path
make swarm-health-strict

# operational swarm-health (pipeline/runtime focus, excludes quality/security gates)
make swarm-health-operational

# strict+operational health against a temporary clean worktree snapshot
make swarm-health-snapshot

# enforce/monitor T1-model policy for basic coding tasks
# (fails the criterion when telemetry is stale/incomplete even if violations=0)
make t1-basic-model-policy-check

# enforce PR tier-label mix so most PRs are T1 unless explicitly escalated
make pr-tier-ratio-check

# enforce least-privilege defaults; breakglass-only elevation with audit evidence
make privilege-policy-check

# enforce backend portfolio + upgrade lifecycle sequencing policy
# (ensures routing + A/B prerequisites before upgrade automation)
make backend-upgrade-policy-check

# enforce external API interoperability policy gates
# (REST/MCP + standards conformance + compatibility/security requirements)
make api-interop-policy-check

# deterministic backlog window controller (no AI routing decisions)
make backlog-control-once

# start/monitor deterministic backlog controller daemon
make backlog-control-start
make backlog-control-status

# grant temporary breakglass elevation (TTL-bound, auditable)
python3 scripts/grant_breakglass_privilege.py --root . --provider codex --reason "incident-mitigation" --scope "task=mesh-cli-rpa" --rollback-proof "revert commit <sha>" --ttl-minutes 30 --json

# revoke active breakglass grant
python3 scripts/revoke_breakglass_privilege.py --root . --reason "mitigation-complete" --json

# generate deterministic 1-week T1 ready queue from live health artifacts
make swarm-ready-queue

# generate cycle report + blocked-cycle escalation artifacts
make swarm-cycle-report
```

Windows PowerShell wrappers:

```powershell
.\scripts\autonomy_manager.ps1 status
.\scripts\autonomy_manager.ps1 start
.\scripts\install_keepalive.ps1 install
```

Foreground debug:

```bash
make run
make supervise
```

Deterministic full-autonomy pass:

```bash
python3 -m orxaq_autonomy.cli --root . process-watchdog --strict
python3 -m orxaq_autonomy.cli --root . full-autonomy --strict
```

Autonomy-mode preflight/readiness (allows dirty sibling repos):

```bash
make preflight-autonomy
make readiness-check-autonomy
```

## VS Code Collaboration Quick Start

Use the full operator guide at `/Users/sdevisch/dev/orxaq-ops/docs/VSCODE_COLLAB_AUTONOMY_RUNBOOK.md`.

Single-command startup flow:

```bash
make bootstrap
```

`make bootstrap` will:
- generate the dual-repo workspace,
- reuse existing `orxaq-dual-agent.code-workspace` if already present,
- verify implementation/test repos exist and are valid git repositories,
- run runtime diagnostics for CLI/auth and print remediation without crashing when missing,
- run preflight checks (dirty repos allowed by default),
- start the autonomy supervisor,
- install keepalive,
- open VS Code,
- write an AI startup packet to `artifacts/autonomy/startup_packet.md`.

## RouteLLM + NPV Profile Quick Start

Use this profile when cost-aware routing and NPV-based capacity allocation should be the active objective.

```bash
make routellm-preflight
make routellm-bootstrap
make routellm-start
make routellm-status
```

For isolated Codex full-autonomy execution:

```bash
make routellm-full-auto-discover
make routellm-full-auto-prepare
make routellm-full-auto-dry-run
# then:
make routellm-full-auto-run
```

## Monitoring

One-shot console snapshot:

```bash
make monitor
```

Live GUI dashboard:

```bash
make dashboard
```

`make dashboard` starts a resilient background dashboard service and returns immediately.
Use `make dashboard-status` to confirm, `make dashboard-logs` for troubleshooting, `make dashboard-ensure` to auto-restart stale dashboard code after updates, and `make dashboard-stop` to stop it.
The dashboard provides live runner/supervisor state, task progress, lane status, conversation timeline, response cost/quality metrics, cost windows (`1h/today/7d/30d`), swarm daily budget status (cap/spend/remaining/hard-stop), a 24h cost trend sparkline, provider/model 30d cost splits, freshness/degradation signals, an auto-selected "most exciting stat" indicator (token flow when available), repo drift, and latest log signals.

To ingest authoritative provider billing/usage snapshots (OpenAI/Anthropic/Gemini) into `artifacts/autonomy/provider_costs`, configure provider endpoint URLs and API keys in `.env.autonomy`, then run:

```bash
make provider-cost-ingest
```

For one-command provider setup + validation bootstrap (auto-updates `.env.autonomy`, then runs preflight/connectivity when required keys are present):

```bash
make provider-autobootstrap
```

Validate freshness/provider health for the latest summary:

```bash
make provider-cost-health
```

This command writes `artifacts/autonomy/provider_cost_health.json` and enforces the swarm-wide daily budget guardrails configured via:

- `ORXAQ_AUTONOMY_SWARM_DAILY_BUDGET_USD` (default `100`)
- `ORXAQ_AUTONOMY_SWARM_BUDGET_WARNING_RATIO` (default `0.8`)
- `ORXAQ_AUTONOMY_SWARM_BUDGET_ENFORCE_HARD_STOP` (default `1`)

Run ingest plus health verification as one step:

```bash
make provider-cost-ingest-check
```

For unattended scheduling, run `make provider-cost-ingest-check` from cron/systemd and alert on non-zero exit. Example:

```bash
0 * * * * cd /Users/sdevisch/dev/orxaq-ops && make provider-cost-ingest-check >> artifacts/autonomy/provider_costs/cron.log 2>&1
```

Continuous health-green remediation loop (hourly, low Codex model, iterates issues until green or safe stop):

```bash
make cleanup-loop-once
make cleanup-loop-start
make cleanup-loop-status
make cleanup-loop-stop
```

Tracking artifacts:
- `artifacts/autonomy/health_green_loop/latest.json`
- `artifacts/autonomy/health_green_loop/history.ndjson`
- `artifacts/autonomy/health_green_loop/loop.log`

Deterministic backlog control loop (bounded ready queue + marker-driven completion + continuous backlog amendment):

```bash
make backlog-control-once
make backlog-control-start
make backlog-control-status
make backlog-control-stop
```

This controller writes `artifacts/autonomy/deterministic_backlog_health.json` and
`artifacts/autonomy/deterministic_backlog_history.ndjson`, keeps ready tasks inside
a configured min/target/max window, and transitions tasks to `done` only when
explicit deterministic completion markers exist in `artifacts/autonomy/task_markers`.

Inspect conversation events directly:

```bash
make conversations
```

Run lane plan (Codex/Gemini/Claude) in parallel:

```bash
make lanes-plan
make lanes-start
make lanes-status
```

Auto-heal stopped lanes (restarts enabled, non-paused lanes):

```bash
make lanes-ensure
```

Provider/model adaptive parallel-capacity telemetry:
- `make lanes-status` now prints `parallel_capacity` (`running/effective_limit`) per provider:model bucket.

## Event Mesh (Decentralized Nodes + GitHub Coordination)

Use the event mesh commands to run decentralized event-driven coordination where each node can operate independently and synchronize through git/GitHub-ledger files:

```bash
python3 -m orxaq_autonomy.cli --root . mesh-init --capability monitoring --capability routing
python3 -m orxaq_autonomy.cli --root . mesh-publish --topic scheduling --event-type task.enqueued --payload-json '{"task_id":"example"}'
python3 -m orxaq_autonomy.cli --root . mesh-sync
python3 -m orxaq_autonomy.cli --root . mesh-autonomy-once
python3 -m orxaq_autonomy.cli --root . mesh-status
```

Design docs:
- `docs/EVENT_DRIVEN_ROOT_CAUSE_ANALYSIS.md`
- `docs/EVENT_DRIVEN_REDESIGN_PLAN.md`
- State is persisted in `artifacts/autonomy/parallel_capacity_state.json`.
- Every `lanes-start`/`lanes-ensure` decision is appended to `artifacts/autonomy/parallel_capacity.ndjson`.
- Capacity limits start from `ORXAQ_AUTONOMY_PARALLEL_CAPACITY_DEFAULT_LIMIT` and auto-adjust down on capacity signals, then recover upward after stable cycles.

Manual startup flow (if you want finer control):

```bash
make workspace
make open-vscode
python3 -m orxaq_autonomy.cli --root /Users/sdevisch/dev/orxaq-ops preflight --allow-dirty
make start
make status
make logs
```

## Reuse Model

This package is reusable in any repo:

```bash
orxaq-autonomy --root /path/to/orxaq-ops start
orxaq-autonomy --root /path/to/orxaq-ops status
```

Skill protocol + MCP context are data-driven, so you can swap project/task context without changing code.

## Versioning

- SemVer is enforced: `MAJOR.MINOR.PATCH`.
- Use automated bump commands:
  - `make bump-patch`
  - `make bump-minor`
  - `make bump-major`
- Validate before push/release:
  - `make version-check`
  - `make repo-hygiene`
  - `make hosted-controls-check`
  - `make readiness-check`

See `/Users/sdevisch/dev/orxaq-ops/docs/VERSIONING.md`.

## CI/CD

- CI matrix: `.github/workflows/ci.yml`
  - Unit tests on Linux, macOS, and Windows (Python 3.11/3.12).
  - Unix shell/lint checks.
  - Package build artifact generation.
- Release pipeline: `.github/workflows/release-pypi.yml`
  - Triggered on `v*` tags.
  - Builds package and publishes to PyPI using GitHub OIDC trusted publishing.

## Governance

- `/Users/sdevisch/dev/orxaq-ops/CODE_OF_CONDUCT.md`
- `/Users/sdevisch/dev/orxaq-ops/GOVERNANCE.md`
- `/Users/sdevisch/dev/orxaq-ops/SUPPORT.md`
- `/Users/sdevisch/dev/orxaq-ops/SECURITY.md`
- `/Users/sdevisch/dev/orxaq-ops/docs/AI_BEST_PRACTICES.md`

## Non-Admin Hardening (Windows/macOS)

- No privileged operations required for routine operation.
- Keepalive installation uses user-space schedulers only:
  - Windows: Task Scheduler (`schtasks`) under current user.
  - macOS: LaunchAgents (`~/Library/LaunchAgents`).
- Subprocesses are forced non-interactive (`CI=1`, `GIT_TERMINAL_PROMPT=0`, `PIP_NO_INPUT=1`, `stdin=DEVNULL`).
- Stale git locks are auto-healed only when no active git process is detected.

## Resilience Summary

- Atomic state/report writes and runner lock file.
- Heartbeat-driven stale-runner detection and restart.
- Exponential backoff for retryable failures.
- Validation retries + fallback validation commands.
- Prompt includes file-type profile + repo-state hints + protocol requirements.
- Machine-readable health snapshot (`make health`) written to `artifacts/autonomy/health.json`.
- Deterministic process watchdog state/history:
  - `artifacts/autonomy/process_watchdog_state.json`
  - `artifacts/autonomy/process_watchdog_history.ndjson`
- Full-autonomy report: `artifacts/autonomy/full_autonomy_report.json`.
