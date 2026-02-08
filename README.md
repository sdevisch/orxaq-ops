# orxaq-ops

Control-plane workspace for unattended multi-agent execution against the Orxaq product repo.

This keeps operational autonomy tooling out of `orxaq` so product releases stay clean.

## Layout

- `scripts/autonomy_runner.py` - task scheduler and agent executor.
- `scripts/autonomy_manager.sh` - lifecycle commands (`run/start/stop/status/logs/reset`).
- `config/tasks.json` - prioritized queue.
- `config/objective.md` - project objective and stop criteria.
- `config/codex_result.schema.json` - expected Codex JSON response schema.
- `state/state.json` - runtime task state (auto-created).
- `artifacts/autonomy/` - reports and runner logs.

## Setup

```bash
cd /Users/sdevisch/dev/orxaq-ops
cp .env.autonomy.example .env.autonomy
```

Edit `.env.autonomy` and set:

- `OPENAI_API_KEY` (required)
- `ORXAQ_IMPL_REPO` (optional, defaults to `../orxaq`)
- `ORXAQ_TEST_REPO` (optional, defaults to `../orxaq_gemini`)

Ensure CLIs are installed and authenticated:

```bash
which codex
which gemini
```

## Commands

```bash
make start
make status
make logs
make stop
make reset
```

Run foreground (for debugging):

```bash
make run
```
