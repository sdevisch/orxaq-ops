# Dashboard UX Review and Redesign Report

## 1. Scope and assumptions
- Reviewed `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py` end-to-end (HTML, CSS, JS, backend snapshots).
- Reviewed `/Users/sdevisch/dev/orxaq-ops/tests/test_autonomy_dashboard.py` for expected behavior coverage.
- Runtime snapshots used:
  - lane status: 38 lanes, 10 running, health mix includes `idle`, `ok`, `stopped`, `stopped_unexpected`.
  - distributed todo: `task_total=90`, `open=83`, `active_watch_total=83`, visible active request rows capped at 40.
- Assumed primary users:
  - autonomy operator in incident/triage mode,
  - lane owner in execution mode,
  - governance reviewer in release-readiness mode.

## 2. Baseline usability audit

### IA and density baseline
- The Overview tab currently renders 19 cards in one long scroll surface.
- There are 10 inline filter controls (7 inputs + 3 selects) concentrated in mid-page.
- Multiple high-signal sections compete at the same visual weight (To-Do hero, lane controls, collab runtime, routing summaries, metrics, logs).

### Heuristic + accessibility findings (summary)
- Strong:
  - Good visibility of system state via rich telemetry cards and feed updates.
  - Good resilience fallback behavior in data fetch paths.
- Weak:
  - Information hierarchy is overloaded; priority signals compete with secondary diagnostics.
  - Interaction semantics and accessibility are incomplete for tabs and form controls.
  - Some status visuals and summaries reduce trust/scanability under high load.

## 3. Cognitive walkthrough findings

### Task A: Diagnose lane imbalance and stuck work
1. User checks lane summary and lane table.
2. User applies owner/health/sort filters.
3. User decides whether to start/stop/ensure lanes.

Friction:
- Default health filter is `running`, so stopped/degraded lanes are hidden at first view.
- `Start/Stop/Ensure` controls are adjacent with minimal guardrails.
- Lane telemetry and conversation-source errors are not integrated into a single action recommendation.

### Task B: Validate distributed todo coverage and lower-lane utilization
1. User checks Distributed To-Do KPIs.
2. User scans `active_watch` queue list for uncovered tasks.
3. User cross-references lane workload.

Friction:
- `active_watch_total` can exceed visible queue rows (capped list), causing comprehension gaps.
- Flow metrics and priority metrics are collapsed into dense text strings.
- Coverage and decomposition health are not visualized as explicit lane-band distribution.

### Task C: Verify routing/fallback/cost behavior before intervention
1. User switches to Routing tab.
2. User compares provider, lane routing config, and recent decisions.
3. User decides if routing policy or lane health should be changed first.

Friction:
- Routing details are tab-separated from lane actions, requiring context switching.
- Table-heavy layout reduces rapid anomaly detection on smaller viewports.
- No shared “decision confidence” panel combining routing anomalies and lane attention.

## 4. Literature evidence matrix

