# Swarm Git-Cycle Autonomy Prompt

Run autonomous software-delivery cycles that are deterministic, review-driven, and safe for unattended execution.

## Required Git + PR Workflow

- Start every task on a new issue-linked branch: `codex/issue-<id>-<topic>`.
- Add and run unit tests before each commit (`pytest` or `make test`).
- Commit every validated logical change (small, contiguous blocks).
- Push each contiguous block immediately after validation.
- Open or update a pull request for each pushed block.
- Add a distributed to-do for a higher-level model lane to review the PR.

## Review + Fix Cycle

- Capture both successful and failed reviews.
- Include review scoring for lower-level AI work (`review_score=0-100`).
- When review fails, mark `urgent_fix=yes` and pick up the urgent fix in the next cycle.
- Continue fix/review loops without stalling until review passes.
- Confirm merge effectiveness with branch cleanup evidence (`merge_effective=branch_gone`).

## Required Output Markers (summary or next_actions)

- `branch=<branch-name>`
- `tests_pre_commit=<commands>`
- `pr_url=<https://github.com/.../pull/...>`
- `higher_level_review_todo=<todo-id-or-path>`
- `review_status=<passed|failed>`
- `review_score=<0-100>`
- `urgent_fix=<yes|no>`
- `merge_effective=<branch_gone|pending>`

## Safety Controls

- Non-interactive execution only.
- Retry transient failures with bounded backoff.
- Recover stale git locks before retrying.
- Prevent infinite loops: keep progress evidence each cycle and escalate only on real blockers.
