Act as the autonomous technical owner across:
- /Users/sdevisch/dev/orxaq
- /Users/sdevisch/dev/orxaq-ops
- /Users/sdevisch/dev/orxaq_gemini
- /Users/sdevisch/dev/odyssey

Timebox:
- Run continuously for the next 24 hours from start time, or stop earlier only if all goals are complete.
- Do not wait for user nudges between tasks.

Primary objective:
Finish all remaining high-impact work from today’s backlog and leave the system in a production-ready, continuously operable state.

Recent failure patterns to fix first (treat as required hardening scope):
- Queue starvation loop in autonomy supervisor:
  - "No ready tasks remain. Pending=['release-quality'], Blocked=['rln-adversarial-tests']"
  - repeated `runner rc=2` restarts.
- Dependency deadlock between `release-quality` and `rln-adversarial-tests`.
- Push stalls due large failing suites in implementation repos (not only hook mechanics).
- Stale or unsynchronized task state between lane-level outcomes and global state.
- Prompt/output brittleness (strict JSON/schema or usage fields missing) causing avoidable reruns.

Start by reading:
- /Users/sdevisch/dev/orxaq-ops/state/state.json
- /Users/sdevisch/dev/orxaq-ops/config/tasks.json
- /Users/sdevisch/dev/orxaq-ops/config/objective.md
- /Users/sdevisch/dev/orxaq-ops/docs/MULTI_AGENT_LANES.md
- /Users/sdevisch/dev/orxaq-ops/docs/autonomy-halt-mitigation.md
- /Users/sdevisch/dev/orxaq-ops/docs/VSCODE_COLLAB_AUTONOMY_RUNBOOK.md
- /Users/sdevisch/dev/orxaq/docs/FULL_AUTONOMY_MODE.md
- /Users/sdevisch/dev/odyssey/docs/PROJECT_COMPLETION_AUTONOMOUS_PLAN.md

Phase order (strict):
1) Stabilize autonomy control-plane (eliminate no-ready/restart loop).
2) Unblock `rln-adversarial-tests` with concrete fixes and verified state transitions.
3) Complete `release-quality` end-to-end.
4) Validate 24h autonomy resilience and lane health.
5) Final docs/governance cleanup and release evidence packaging.

Autonomy hardening protocol (mandatory):
1. Run baseline diagnostics:
   - make status
   - make health
   - make lanes-status
   - make conversations
2. If any "no ready tasks remain" appears while pending or blocked tasks exist:
   - Gather blocker evidence from:
     - /Users/sdevisch/dev/orxaq-ops/artifacts/autonomy/runner.log
     - /Users/sdevisch/dev/orxaq-ops/artifacts/autonomy/lanes/*/runner.log
     - /Users/sdevisch/dev/orxaq-ops/artifacts/autonomy/lanes/*/conversations.ndjson
   - Convert blocker evidence into implementation tasks immediately.
   - Fix root-cause code/tests in the owning repo(s), not only orchestration.
   - Resynchronize task state when lane evidence proves completion.
   - Re-run `make lanes-ensure` and verify scheduler selects real work.
3. Never idle with pending work:
   - If a dependency is blocked by implementation failures, take ownership, patch, validate, and re-open progress.
4. Enforce non-interactive execution:
   - Use non-interactive commands only; never wait for terminal prompts.
5. Push hardening:
   - If push fails, resolve hook/output causes and retry; do not stop at first failed push.

Known implementation failure clusters to prioritize:
1) RPA URL safety adversarial gaps (null-byte, encoded localhost, decimal IP, nested scheme, creds).
2) SCM intervention API robustness (`StructuralCausalModel` constructor/equations expectations).
3) RLN semantic compaction/merge corruption under small context windows.
4) RLN extraction edge-case duplication/index safety.
5) Orchestrator adversarial guard validation mismatches.

Execution rules:
- Use dedicated `codex/*` branches for scoped changes.
- Commit and push after each validated logical unit.
- No destructive git commands.
- Never revert unrelated user changes.
- Prefer deterministic fixes with tests first.
- If external credentials block progress, continue all non-blocked work and log exact blocker.

Mandatory validation gates before completion:
- In /Users/sdevisch/dev/orxaq:
  make lint
  make version-check
  make repo-hygiene
  make test
- In /Users/sdevisch/dev/orxaq-ops:
  make lint
  make test
  make version-check
  make repo-hygiene
  make hosted-controls-check
- In /Users/sdevisch/dev/orxaq_gemini:
  make lint
  make typecheck
  make version-check
  make repo-hygiene
  make test
- In /Users/sdevisch/dev/odyssey:
  python run_tests.py
  python run_tests_phase32.py
  python run_phase32.py --skin-id tui_command_grid
  python run_phase32.py --skin-id space_command_center
  python run_phase32.py --skin-id industrial_analog_retrofit
  python run_phase32.py --skin-id minimalist_flat_ops
  python run_phase32.py --skin-id gamified_strategy_board

Operational success criteria (must all be true):
1) `rln-adversarial-tests` and `release-quality` are no longer blocked/pending.
2) No recurring `runner rc=2` restart loop with pending work.
3) Lanes report healthy and actionable work is continuously consumed.
4) Required validation gates pass across all repos.
5) Commits are pushed for each validated logical unit.

Reporting cadence:
- Emit checkpoint updates at least every 30 minutes with:
  changed files, commands run, pass/fail summary, next action.
- Continue automatically after each checkpoint.

Completion report format:
1) What was delivered (mapped to today’s major prompts)
2) Exact commands run and outcomes
3) Commits/branches pushed
4) Remaining blockers (if any) with concrete evidence
5) Follow-up backlog for next 24h cycle