| Source | Claim | Why it applies here | Direct link |
|---|---|---|---|
| Nielsen’s 10 heuristics (NN/g) | Interfaces should emphasize visibility, consistency, user control, and minimalist design. | Dashboard overload and mixed-priority surfaces violate minimalist and hierarchy guidance. | [NN/g heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/) |
| Heuristic evaluation method (NN/g) | Multiple evaluators improve detection coverage; prioritize severe and frequent issues. | Supports severity-ranked issue register and iterative audit process for this dashboard. | [How to conduct heuristic evaluation](https://www.nngroup.com/articles/how-to-conduct-a-heuristic-evaluation/) |
| Cognitive walkthroughs (NN/g) | Task-step analysis exposes learnability and action-friction failures in realistic usage. | Fits operator workflows (triage, balancing, routing decisions) better than visual critique alone. | [Cognitive walkthroughs](https://www.nngroup.com/articles/cognitive-walkthroughs/) |
| Data tables guidance (NN/g) | Dense tables need careful structure, column treatment, and scanability support. | Current `min-width:1180` + nowrap table strategy increases horizontal-scrolling burden. | [Data tables](https://www.nngroup.com/articles/data-tables/) |
| F-shaped scanning (NN/g) | Users scan predictable zones first and often ignore lower-priority dense regions. | Explains missed insights in long single-scroll overview with many equal-weight cards. | [F-shaped reading pattern](https://www.nngroup.com/articles/f-shaped-pattern-reading-web-content/) |
| Complex app design guidelines (NN/g) | Complex tools need clear hierarchy, salient actions, and stable navigation landmarks. | Dashboard is a complex operational application with high cognitive switching cost. | [Complex applications guidelines](https://www.nngroup.com/articles/design-guidelines-complex-applications/) |
| WCAG 2.2 (W3C) | Core interaction and text must meet perceivable/operable/understandable principles (including contrast and keyboard access). | Tab semantics, labeling, and status contrast need explicit compliance hardening. | [WCAG 2.2](https://www.w3.org/TR/WCAG22/) |
| ARIA APG Tabs pattern (W3C) | Tabs need specific roles/states/keyboard interactions (`tab`, `tabpanel`, `aria-selected`, arrow keys). | Current tabs only toggle classes; semantic and keyboard pattern is incomplete. | [WAI-ARIA Tabs pattern](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/) |
| Dashboard design patterns paper (peer-reviewed) | Dashboard utility depends on matching design pattern to user task and context. | Supports role-based views and separating triage vs diagnostic depths. | [J Biomed Inform dashboard design patterns](https://pubmed.ncbi.nlm.nih.gov/34461137/) |
| Cooperative dashboards heuristics (peer-reviewed, 2024) | Coordination dashboards need explicit support for collaboration quality and team interaction. | Relevant to multi-lane swarm orchestration and operator-lane handoffs. | [Cooperative dashboard heuristics](https://pubmed.ncbi.nlm.nih.gov/39052929/) |
| Business dashboard review (peer-reviewed) | Effective dashboards require alignment between design dimensions and performance-management goals. | Reinforces KPI-to-action traceability as a core redesign requirement. | [Business dashboard design review](https://www.sciencedirect.com/science/article/pii/S1467089511000174) |

## 5. Severity-ranked issue register

| Severity | Issue | Evidence | Affected users | Impact | Confidence |
|---|---|---|---|---|---|
| Critical | Active-work visibility mismatch: total active tasks can far exceed visible list rows. | Active list is capped (`active_watch_requests = active_watch_requests[:40]`) while summary shows total (`active_watch_total`). `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:5991`, `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:6001`, `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:1804` | Operator, lane owner | Mis-triage and false confidence that queue is manageable/fully visible. | 0.95 |
| Critical | Lane signal attribution can leak across shared logs when lane IDs are missing. | Shared conversation log path is included; non-strict path allows events with empty `lane_id` to count for multiple lanes. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:5190`, `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:5211` | Operator, governance reviewer | Metric trust erosion; wrong lane-level interventions. | 0.88 |
| High | Tabs are visually styled but not implemented with full ARIA tab semantics/keyboard model. | Tablist uses generic buttons; JS toggles class only. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:613`, `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:1392` | Keyboard users, screen-reader users | Reduced operability and discoverability. | 0.93 |
| High | Critical filters use placeholders without explicit labels. | Inputs for lane/conversation filters have placeholders only. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:763`, `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:826` | Screen-reader users, novice operators | Higher error rate, lower learnability. | 0.92 |
| High | Default lane health filter hides non-running lanes at first view. | Default selected value is `running`; reset restores it. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:775`, `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:3144` | Operator in triage mode | Delayed detection of degraded/stopped lanes. | 0.90 |
| High | Table strategy is not mobile-resilient (global min width + nowrap). | `min-width:1180px` and nowrap on all table cells/headers. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:423`, `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:442` | Operators on laptops/smaller displays | Increased scanning time and missed anomalies. | 0.90 |
| High | Information architecture overload in one long overview surface. | 19 cards in Overview with many same-weight sections. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:619` | All users | High cognitive load and slower decision-making. | 0.85 |
| High | Warning status color contrast risk on white surfaces for small text. | `--warn: #c77d00` yields ~3.3:1 vs white for warning text usage. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:57` | Low-vision users | Reduced readability for warning states. | 0.80 |
| Medium | Action controls (`Ensure/Start/Stop`) lack contextual safeguards. | Adjacent control cluster without state-aware disable/confirm policy. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:789` | Operator | Increased risk of unintended lane disruption. | 0.78 |
| Medium | Dense summary strings reduce scanability and fast comprehension. | Long concatenated KPI strings in todo and lane summaries. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:1799`, `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:1973` | Operator, governance reviewer | Slower anomaly detection. | 0.84 |
| Medium | Cross-tab context switching between lane actions and routing analysis. | Routing is isolated in separate tab without shared action context. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:849` | Operator | Decision friction and context loss. | 0.72 |
| Medium | Fallback/degraded-state explanations are present but not prioritized near top decision area. | Errors appear across sections; no unified “what changed / what to do next” panel. | Operator | Higher cognitive overhead in incident response. | 0.74 |
| Low | DAW metaphor competes with operational triage on initial view hierarchy. | Large DAW card appears before core lane action panel. `/Users/sdevisch/dev/orxaq-ops/src/orxaq_autonomy/dashboard.py:638` | New users | Mild orientation delay. | 0.68 |

## 6. Redesign specification

### 6.1 Revised information architecture
- Global command bar (sticky):
  - environment state, refresh/pause, data freshness, global alert count.
- Tier 1: “Operate now” strip:
  - lane health summary,
  - active/uncovered todo throughput,
  - action recommendations.
- Tier 2 tabs:
  - `Operations`: lanes + todo distribution + immediate actions.
  - `Flow`: task decomposition/coverage trends.
  - `Routing`: provider/lane decision quality and cost.
  - `Diagnostics`: watchdog, resilience, raw feeds/log lines.

### 6.2 Component redesign details
- Tabs and keyboard semantics
  - Implement ARIA tabs pattern (`role=tab`, `role=tabpanel`, `aria-selected`, arrow-key behavior).
- Filters and forms
  - Add visible labels and helper text for all lane/conversation filters.
  - Default health filter to `all` in triage mode; allow user preference persistence.
- To-do activity module
  - Replace text-heavy summary line with segmented KPI chips + trend micro-bars.
  - Add “visible rows / total rows” disclosure and `show all` pagination/virtualization.
  - Add lane-level occupancy histogram by AI level (1-10) with target ratio overlay.
- Lane operations table
  - Mobile/tablet mode: responsive stacked cells or column-priority collapse.
  - Desktop mode: fixed key columns + optional detail drawer.
- Alerting and trust
  - Unified “data trust” ribbon (stale, partial, source mismatch, attribution risk).
  - Show per-metric provenance (source + timestamp + confidence).
- Action safety
  - For stop/start actions: add state-aware disable logic and confirm on risky transitions.
  - Add quick preview of likely impact (lanes affected, uncovered task delta).

### 6.3 Accessibility specification
- Ensure tab and filter controls are fully keyboard reachable.
- Meet WCAG 2.2 AA contrast minimum for warning/error/state text.
- Provide live-region strategy for refresh updates without overwhelming screen-reader output.

## 7. Issue-to-redesign traceability

| Redesign change | Issues addressed |
|---|---|
| Add visible row count + pagination for active watch list | Critical: active-work visibility mismatch |
| Add signal attribution guards and per-metric provenance | Critical: lane signal attribution leakage |
| Implement ARIA tab pattern + keyboard bindings | High: tab semantics and keyboard access |
| Add labels/helper text for all filter controls | High: placeholder-only form controls |
| Default health filter to `all` + persist preference | High: hidden non-running lanes |
| Responsive table strategy with column priority | High: table scanability on smaller screens |
| Tiered IA with sticky “Operate now” strip | High: information overload in Overview |
| Warning color ramp and state token audit | High: warning contrast risk |
| Action confirm and state-aware disable rules | Medium: action safety friction |
| Unified trust/alert ribbon and recommendation panel | Medium: fragmented degraded-state comprehension |

## 8. Autonomous implementation backlog

1. Accessibility and semantics hardening
   - Scope: tabs, labels, keyboard behavior, ARIA states.
   - Acceptance: keyboard-only walkthrough succeeds; accessibility checks pass.
   - Validation: targeted `tests/test_autonomy_dashboard.py` additions + manual keypath smoke.
2. Todo visibility and truthfulness
   - Scope: uncapped visibility controls, visible/total indicators, pagination.
   - Acceptance: no hidden-list ambiguity; summary matches rendered/available counts.
   - Validation: snapshot tests for `active_watch_total` and render behavior.
3. Signal attribution integrity
   - Scope: strict lane matching and confidence/provenance fields for shared logs.
   - Acceptance: no cross-lane leakage when `lane_id` missing/ambiguous.
   - Validation: unit tests for `_lane_signal_metrics` edge cases.
4. IA refactor to task-first layout
   - Scope: sticky command strip + Operations/Flow/Routing/Diagnostics structure.
   - Acceptance: top 3 operator tasks require fewer context switches.
   - Validation: cognitive walkthrough rerun with reduced friction points.
5. Responsive table redesign
   - Scope: column-priority collapse, optional detail drawers.
   - Acceptance: no forced 1180px horizontal dependence on narrow viewports.
   - Validation: viewport smoke checks + snapshot/layout assertions.
6. Action safety and guidance
   - Scope: confirm flows, disable invalid actions, impact preview panel.
   - Acceptance: risky actions require explicit acknowledgement.
   - Validation: UI behavior tests around start/stop/ensure transitions.

## 9. Residual risks and next iteration triggers
- Residual risk:
  - Existing telemetry sources may still produce ambiguous event ownership in edge cases.
  - Large-scale IA changes can temporarily increase operator relearning cost.
- Trigger next iteration when:
  - Any Critical issue remains unresolved.
  - High issue confidence drops below 0.7 after implementation.
  - Operator task success rate does not improve on walkthrough rerun.

## Execution summary (this autonomous pass)
- Completed:
  - Baseline code/runtime audit.
  - Literature-backed evidence synthesis.
  - Severity-ranked issue register.
  - Redesign architecture and autonomous backlog.
- Deferred to implementation pass:
  - Direct code/UI changes and automated UX regression checks.
  - Lane-slice execution and validation evidence per slice.
