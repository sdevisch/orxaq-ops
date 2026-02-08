# orxaq-ops

Control-plane workspace for unattended multi-agent execution against the Orxaq product repo.

This keeps operational autonomy tooling out of `orxaq` so product releases stay clean.

## Layout

- `scripts/autonomy_runner.py` - task scheduler and agent executor.
- `scripts/autonomy_manager.sh` - lifecycle and self-healing supervisor (`run/supervise/start/stop/ensure/status/logs/reset`).
- `scripts/preflight.sh` - strict readiness gate (auth, tools, repo cleanliness).
- `scripts/generate_workspace.sh` - writes VS Code multi-root workspace file.
- `scripts/install_keepalive.sh` - installs/uninstalls user-space keepalive job (macOS launchd).
- `docs/autonomy-halt-mitigation.md` - failure-mode playbook and built-in controls.
- `config/tasks.json` - prioritized queue.
- `config/objective.md` - project objective and stop criteria.
- `config/codex_result.schema.json` - expected Codex JSON response schema.
- `state/state.json` - runtime task state (auto-created).
- `artifacts/autonomy/` - reports, logs, heartbeat, and lock files.

## Setup

```bash
cd /Users/sdevisch/dev/orxaq-ops
cp .env.autonomy.example .env.autonomy
```

Edit `.env.autonomy` and set:

- `GEMINI_API_KEY` (required unless `~/.gemini/settings.json` is already configured)
- `OPENAI_API_KEY` (optional if `codex login` is already configured)
- `ORXAQ_IMPL_REPO` (optional, defaults to `../orxaq`)
- `ORXAQ_TEST_REPO` (optional, defaults to `../orxaq_gemini`)

Ensure CLIs are installed and authenticated:

```bash
which codex
which gemini
```

## Commands

```bash
make start      # start supervisor in background
make ensure     # self-heal: start if stopped, restart stale runner
make status
make logs
make stop
make reset
make preflight
make workspace
make open-vscode
make install-keepalive
make keepalive-status
make lint
make test
```

`make open-vscode` launches `Visual Studio Code.app` explicitly (not Cursor).

Run foreground (for debugging):

```bash
make run         # runner only
make supervise   # supervisor + auto-restart in foreground
```

Install OS-level self-heal (recommended):

```bash
make install-keepalive
```

## Resilience Features

- File lock (`artifacts/autonomy/runner.lock`) prevents concurrent runner instances.
- Atomic state/report writes prevent partial JSON corruption on crashes.
- Heartbeat file (`artifacts/autonomy/heartbeat.json`) updated every cycle/phase.
- Supervisor monitors heartbeat and restarts runner if stale or crashed.
- Exponential backoff on retryable failures (timeouts, 429, transient network/service issues).
- Cooldown-aware task scheduling (`not_before`) avoids tight failure loops.
- Retry context is fed back into prompts so agents continue from previous failures.
- Non-strict agent output parsing recovers JSON from fenced/embedded text.
- All subprocesses run in forced non-interactive mode (`CI=1`, `GIT_TERMINAL_PROMPT=0`, `PIP_NO_INPUT=1`, no TTY stdin).
- Stale git lock files are auto-healed before task execution and after git-lock related failures.
- Validation commands auto-retry for test commands and use fallback commands when `make` targets are missing.
- Prompt context includes repository file-type profile and active git-state hints (merge/rebase/cherry-pick in progress).

## Failure Handling Matrix

- CLI crash/non-zero exit: retry with backoff; supervisor restarts process.
- Runner hang: stale heartbeat detection triggers forced restart.
- State corruption risk: atomic writes + lock file.
- Transient API/network failures: classified as retryable and re-queued.
- Partial task output: automatically re-queued up to retry budget.
- True hard blockers: task marked blocked with full error context in state and report.

## Recommended Cron Self-Heal

- macOS: use `make install-keepalive` (launchd, user-space, no admin).
- Other systems fallback command:
  `cd /Users/sdevisch/dev/orxaq-ops && ./scripts/autonomy_manager.sh ensure`
