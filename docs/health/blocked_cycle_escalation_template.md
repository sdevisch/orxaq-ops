# Blocked Cycle Escalation Template

Use this template when an autonomous remediation cycle remains blocked after best-effort recovery.

- status: `blocked`
- blocker_id: `<stable-id>`
- blocker_reason: `<concise root cause>`
- escalation_target: `<team/lane/tier>`
- next_action: `<deterministic next step>`
- evidence:
  - `<artifact-path-or-key-fact>`
- generated_utc: `<ISO-8601 UTC>`

## Rules

- Keep entries deterministic and artifact-backed.
- Prefer T1 remediation tasks first; escalate tier only on explicit trigger.
- Include rollback-safe next actions and verification commands when possible.
- Do not mark complete without artifact evidence.
