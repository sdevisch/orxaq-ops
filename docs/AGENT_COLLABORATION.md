# Agent Collaboration Contract

## Goals

- Enable multiple agents to work in parallel without stepping on each other.
- Keep integration quality high with serialized merges into protected branches.

## Required workflow

1. Start from an isolated lane (worktree or clone) with a dedicated branch.
2. Claim the path scope before editing (`.agent-coordination` claim).
3. Keep changes inside claimed scope when possible.
4. Open a PR and wait for required CI checks.
5. Merge through merge queue when targeting protected branches.
6. Release the claim after merge or task cancellation.

## Lane bootstrap examples

```bash
/Users/sdevisch/dev/tools/agent-collab/agentctl.sh bootstrap \
  --repo <repo-path> \
  --agent <agent-name> \
  --task <task-slug> \
  --mode worktree \
  --sparse ".github,docs,src,tests"
```

## Path claim examples

```bash
/Users/sdevisch/dev/tools/agent-collab/agentctl.sh claim \
  --repo <repo-path> \
  --agent <agent-name> \
  --path <repo-relative-path> \
  --ttl-hours 8 \
  --note "short task description"

/Users/sdevisch/dev/tools/agent-collab/agentctl.sh release --id <claim-id>
```

## CI and protection requirements

- Workflows that gate protected branches must run on `pull_request` and `merge_group`.
- Workflow-level `concurrency` must be enabled to prevent duplicate CI storms.
- `CODEOWNERS` must include ownership for policy files:
  - `.github/CODEOWNERS`
  - `.github/workflows/`

## Review policy

- Avoid multi-domain PRs when not needed.
- Keep PR titles scoped to one lane/path ownership.
- If two tasks require overlapping paths, sequence them explicitly instead of parallelizing.
