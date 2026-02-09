# Issue #4 Review Evidence

## Scope reviewed
Instruction refactor for:
- shared autonomy contract,
- Codex/Gemini/Claude startup prompts,
- skill protocol defaults.

## Reviewer attempts
1. Gemini CLI review attempt
- Command: `gemini --yolo --output-format text -p <prompt> < artifacts/issue_4_model_instruction_refactor.diff`
- Result: blocked by missing Gemini auth (`GEMINI_API_KEY` / Vertex / GCA not configured).

2. Local LM Studio reviewer model
- Model: `deepseek-coder-v2-lite-instruct`
- Input package: `artifacts/issue_4_review_package.txt` (context-truncated)
- Output evidence:
  - `artifacts/issue_4_lmstudio_review.json`
  - `artifacts/issue_4_lmstudio_review.md`

## Findings summary from reviewer model
- Shared contract improves redundancy reduction and maintainability.
- Model-brand separation is clearer.
- Workflow preferences are covered.
- Suggested hardening: make clean-environment evidence and review-evidence formatting explicit.

## Resolutions applied
- Added explicit baseline evidence requirements in `config/prompts/shared_autonomy_instruction_contract.md`.
- Added machine-parseable review evidence format across prompt output contracts:
  - `review_evidence: reviewer=<model>; artifact=<path>; findings=<n>; resolved=<n>`.

## Review evidence block
`review_evidence: reviewer=deepseek-coder-v2-lite-instruct; artifact=artifacts/issue_4_lmstudio_review.md; findings=1; resolved=1`
