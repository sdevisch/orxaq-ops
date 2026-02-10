# Cross-Model Review Request

Task: task-dashboard-todo-metrics-fix  
Issue: #1  
Branch: codex/issue-1-dashboard-todo-metrics-fix-lane1-20260210155150

Requested reviewer: Gemini

Scope to review:
- src/orxaq_autonomy/dashboard.py
- tests/test_autonomy_dashboard.py

Expected checks:
- Deterministic handling of None/non-dict payloads.
- Input immutability regression coverage.
- Symmetric missing-field normalization (`live_covered` absent while `live_uncovered` is present).
- No behavior regressions outside dashboard todo metric normalization tests.

Status: requested (pending execution by Gemini lane)

Patch commit under review: pending (will be updated after commit)
