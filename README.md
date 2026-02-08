# orxaq-ops

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

Legacy shell scripts remain for compatibility, but `make` now uses the package CLI.

## Setup

```bash
cd /Users/sdevisch/dev/orxaq-ops
cp .env.autonomy.example .env.autonomy
python3 -m pip install -e .
```

Set auth and repos in `.env.autonomy`:

- `GEMINI_API_KEY` or `~/.gemini/settings.json`
- `OPENAI_API_KEY` or `codex login`
- `ORXAQ_IMPL_REPO` (default `../orxaq`)
- `ORXAQ_TEST_REPO` (default `../orxaq_gemini`)

Optional reusable context controls:

- `ORXAQ_AUTONOMY_SKILL_PROTOCOL_FILE` (default `config/skill_protocol.json`)
- `ORXAQ_AUTONOMY_MCP_CONTEXT_FILE` (optional MCP-style JSON file)

## Commands

```bash
make preflight
make start
make ensure
make status
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
make package
```

Foreground debug:

```bash
make run
make supervise
```

## Reuse Model

This package is reusable in any repo:

```bash
orxaq-autonomy --root /path/to/orxaq-ops start
orxaq-autonomy --root /path/to/orxaq-ops status
```

Skill protocol + MCP context are data-driven, so you can swap project/task context without changing code.

## CI/CD

- CI matrix: `.github/workflows/ci.yml`
  - Unit tests on Linux, macOS, and Windows (Python 3.11/3.12).
  - Unix shell/lint checks.
  - Package build artifact generation.
- Release pipeline: `.github/workflows/release-pypi.yml`
  - Triggered on `v*` tags.
  - Builds package and publishes to PyPI using GitHub OIDC trusted publishing.

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
