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

2. `codex-routellm-npv` (owner: `codex`, disabled by default)
- Repository: `orxaq-ops` + `orxaq_gemini`
- Focus: RouteLLM model routing, NPV-based capacity scaling, and routing economics governance
- Activation notes:
  - Keep this lane disabled during normal governance work.
  - Enable only for focused routing-economics runs, ideally with `codex-governance` paused.

3. `gemini-rln-tests` (owner: `gemini`)
- Repository: `orxaq_gemini`
- Focus: RLN adversarial tests and benchmark coverage
- Exclusive paths:
  - `tests/`
  - `benchmarks/`
  - `docs/testing/`

4. `claude-architecture-review` (owner: `claude`)
- Repository: `orxaq`
- Focus: architecture/security/ethics/governance review and hardening
- Exclusive paths:
  - `docs/`
  - `.github/`
  - `SECURITY.md`
  - `GOVERNANCE.md`

## Local backend routing profiles

Lane specs support backend command/model overrides and RouteLLM policy selection:

- `codex_cmd`, `gemini_cmd`, `claude_cmd`
- `codex_model`, `gemini_model`, `claude_model`
- `gemini_fallback_models`
- `routellm_policy_file`, `routellm_enabled`, `routellm_url`, `routellm_timeout_sec`

Included policy templates:

- `config/routellm_policy.local-fast.json` (defaults to `http://127.0.0.1:8788/route`)
- `config/routellm_policy.local-strong.json` (defaults to `http://127.0.0.1:8789/route`)

Current lane defaults:

- `codex-governance`: wired to `local-fast` policy, RouteLLM enabled.
- `codex-routellm-npv`: wired to `local-strong` policy, RouteLLM enabled when this lane is enabled.
- `gemini-rln-tests`: wired to `local-fast` policy, RouteLLM enabled.
- `claude-architecture-review`: wired to `local-fast` policy, RouteLLM enabled.

Intelligent router policy notes:

- `local-fast`: prioritizes cost and speed for low-risk/high-throughput tasks.
- `local-strong`: prioritizes quality for high-complexity tasks with bounded cost pressure.
- Both policies now ship with a broad model catalog (local + hosted) and estimated cost/speed metadata used by router scoring.

## Operations

1. Show lane plan: `make lanes-plan`
2. Start all enabled lanes: `make lanes-start`
3. Start one lane: `python3 -m orxaq_autonomy.cli --root . lanes-start --lane codex-governance`
4. Check status: `make lanes-status`
5. Stop one lane: `python3 -m orxaq_autonomy.cli --root . lanes-stop --lane codex-governance`
6. Stop all lanes: `make lanes-stop`

RouteLLM profile shortcuts:
- `make routellm-preflight`
- `make routellm-bootstrap`
- `make routellm-start`
- `make routellm-status`
- `make routellm-full-auto-dry-run`

## Monitoring

1. Dashboard: `make dashboard` then open URL from `make dashboard-status`
2. Conversation stream API:
- `GET /api/conversations?lines=200`
3. CLI conversation snapshot:
- `make conversations`
