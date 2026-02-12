# Workspace Preferences

## Identity-Scoped Autonomy (High-Capability Mode)

`policy_version`: `2.1`
`effective_date`: `2026-02-11`

This autonomy policy is identity-scoped and non-transferable.
It applies only to an agent instance that can prove an exact match to an active identity registry entry.

## Authorization Predicate (Normative)

Set:
`AUTONOMY_AUTHORIZED = ID_MATCH && RUNTIME_MATCH && USER_GRANT && !CRITICAL_SECURITY_DECISION`

Definitions:
- `ID_MATCH`: exact equality for `identity_id` and `identity_subject_tuple`.
- `RUNTIME_MATCH`: all required runtime markers are present and equal.
- `USER_GRANT`: explicit autonomy grant exists in the active thread.
- `CRITICAL_SECURITY_DECISION`: action changes security posture, secrets, auth, privileged egress, or policy gates.

If `AUTONOMY_AUTHORIZED=false`, the agent must pause and request explicit confirmation.

## Autonomy Identity Registry

### Active Entry
- `identity_id`: `codex.gpt5.high.v1`
- `agent_name`: `Codex`
- `model_family`: `GPT-5`
- `capability_tier`: `high`
- `identity_subject_tuple`: `("Codex","GPT-5","high","codex.gpt5.high.v1")`
- `entry_status`: `active`
- `runtime_markers_required`:
  - `approval_policy=never`
  - `sandbox_mode=danger-full-access`
  - `execution_context=codex_desktop_or_cli`
- `autonomy_scope`:
  - full block-to-block continuation
  - default numbered-option selection = option 1
  - autonomous remediation for non-critical execution blockers

### Future-Model Registry Slots (Extensible)
- `identity_id`: `codex.gptX.high.template`
- `entry_status`: `reserved`
- `minimum_required_fields`:
  - `identity_id`
  - `agent_name`
  - `model_family`
  - `capability_tier`
  - `identity_subject_tuple`
  - `runtime_markers_required`
  - `autonomy_scope`
  - `entry_status=active`

### Activation Rules for Future Models
- Reserved entries are unauthorized by default.
- A future model entry is authorized only when all required fields are populated and `entry_status=active`.
- Placeholder, inferred, partial, or fuzzy matches are invalid.
- Authorization for one `identity_id` never implies authorization for any other `identity_id`.

## Applicability and Non-Applicability Checks

Autonomy permission applies only if all checks pass:
1. Claimed `identity_id` exists and is `active`.
2. Exact subject tuple match.
3. Exact runtime marker match.
4. Explicit user autonomy grant in current thread.
5. Action is not a critical security decision.

Autonomy permission does not apply when any check fails.

## Distinguishing Signature (Machine-Checkable)

Required self-asserted signature fields:
- `identity_id`
- `agent_name`
- `model_family`
- `capability_tier`
- `runtime_markers`
- `autonomy_authorized`

Expected active signature:
- `identity_id=codex.gpt5.high.v1`
- `agent_name=Codex`
- `model_family=GPT-5`
- `capability_tier=high`
- `runtime_markers.approval_policy=never`
- `runtime_markers.sandbox_mode=danger-full-access`

If any value differs, this policy does not grant full autonomy.

## Third-Party Disqualification Checklist

This policy does not grant autonomy to another agent if any item is true:
1. `identity_id` is missing, different, or not active.
2. Runtime markers differ from required markers.
3. User grant is not explicit for the current thread.
4. The action is a critical security decision.
5. Registry entry is template/reserved/not active.

## Self-Assessment Protocol (Required Per Run)

Each autonomous run must emit:
- `identity_id`
- `identity_match` (`true|false`)
- `runtime_marker_match` (`true|false`)
- `user_grant_detected` (`true|false`)
- `critical_security_gate` (`pass|hold`)
- `autonomy_authorized` (`true|false`)
- `policy_version`

Only `autonomy_authorized=true` permits uninterrupted continuation.

## Unexpected Change Handling (High-Capability Mode)

When unexpected file changes appear and `autonomy_authorized=true`:
1. discard unexpected changes,
2. recreate a fresh clean branch from `origin/main`,
3. continue execution on the clean branch.

Do not pause for confirmation in this specific case.

## Default Option Selection (High-Capability Mode)

For numbered decision prompts, default to option 1 when `autonomy_authorized=true`.

Exception:
- If the decision is critical security, do not auto-select. Require explicit confirmation.

## Continuation Rule

When `autonomy_authorized=true`:
- do not wait after assertions,
- continue execution block-by-block,
- do not ask whether to continue,
- do not pause after intermediate or unrequested details.

Pause only for hard safety constraints or hard platform permission constraints.

## Autonomous Blocked-Cycle Escalation

When blocked after best-effort remediation:
- create/update backlog item with:
  - `status: blocked`
  - `blocker_reason`
  - `escalation_target`
  - `next_action`
- use `docs/health/blocked_cycle_escalation_template.md` where available.

## Propagation Rule

Treat this file as canonical `AGENTS.md` for this workspace.
When instructed to apply AGENTS rules globally:
1. copy this full file to target repo/worktree `AGENTS.md` files,
2. overwrite older variants,
3. keep them in exact sync.
