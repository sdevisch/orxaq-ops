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
- `config/prompts/codex_impl_prompt.md` - baseline implementation prompt for Codex.
- `config/prompts/gemini_test_prompt.md` - baseline independent-test prompt for Gemini.
- `config/prompts/claude_review_prompt.md` - baseline governance/review prompt for Claude.
- `config/lanes.json` - parallel lane plan for Codex/Gemini/Claude with non-overlapping scopes.
- `AGENTS.md` - canonical collaboration standard for hybrid human + IDE + API agent workflows.
- `docs/autonomy-halt-mitigation.md` - failure-mode playbook.
- `docs/VSCODE_COLLAB_AUTONOMY_RUNBOOK.md` - end-to-end VS Code + multi-agent operating guide.
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
- `ORXAQ_AUTONOMY_CODEX_PROMPT_FILE` (default `config/prompts/codex_impl_prompt.md`)
- `ORXAQ_AUTONOMY_GEMINI_PROMPT_FILE` (default `config/prompts/gemini_test_prompt.md`)
- `ORXAQ_AUTONOMY_CODEX_CMD` (default `codex`; can be absolute path)
- `ORXAQ_AUTONOMY_GEMINI_CMD` (default `gemini`; can be absolute path)
- `ORXAQ_AUTONOMY_CLAUDE_CMD` (default `claude`; can be absolute path)
- `ORXAQ_AUTONOMY_CONVERSATION_LOG_FILE` (default `artifacts/autonomy/conversations.ndjson`)
- `ORXAQ_AUTONOMY_METRICS_FILE` (default `artifacts/autonomy/response_metrics.ndjson`)
- `ORXAQ_AUTONOMY_METRICS_SUMMARY_FILE` (default `artifacts/autonomy/response_metrics_summary.json`)
- `ORXAQ_AUTONOMY_PRICING_FILE` (default `config/pricing.json`)
- `ORXAQ_AUTONOMY_LANES_FILE` (default `config/lanes.json`)

Configure per-model rates in `/Users/sdevisch/dev/orxaq-ops/config/pricing.json` to enable exact response cost tracking.

## Commands

```bash
make preflight
make bootstrap
make start
make ensure
make status
make monitor
make metrics
make health
make logs
make dashboard
make dashboard-status
make dashboard-logs
make dashboard-stop
make conversations
make lanes-plan
make lanes-status
make lanes-start
make lanes-ensure
make lanes-stop
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
The dashboard provides live runner/supervisor state, task progress, lane status, conversation timeline, response cost/quality metrics, an auto-selected "most exciting stat" indicator (token flow when available), repo drift, and latest log signals.

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
