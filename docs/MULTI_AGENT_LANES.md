# Multi-Agent Lane Plan

This lane plan keeps parallel agents from editing the same areas at the same time.

Source of truth: `config/lanes.json`

## Lanes

1. `codex-governance` (owner: `codex`)
- Repository: `orxaq-ops`
- Focus: governance dashboard, orchestration controls, monitoring APIs/UI
- Exclusive paths:
  - `src/orxaq_autonomy`
  - `tests/test_autonomy_*`
  - `docs/VSCODE_COLLAB_AUTONOMY_RUNBOOK.md`
  - `config/lanes.json`

2. `gemini-rln-tests` (owner: `gemini`)
- Repository: `orxaq_gemini`
- Focus: RLN adversarial tests and benchmark coverage
- Exclusive paths:
  - `tests/`
  - `benchmarks/`
  - `docs/testing/`

3. `claude-architecture-review` (owner: `claude`)
- Repository: `orxaq`
- Focus: architecture/security/ethics/governance review and hardening
- Exclusive paths:
  - `docs/`
  - `.github/`
  - `SECURITY.md`
  - `GOVERNANCE.md`

## Operations

1. Show lane plan: `make lanes-plan`
2. Start all enabled lanes: `make lanes-start`
3. Start one lane: `python3 -m orxaq_autonomy.cli --root . lanes-start --lane codex-governance`
4. Check status: `make lanes-status`
5. Stop one lane: `python3 -m orxaq_autonomy.cli --root . lanes-stop --lane codex-governance`
6. Stop all lanes: `make lanes-stop`

## Monitoring

1. Dashboard: `make dashboard` then open URL from `make dashboard-status`
2. Conversation stream API:
- `GET /api/conversations?lines=200`
3. CLI conversation snapshot:
- `make conversations`
