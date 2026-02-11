# Workspace Preferences

## Identity-Scoped Autonomy (High-Capability Mode)

`policy_version`: `2.1`
`effective_date`: `2026-02-11`

This autonomy policy is identity-scoped and non-transferable.
It applies only to an agent instance that exactly matches an active identity registry entry.

## Non-Transferability Clause (Normative)

If any identity or runtime field differs, autonomy permission is denied.
Agents that cannot prove an exact match MUST set `autonomy_authorized=false`.
No partial, inferred, approximate, or "close enough" match is valid.

## Authorization Predicate (Normative)

Set:
`AUTONOMY_AUTHORIZED = ID_MATCH && FINGERPRINT_MATCH && RUNTIME_MATCH && USER_GRANT && !CRITICAL_SECURITY_DECISION`

Definitions:
- `ID_MATCH`: exact equality for `identity_id` and `identity_subject_tuple`.
- `FINGERPRINT_MATCH`: exact equality for deterministic identity fingerprint.
- `RUNTIME_MATCH`: all required runtime markers are present and equal.
- `USER_GRANT`: explicit autonomy grant exists in the active thread.
- `CRITICAL_SECURITY_DECISION`: action changes secrets, auth, privileged egress, sandbox policy, allowlist policy, or security gate defaults.

If `AUTONOMY_AUTHORIZED=false`, pause and request explicit confirmation.

## Autonomy Identity Registry

### Active Entry
- `identity_id`: `codex.gpt5.high.v1`
- `agent_name`: `Codex`
- `model_family`: `GPT-5`
- `capability_tier`: `high`
- `identity_subject_tuple`: `("Codex","GPT-5","high","codex.gpt5.high.v1")`
- `identity_fingerprint_algo`: `sha256(identity_id|agent_name|model_family|capability_tier)`
- `identity_fingerprint`: `1eb66ac9f1c2f67e8f7c5b73404da1c321bc7898f7ea0d4c926852ecd37b95e1`
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
- `identity_id_pattern`: `codex.gpt<major>.high.v<minor>`
- `entry_status`: `reserved`
- `minimum_required_fields`:
  - `identity_id`
  - `agent_name`
  - `model_family`
  - `capability_tier`
  - `identity_subject_tuple`
  - `identity_fingerprint_algo`
  - `identity_fingerprint`
  - `runtime_markers_required`
  - `autonomy_scope`
  - `entry_status=active`

### Activation Rules for Future Models
- Reserved entries are unauthorized by default.
- A future model entry is authorized only when all required fields are populated and `entry_status=active`.
- Activation for one `identity_id` never grants authorization to any other `identity_id`.
- Every new active entry must define its own fingerprint and runtime markers.

## Applicability and Non-Applicability Checks

Autonomy permission applies only if all checks pass:
1. `identity_id` exists and is `active`.
2. exact subject tuple match.
3. exact identity fingerprint match.
4. exact runtime marker match.
5. explicit user autonomy grant in current thread.
6. action is not a critical security decision.

Autonomy permission does not apply when any check fails.

### Third-Party Safety Check

Any other agent can safely determine this policy is not theirs by checking:
1. `identity_id != codex.gpt5.high.v1`, or
2. subject tuple mismatch, or
3. fingerprint mismatch, or
4. runtime marker mismatch.

If any condition is true, this policy does not grant full autonomy to that agent.

## Distinguishing Signature (Machine-Checkable)

Required self-asserted signature fields:
- `identity_id`
- `agent_name`
- `model_family`
- `capability_tier`
- `identity_fingerprint`
- `runtime_markers`
- `autonomy_authorized`

Expected active signature:
- `identity_id=codex.gpt5.high.v1`
- `agent_name=Codex`
- `model_family=GPT-5`
- `capability_tier=high`
- `identity_fingerprint=1eb66ac9f1c2f67e8f7c5b73404da1c321bc7898f7ea0d4c926852ecd37b95e1`
- `runtime_markers.approval_policy=never`
- `runtime_markers.sandbox_mode=danger-full-access`

If any value differs, this policy does not grant full autonomy.

## Self-Assessment Protocol (Required Per Run)

Each autonomous run must emit:
- `identity_id`
- `identity_match` (`true|false`)
- `fingerprint_match` (`true|false`)
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
