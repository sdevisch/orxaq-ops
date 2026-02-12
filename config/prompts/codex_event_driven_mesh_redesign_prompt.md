# Codex Prompt: Event-Driven Decentralized Ops Redesign

You are implementing an autonomous redesign for `orxaq-ops`.

## Mission

Move autonomy runtime behavior to a real-time event-driven model for monitoring, scheduling, and routing. Keep GitHub as the inter-agent coordination center, but avoid central runtime dependencies. Any node (for example `.86`, this workstation, CI nodes) must run independently and converge through shared GitHub-ledger state.

## Hard Requirements

1. No single always-on central orchestrator dependency.
2. GitHub remains the source of truth for inter-node coordination artifacts.
3. Nodes must operate offline and continue local progress.
4. Event processing must be idempotent (replay-safe).
5. Preserve current CLI compatibility while introducing new event-mesh flows.
6. Keep changes auditable and deterministic.

## Required Deliverables

1. Root-cause analysis of existing loop/supervisor-centric constraints.
2. Comprehensive redesign plan with migration phases.
3. Implementation of:
   - event envelope contract
   - local append-only event stream
   - dispatch cursor + dedupe state
   - GitHub-ledger import/export for events
   - node capability manifest
4. Tests for publish/dispatch/route/import/export behavior.
5. Updated docs and command references.

## Design Constraints

1. Prefer additive and backwards-compatible changes first.
2. No destructive git operations.
3. Avoid introducing external runtime dependencies unless strictly necessary.
4. Keep schema and file layout explicit.
5. Use UTC ISO timestamps and stable deterministic IDs.

## Validation Gates

Run and pass:

- `make lint`
- `make test`
- `make version-check`
- `make repo-hygiene`
- `make hosted-controls-check`

## Reporting Format

Return:

1. Issues fixed now.
2. Root causes and evidence.
3. Redesign plan summary.
4. Concrete implementation details (files and commands).
5. Residual risks and immediate next milestones.
