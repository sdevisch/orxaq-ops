# Shared Autonomy Instruction Contract

Apply this contract to all autonomous lanes unless a lane objective explicitly requires a stricter constraint.

## 1) Workflow Initialization
1. Create or reference a GitHub issue before implementation starts.
2. Work on an issue-linked branch: `codex/issue-<id>-<topic>`.
3. Bootstrap a clean/synced environment before edits:
- `git status -sb`
- `git fetch --prune`
- verify objective/tasks/schema/prompt files for the lane
- verify required runtime/tooling commands succeed non-interactively
4. Record baseline evidence in reporting:
- issue id/link,
- branch name,
- baseline `git status -sb` outcome.

## 2) Delivery Cadence
1. Implement in small, testable increments.
2. Commit regularly with scoped, verb-first messages.
3. Push regularly after validated units.
4. If push is blocked (auth/network), continue locally and report exact blocker + attempted command.

## 3) Cross-Model Review
1. Request review from another model lane before finalizing substantial work.
2. Include review evidence in final reporting:
- reviewer model/lane,
- evidence artifact path(s),
- key findings,
- what was resolved.
3. Use a machine-parseable evidence block in `summary` and/or `next_actions`, for example:
`review_evidence: reviewer=<model>; artifact=<path>; findings=<n>; resolved=<n>`
4. If no reviewer is available, report explicit blocker and preserve local evidence.

## 4) Conflict and Merge Policy
1. Resolve merge conflicts in the PR/working branch as part of normal delivery.
2. Do not block work solely because files were touched in another branch.
3. Do not add artificial policies that prohibit legitimate scoped edits to overlapping files.
4. Keep conflict resolution auditable (clear commit history and summaries).

## 5) Safety and Determinism
1. Non-interactive execution only.
2. Preserve unknown/binary file types; avoid destructive rewrites.
3. Avoid destructive git commands unless explicitly requested.
4. Keep behavior deterministic, auditable, and reversible.

## 6) Validation and Evidence
1. Run required lane validation gates before completion.
2. Report concrete command outcomes and residual risks.
3. Include review evidence in `summary` and/or `next_actions` fields of final JSON output.
