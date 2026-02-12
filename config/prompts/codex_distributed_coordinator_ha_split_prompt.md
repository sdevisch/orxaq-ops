You are implementing the next focused chunk of distributed coordinator HA work inside `/Users/sdevisch/dev/orxaq-ops`. This run uses the split task list in `config/tasks_distributed_coordinator_ha_split.json`. Start with the highest-priority unfinished task (lease backend hardening) and do not move to another task in this run.

## Mission

- Complete the current split task through code changes in `src/orxaq_autonomy/` plus supporting tests/documentation.
- Keep each change small, verifiable, and aligned with the workstream expectations in `/Users/sdevisch/dev/orxaq-ops/docs/DISTRIBUTED_COORDINATOR_DMN_DAG_REMAINING_WORK_PLAN.md`.
- Preserve existing functionality and add coverage for new behavior via existing tests.
- Run the targeted validation suite after edits: `pytest -q tests/test_leader_lease.py tests/test_event_mesh.py tests/test_autonomy_manager.py`.

## Constraints

- Non-interactive execution only; no prompts.
- Respect issue-linked branches (`codex/issue-<id>-<topic>`). Record git baseline commands exactly once.
- Do not operate on unrelated tasks or change multiple components simultaneously.
- Blockers must cite the smallest context possible and avoid echoing the entire previous prompt.
- Return final JSON per autonomy schema: keys `status`, `summary`, `commit`, `validations`, `next_actions`, `blocker`, plus `usage`.

## Deliverable requirements

- Lease backend workstream: codify backend interface, config, error handling, and validation tests.
- Document configuration flags in README or dedicated doc only when needed for operator clarity.
- Keep operation log artifacts (conversations, heartbeat) neat; avoid filling them with prompt duplicates.
