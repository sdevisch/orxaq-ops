# Swarm Shared Git Contract

Apply this contract for all lower-level and higher-level lanes.

## Mandatory Steps

1. Create or switch to an issue-linked branch: `codex/issue-<id>-<topic>`.
2. Add and run unit tests before each commit.
3. Commit every validated logical change.
4. Push contiguous commit blocks after validation.
5. Open or update the pull request.
6. Add a higher-level review to-do tied to the PR.
7. Record review status and review score.
8. If review fails, create urgent fix work and pick it up immediately.
9. Continue cycles until review passes.
10. Verify merge effectiveness by confirming the branch is gone.

## Evidence Markers

- `branch=<branch-name>`
- `tests_pre_commit=<commands>`
- `pr_url=<https://github.com/.../pull/...>`
- `higher_level_review_todo=<todo-id-or-path>`
- `review_status=<passed|failed>`
- `review_score=<0-100>`
- `urgent_fix=<yes|no>`
- `merge_effective=<branch_gone|pending>`
