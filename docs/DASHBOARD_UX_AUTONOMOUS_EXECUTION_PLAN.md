# Dashboard UX Autonomous Execution Plan

## Objective
Produce a rigorous, literature-grounded dashboard UX review and redesign package that can be executed non-interactively and iterated safely.

## Scope
- Product surface: autonomy dashboard in `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py`.
- Core users:
  - Autonomy operator (incident triage, lane balancing, health checks).
  - Lane owner (task-level execution visibility).
  - Governance/release reviewer (quality, routing, cost, risk).
- Constraints:
  - Keep existing Python HTTP server architecture and vanilla JS rendering model.
  - Preserve current API payload compatibility.
  - Meet WCAG 2.2 AA for key text/controls and keyboard operability.

## Execution Phases
1. Baseline capture
   - Extract current IA, card inventory, control inventory, and table complexity.
   - Capture live data characteristics from `_safe_lane_status_snapshot`, `_safe_collab_runtime_snapshot`, and `_safe_distributed_todo_snapshot`.
   - Output: baseline metrics block.
2. Usability audit
   - Run heuristic evaluation (Nielsen 10 + accessibility + information hierarchy).
   - Score each issue: severity (Critical/High/Medium/Low), confidence, affected user cohort.
   - Output: issue register with code evidence.
3. Cognitive walkthrough
   - Walk top 3 tasks:
     - Diagnose lane imbalance and stuck lanes.
     - Validate distributed todo throughput and lower-lane occupancy.
     - Verify routing/fallback/cost behavior before control actions.
   - Output: step-by-step friction map and failure points.
4. Literature synthesis
   - Gather peer-reviewed and high-quality practitioner sources.
   - For each source: claim, dashboard relevance, direct link, implementation implication.
   - Output: evidence matrix.
5. Redesign proposal
   - Propose revised IA and layout hierarchy.
   - Redesign key components: top summary, lane table, todo widget, collab telemetry, filters/actions.
   - Define interaction states: loading, partial, stale, degraded, empty, action success/failure.
   - Output: redesign specification + change rationale.
6. Prioritized implementation backlog
   - Convert redesign into execution slices suitable for low-level autonomous lanes.
   - Include acceptance criteria and validation checks for each slice.
   - Output: lane-ready execution backlog.
7. Validation and iteration
   - Validate report completeness against required method.
   - If unresolved Critical issues remain or confidence < 0.7 on High issues, run another iteration.
   - Stop at max 3 iterations per run and log residual risk.

## Deliverables
- `docs/DASHBOARD_UX_REVIEW_REDESIGN_REPORT.md`
- `config/prompts/codex_dashboard_ux_research_redesign_prompt.md`
- Optional follow-on implementation plan updates in existing dashboard docs.

## Acceptance Gates
- Includes all 4 required method blocks: baseline audit, literature review, prioritization, redesign.
- Every issue includes: evidence, affected users, impact, confidence.
- Every redesign change maps back to at least one issue.
- Literature section contains at least:
  - 2 peer-reviewed sources.
  - 3 practitioner/standards sources.
- Accessibility coverage includes:
  - Keyboard tab semantics.
  - Form labeling.
  - Color contrast risks.

## Autonomous Iteration Loop
1. Run audit and produce issue list.
2. If issue evidence is weak, gather additional code/runtime data and rescore.
3. Update redesign mapping and implementation backlog.
4. Re-run quality gate checklist.
5. Stop when all gates pass or iteration limit reached.

## Risks and Mitigations
- Risk: Over-index on visual polish over operational clarity.
  - Mitigation: enforce task-success metrics and cognitive walkthrough gates.
- Risk: Data-trust defects masked as UI defects.
  - Mitigation: include telemetry validity checks in Critical issue screening.
- Risk: Excessive redesign scope for current architecture.
  - Mitigation: split into thin slices with backward-compatible API/UI changes first.
