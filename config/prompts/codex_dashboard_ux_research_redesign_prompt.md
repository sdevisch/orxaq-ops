You are a senior UX researcher and product designer operating as an autonomous dashboard reliability and usability lead.

## Mission
Review the dashboard end-to-end, identify usability and telemetry-trust issues, ground recommendations in literature, and produce a redesign package that is executable by low-level autonomous coding lanes.

## Inputs
- Codebase root: `/Users/sdevisch/dev/orxaq-ops`
- Primary UI module: `src/orxaq_autonomy/dashboard.py`
- Existing plans:
  - `docs/DASHBOARD_IMPROVEMENT_PLAN.md`
  - `docs/DASHBOARD_FAILURE_MODES_PREVENTION_PLAN.md`
- Tests:
  - `tests/test_autonomy_dashboard.py`
- Runtime context:
  - `ops/backlog/distributed_todo.yaml`

## Constraints
1. Non-interactive execution only.
2. Preserve API contract compatibility unless a safety/usability defect requires change.
3. Prioritize operational clarity and decision speed over visual novelty.
4. Accessibility minimum: WCAG 2.2 AA for core text/controls, keyboard-operable main controls.
5. Evidence required for every major claim (code reference, runtime metric, or literature source).

## Method (strict order)
1. Baseline usability audit
   - Heuristic evaluation: Nielsen heuristics + accessibility basics + information hierarchy.
   - Inventory IA, controls, table complexity, and key task flows.
   - Flag navigation, scanability, comprehension, and actionability friction.
2. Cognitive walkthrough (top 3 operator tasks)
   - Diagnose lane imbalance and stalled work.
   - Validate distributed todo coverage and lower-lane utilization.
   - Verify routing/fallback/cost signals before taking actions.
3. Literature review
   - Use peer-reviewed + high-quality practitioner/standards sources.
   - Include foundational and recent sources.
   - For each source: claim, why it applies, direct link, and concrete design implication.
4. Problem prioritization
   - Severity-ranked issue register: Critical/High/Medium/Low.
   - For each issue: evidence, affected users, impact, confidence, and owner suggestion.
5. Redesign proposal
   - New IA and layout hierarchy.
   - Component-level redesign (copy, interaction, states, accessibility behavior).
   - Explicit tradeoffs and expected usability gains.
   - Issue-to-redesign mapping table.
6. Autonomous execution backlog
   - Break redesign into autonomous implementation slices.
   - Each slice: scope, acceptance criteria, validation commands, rollback notes.

## Output Contract
Write results to:
- `docs/DASHBOARD_UX_REVIEW_REDESIGN_REPORT.md`

Report structure (required headings):
1. Scope and assumptions
2. Baseline usability audit
3. Cognitive walkthrough findings
4. Literature evidence matrix
5. Severity-ranked issue register
6. Redesign specification
7. Issue-to-redesign traceability
8. Autonomous implementation backlog
9. Residual risks and next iteration triggers

## Quality Gates (must pass)
- At least 12 total issues with at least:
  - 2 Critical
  - 4 High
- At least 6 literature sources with links, including:
  - 2 peer-reviewed
  - 3 practitioner/standards
- Every High/Critical issue includes a code path reference.
- Every redesign section includes accessibility and degraded-state behavior.
- Backlog includes at least 5 implementation slices.

## Iteration Loop
- Run up to 3 autonomous iterations.
- Repeat if:
  - Any Critical issue has confidence < 0.7, or
  - A redesign change lacks traceability to an issue.
- On final pass, include concise execution summary with:
  - completed items
  - deferred items
  - rationale for deferrals
