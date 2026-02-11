# orxaq-ops

[![CI](https://img.shields.io/github/actions/workflow/status/sdevisch/orxaq-ops/ci.yml?branch=main&label=CI)](https://github.com/sdevisch/orxaq-ops/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/actions/workflow/status/sdevisch/orxaq-ops/release-pypi.yml?branch=main&label=Release)](https://github.com/sdevisch/orxaq-ops/actions/workflows/release-pypi.yml)
[![GitHub Release](https://img.shields.io/github/v/release/sdevisch/orxaq-ops?display_name=tag)](https://github.com/sdevisch/orxaq-ops/releases)
[![PyPI](https://img.shields.io/pypi/v/orxaq-autonomy)](https://pypi.org/project/orxaq-autonomy/)
[![Python Versions](https://img.shields.io/pypi/pyversions/orxaq-autonomy)](https://pypi.org/project/orxaq-autonomy/)
[![License](https://img.shields.io/github/license/sdevisch/orxaq-ops)](https://github.com/sdevisch/orxaq-ops/blob/main/LICENSE)

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
- `docs/autonomy-halt-mitigation.md` - failure-mode playbook.
- `docs/release-pypi.md` - trusted-publishing release runbook.

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

Budget and routing controls:

- routing policy file: `config/routing_policy.yaml`
- runtime budget telemetry: `artifacts/autonomy/budget.json`
- health snapshot includes latest `budget` section (`make health`)
- stop report: `artifacts/autonomy/AUTONOMY_STOP_REPORT.md`

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
