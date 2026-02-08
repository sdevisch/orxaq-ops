# AI Best Practices

This repo is designed for autonomous multi-agent execution. Agents must follow these rules.

## Mandatory Gates

1. Run before push:
   - `make lint`
   - `make test`
   - `make version-check`
   - `make repo-hygiene`
   - `make hosted-controls-check`
   - `make readiness-check`
2. Keep changes scoped and test-backed.
3. Do not commit generated artifacts, caches, or environment files.

## Safety and Security

1. Never commit secrets or credentials.
2. Keep automation in user-space and least privilege mode.
3. Use non-interactive subprocess settings for unattended runs.

## Versioning

1. Use SemVer with automated bump commands.
2. Align release tags with project version exactly.

## Reliability

1. Preserve restart/resume safety and idempotency in supervisor changes.
2. Treat runtime state files as operational data, not source artifacts.
