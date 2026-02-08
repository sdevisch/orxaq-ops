# VS Code Collaboration and Autonomy Runbook

This runbook is the single operator guide for running Codex and Gemini together with autonomous retries and restart safety.

## Source of Truth

- Objective and done criteria: `config/objective.md`
- Task queue and role split: `config/tasks.json`
- Skill protocol contract: `config/skill_protocol.json`
- Halt and recovery controls: `docs/autonomy-halt-mitigation.md`
- AI behavior and gates: `docs/AI_BEST_PRACTICES.md`
- Reusable prompts:
  - `config/prompts/codex_impl_prompt.md`
  - `config/prompts/gemini_test_prompt.md`

## One-Time Setup

1. Install package and hooks.

```bash
cd /Users/sdevisch/dev/orxaq-ops
python3 -m pip install -e .
make setup
```

2. Configure runtime env.

```bash
cp .env.autonomy.example .env.autonomy
```

Required auth in `.env.autonomy` or local CLI auth:
- Codex: `OPENAI_API_KEY` or `codex login`
- Gemini: `GEMINI_API_KEY` or `~/.gemini/settings.json`

Set repository paths:
- `ORXAQ_IMPL_REPO=/Users/sdevisch/dev/orxaq`
- `ORXAQ_TEST_REPO=/Users/sdevisch/dev/orxaq_gemini`

## VS Code Transition

1. Generate dual-repo workspace.

```bash
make workspace
```

2. Open workspace in VS Code.

```bash
make open-vscode
```

Windows:

```powershell
.\scripts\autonomy_manager.ps1 workspace
.\scripts\autonomy_manager.ps1 open-ide --ide vscode
```

## Start Autonomy

1. Preflight (auth, binaries, repo checks).

```bash
make preflight
```

If implementation repos intentionally contain active local work, use:

```bash
python3 -m orxaq_autonomy.cli --root /Users/sdevisch/dev/orxaq-ops preflight --allow-dirty
```

2. Start supervisor.

```bash
make start
```

3. Confirm health and tail logs.

```bash
make status
make health
make logs
```

4. Enable host keepalive for restart resilience.

```bash
make install-keepalive
make keepalive-status
```

## Day-2 Operations

- Self-heal if runner exited or heartbeat is stale:

```bash
make ensure
```

- Stop cleanly:

```bash
make stop
```

- Full state reset (safe for restart from task queue state):

```bash
make reset
```

## Collaboration Contract

- Codex owns implementation and production changes in `orxaq`.
- Gemini owns independent test design and adversarial validation in `orxaq_gemini`.
- Both agents follow:
  - non-interactive execution only,
  - no destructive history operations on shared branches,
  - validation gates before claiming progress (`make lint`, `make test`).
- Hand off through explicit artifacts and commit messages, not assumptions.

## Prompting Guidance

Use these prompts as the baseline:
- Codex: `config/prompts/codex_impl_prompt.md`
- Gemini: `config/prompts/gemini_test_prompt.md`

Both prompts enforce:
- deterministic and test-backed changes,
- anti-compaction and detail-retention validation for RLN,
- security, ethics, and Windows non-admin constraints.

## Failure Triage

1. Read status:

```bash
make status
make health
```

2. Inspect logs:

```bash
make logs
```

3. Apply self-heal:

```bash
make ensure
```

4. If still blocked, use mitigation playbook:
- `docs/autonomy-halt-mitigation.md`
