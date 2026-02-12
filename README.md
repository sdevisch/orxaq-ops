# orxaq-ops

[![CI](https://img.shields.io/github/actions/workflow/status/sdevisch/orxaq-ops/ci.yml?branch=main&label=CI)](https://github.com/sdevisch/orxaq-ops/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/actions/workflow/status/sdevisch/orxaq-ops/release-pypi.yml?branch=main&label=Release)](https://github.com/sdevisch/orxaq-ops/actions/workflows/release-pypi.yml)
[![GitHub Release](https://img.shields.io/github/v/release/sdevisch/orxaq-ops?display_name=tag)](https://github.com/sdevisch/orxaq-ops/releases)
[![PyPI](https://img.shields.io/pypi/v/orxaq-autonomy)](https://pypi.org/project/orxaq-autonomy/)
[![Python Versions](https://img.shields.io/pypi/pyversions/orxaq-autonomy)](https://pypi.org/project/orxaq-autonomy/)
[![License](https://img.shields.io/github/license/sdevisch/orxaq-ops)](https://github.com/sdevisch/orxaq-ops/blob/main/LICENSE)

Reusable autonomy control-plane for Orxaq. The autonomy runtime is a standalone Python package (`orxaq-autonomy`) with protocol-based task execution, optional MCP context ingestion, cross-platform lifecycle management, and local-first monitoring helpers (dashboard/router/RPA scheduler).

## What Changed

- Autonomy is packaged in `src/orxaq_autonomy` (independent package, reusable outside this repo).
- Runner supports a reusable **skill protocol** (`config/skill_protocol.json`).
- Runner can inject optional **MCP context** (`--mcp-context-file`) into prompts.
- Supervisor/manager is Python-based and works on macOS + Windows in user space (no admin required).
- IDE launch/open flows are IDE-independent (VS Code, Cursor, PyCharm).

## Layout

- `src/orxaq_autonomy/cli.py` - package CLI (`orxaq-autonomy`).
- `src/orxaq_autonomy/manager.py` - cross-platform supervisor, keepalive, lifecycle, stop reports.
- `src/orxaq_autonomy/runner.py` - resilient task runner with retries/validation/checkpoints/budgets.
- `src/orxaq_autonomy/protocols.py` - skill protocol + MCP context interfaces.
- `src/orxaq_autonomy/ide.py` - workspace generation and IDE launch helpers.
- `src/orxaq_autonomy/providers.py` - provider registry parsing and connectivity checks.
- `src/orxaq_autonomy/task_queue.py` - task queue validation + checkpoint helpers.
- `src/orxaq_autonomy/profile.py` - provider profile application (`local`, `lan`, `travel`).
- `src/orxaq_autonomy/router.py` - router connectivity checks + router profile application.
- `src/orxaq_autonomy/rpa_scheduler.py` - deterministic RPA scheduler.
- `src/orxaq_autonomy/dashboard.py` - local file dashboard with traversal protection.
- `skills/orxaq-autonomy-agent/SKILL.md` - reusable skill definition for autonomy workflows.
- `config/skill_protocol.json` - reusable autonomy protocol contract.
- `config/mcp_context.example.json` - sample MCP-style context payload.
- `config/providers.example.yaml` - provider registry template.
- `config/task_queue.schema.json` - JSON schema for task queue payloads.
- `profiles/*.yaml` - provider profile overlays (`local`, `lan`, `travel`) for `config/providers.active.yaml`.
- `router_profiles/*.(json|yaml)` - router profile overlays (`local`, `lan`, `travel`) for `config/router.active.yaml`.
- `docs/autonomy-halt-mitigation.md` - failure-mode playbook.
- `docs/release-pypi.md` - trusted-publishing release runbook.

Legacy shell scripts remain for compatibility, but `make` uses the package CLI.

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

Optional checkpoint controls:

- `ORXAQ_AUTONOMY_CHECKPOINT_DIR` (default `artifacts/checkpoints`)
- `ORXAQ_AUTONOMY_RUN_ID` (optional explicit run id)
- `ORXAQ_AUTONOMY_RESUME_RUN_ID` (resume from `artifacts/checkpoints/<run_id>.json`)

Optional budget controls:

- `ORXAQ_AUTONOMY_MAX_RUNTIME_SEC` (hard run wall-clock budget; `0` disables)
- `ORXAQ_AUTONOMY_MAX_TOTAL_TOKENS` (hard run token budget; `0` disables)
- `ORXAQ_AUTONOMY_MAX_TOTAL_COST_USD` (hard run cost budget; `0` disables)
- `ORXAQ_AUTONOMY_MAX_TOTAL_RETRIES` (hard cap on total retry events; `0` disables)
- `ORXAQ_AUTONOMY_BUDGET_REPORT_FILE` (default `artifacts/autonomy/budget.json`)

## Commands

```bash
make preflight
make start
make ensure
make status
make health
make logs
make stop
make router-check
make router-profile-apply PROFILE=local
make rpa-schedule
make dashboard
make install-keepalive
make keepalive-status
make workspace
make open-vscode
make open-cursor
make open-pycharm

orxaq-autonomy --root . providers-check --config config/providers.example.yaml --output artifacts/providers_check.json --strict
orxaq-autonomy --root . profile-apply local
orxaq-autonomy --root . task-queue-validate --tasks-file config/tasks.json
orxaq-autonomy --root . router-check --config ./config/router.example.yaml --output ./artifacts/router_check.json --strict
orxaq-autonomy --root . router-profile-apply travel --config ./config/router.example.yaml --profiles-dir ./router_profiles --output ./config/router.active.yaml
orxaq-autonomy --root . rpa-schedule --config ./config/rpa_schedule.example.json --output ./artifacts/autonomy/rpa_scheduler_report.json --strict
orxaq-autonomy --root . dashboard --artifacts-dir ./artifacts --host 127.0.0.1 --port 8787

orxaq-autonomy --root . pr-open --title "Autonomy update" --body "Objective + acceptance criteria"
orxaq-autonomy --root . pr-wait --pr 123 --close-on-failure --open-issue-on-failure
orxaq-autonomy --root . pr-merge --pr 123 --swarm-health-json ../orxaq/artifacts/health.json --min-swarm-health 85 --delete-branch

make lint
make test
make version-check
make repo-hygiene
make bump-patch
make bump-minor
make bump-major
make package
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

GitOps safety policy for `pr-merge`:

- merge only when PR checks are green (override only with `--allow-ci-yellow`)
- require swarm-health score from `--swarm-health-json` or `--swarm-health-score`
- block merge when score is below `--min-swarm-health` threshold

## Monitoring And Artifacts

- provider registry template: `config/providers.example.yaml`
- provider active config written by `profile-apply`: `config/providers.active.yaml`
- provider profiles: `profiles/local.yaml`, `profiles/lan.yaml`, `profiles/travel.yaml`
- router config example: `config/router.example.yaml`
- router profiles: `router_profiles/local.json`, `router_profiles/lan.json`, `router_profiles/travel.json`
- active router config written by `router-profile-apply`: `config/router.active.yaml`
- runtime budget telemetry: `artifacts/autonomy/budget.json` (also included in `make health`)
- stop report: `artifacts/autonomy/AUTONOMY_STOP_REPORT.md`
- router connectivity report: `artifacts/router_check.json`
- providers connectivity report: `artifacts/providers_check.json`
- RPA scheduler report: `artifacts/autonomy/rpa_scheduler_report.json`
- local dashboard: `orxaq-autonomy --root . dashboard --artifacts-dir ./artifacts --host 127.0.0.1 --port 8787`

Stop with report + optional issue filing:

```bash
python3 -m orxaq_autonomy.cli --root . stop --reason "blocked by failing CI"
python3 -m orxaq_autonomy.cli --root . stop --reason "manual intervention" --file-issue --issue-repo Orxaq/orxaq-ops --issue-label autonomy --issue-label blocked
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
- `/Users/sdevisch/dev/orxaq-ops/docs/DASHBOARD.md`

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

