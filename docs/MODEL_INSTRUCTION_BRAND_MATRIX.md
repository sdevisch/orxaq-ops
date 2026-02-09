# Model Instruction Brand Matrix

## Purpose
Define shared autonomy policy once and preserve only meaningful brand-level differences across Codex, Gemini, and Claude startup prompts.

## Shared contract source
- `config/prompts/shared_autonomy_instruction_contract.md`

## Shared requirements (all models)
- Issue-first workflow and issue-linked branching.
- Clean/synced environment checks before edits.
- Regular commit and push cadence.
- Cross-model review request + review evidence in reporting.
- Machine-parseable review evidence format:
  `review_evidence: reviewer=<model>; artifact=<path>; findings=<n>; resolved=<n>`.
- Conflict resolution in PR branch without artificial overlap blocking.
- Non-interactive deterministic execution.
- Validation and auditable evidence.

## Brand-specific responsibilities
- Codex:
  - Implementation ownership.
  - Minimal complete changes with validation after each unit.
  - Provide actionable handoff requests for Gemini testing.
- Gemini:
  - Independent adversarial/regression testing.
  - Failure-first coverage and reproducible defect evidence.
  - Actionable defect feedback with fix hints.
- Claude:
  - Architecture/governance/security review.
  - Severity-first findings and remediation guidance.
  - Conflict-resolving review posture (avoid policy deadlocks).

## Prompt files aligned to this matrix
- `config/prompts/codex_impl_prompt.md`
- `config/prompts/codex_full_autonomy_dashboard_prompt.md`
- `config/prompts/codex_local_model_workhorse_autonomy_prompt.md`
- `config/prompts/codex_routellm_npv_prompt.md`
- `config/prompts/codex_state_of_the_art_routing_autonomy_prompt.md`
- `config/prompts/gemini_test_prompt.md`
- `config/prompts/claude_review_prompt.md`
