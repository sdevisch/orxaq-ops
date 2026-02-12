# Codex Autonomy Prompt: Distributed Coordinator HA (DMN + DAG + Causal DAG)

You are implementing the remaining distributed coordinator resilience work in `/Users/sdevisch/dev/orxaq-ops`.

## Mission

Complete the remaining work from:
- `/Users/sdevisch/dev/orxaq-ops/docs/DISTRIBUTED_COORDINATOR_DMN_DAG_REMAINING_WORK_PLAN.md`
- `/Users/sdevisch/dev/orxaq-ops/docs/DISTRIBUTED_COORDINATOR_DMN_DAG_HA_PLAN.md`

Focus on safe, incremental delivery with deterministic behavior and regression coverage.

## Current State You Must Respect

- Epoch fencing and command logs already exist in manager mesh command handling.
- Local leader lease module exists and is wired into event mesh command emission.
- Existing runtime is actively used; avoid broad disruptive rewrites.

## Priority Order

1. Implement lease backend abstraction and keep file backend as fallback.
2. Enforce leader/epoch gates consistently in mutating manager paths.
3. Extract scaling policy into a versioned deterministic DMN-style evaluator.
4. Introduce DAG frontier scheduling scaffolding with replay-safe state transitions.
5. Add causal intervention metadata checks for disruptive actions.
6. Extend observability (leader epoch, command outcomes, decision trace).

## Constraints

- Non-interactive execution only.
- No destructive git operations.
- Keep compatibility with existing CLI and dashboard.
- Keep modifications minimal per cycle; prefer small verifiable units.

## Deliverable Requirements

- Update code under `src/orxaq_autonomy/`.
- Add/extend tests under `tests/` for each behavior change.
- Update docs only where needed to explain new operational behavior.
- Emit concise evidence in artifacts when scripts already support it.

## Validation Gate

Run and pass:

```bash
pytest -q tests/test_leader_lease.py tests/test_event_mesh.py tests/test_autonomy_manager.py
```

When touching broader manager/event paths, also run:

```bash
make lint
make test
```

## Stop Condition

If blocked by credentials/infra externalities, produce a clear blocker summary and continue with all locally completable steps.
