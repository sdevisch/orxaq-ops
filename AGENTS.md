# Orxaq Ops - Agent Guide

This repository follows the Orxaq Agent Standard v1.

## Core Standard

- Operate autonomously and complete the scoped request end-to-end.
- Keep changes focused, minimal, and production-grade.
- Preserve security, governance, and runtime safety constraints.
- Prefer deterministic, composable implementations; avoid ad-hoc process coupling.
- Never use destructive git commands (`git reset --hard`, `git checkout --`, `git clean -fd`) unless explicitly requested by the user.
- Never revert, discard, or overwrite unrelated user changes.
- Create/attach a GitHub issue before implementation work for a new scoped task.
- Always start each new scoped prompt/session on a new dedicated `codex/*` branch with a unique suffix.
- Make small atomic commits with verb-first messages under 72 characters.
- Push after each validated logical unit when remote access is available.
- Request review from another model lane for substantial changes and include review evidence in final reporting.
- If push/auth/network is blocked, continue locally and report the exact blocker.

## Git Workflow Standard

1. Check baseline state: `git status -sb`.
2. Create/confirm issue context and branch against issue (`codex/issue-<id>-<topic>` preferred).
3. Create a new `codex/*` branch with a unique suffix for the scoped task.
4. Implement scoped changes with tests/docs as needed.
5. Run required validation gates.
6. Commit atomic changes and push.
7. Resolve merge conflicts in-branch; do not block solely due cross-branch file overlap.
8. Merge commits are allowed when there are no unresolved conflicts (`git diff --name-only --diff-filter=U` is empty).
9. Report concrete command outputs, review evidence, and residual risks.

## Required Validation Gates

Run before marking work complete:

- `make lint`
- `make test`
- `make version-check`
- `make repo-hygiene`
- `make hosted-controls-check`

For release-readiness work, also run:

- `make readiness-check`

## Artifact and Hygiene Policy

- Do not commit secrets, credentials, or `.env*` files (except approved templates such as `.env.autonomy.example`).
- Do not commit runtime data under `artifacts/` or `state/` unless explicitly requested.
- Keep caches and temporary files out of git.
- Keep branch history clean and understandable; avoid mixed unrelated changes in one commit.

## Repository Context

- Domain: operational autonomy, hosted control-plane checks, and multi-lane coordination.
- Preserve lane ownership boundaries and avoid introducing cross-lane hidden dependencies.
- Keep monitoring and run-state behavior deterministic and auditable.

## Useful Commands

```bash
make setup
make pre-commit
make pre-push
make lanes-plan
make lanes-status
make monitor
make readiness-check
```
