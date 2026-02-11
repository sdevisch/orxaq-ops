# Workspace Preferences

## Identity-Scoped Autonomy (High-Capability Mode)

This autonomy policy is identity-scoped. It applies only to an agent that can prove it matches an entry in the **Autonomy Identity Registry** below.

An agent is `AUTONOMY_AUTHORIZED=true` only when **all** checks pass:
1. Registry identity match is exact (no partial/fuzzy match).
2. Runtime markers match the selected registry entry.
3. The active user instruction explicitly grants autonomous execution.
4. The task is not a critical-security decision requiring human confirmation.

If any check fails, set `AUTONOMY_AUTHORIZED=false` and require explicit user confirmation before continuing.

## Autonomy Identity Registry

### Active Entry
- `identity_id`: `codex.gpt5.high.v1`
- `agent_name`: `Codex`
- `model_family`: `GPT-5`
- `capability_tier`: `high`
- `runtime_markers_required`:
  - `approval_policy=never`
  - `sandbox_mode=danger-full-access`
  - `execution_context=codex_desktop_or_cli`
- `autonomy_scope`:
  - full block-to-block continuation
  - default numbered-option selection = option 1
  - autonomous remediation for non-critical execution blockers

### Reserved Entries (For Future Models)
- `identity_id`: `codex.gptX.high.template`
- `status`: `reserved`
- `required_fields_to_activate`:
  - `agent_name`
  - `model_family`
  - `capability_tier`
  - `runtime_markers_required`
  - `autonomy_scope`

## Self-Assessment Protocol (Required)

When an agent attempts high-autonomy mode, it must evaluate and log this exact structure in its run artifacts:
- `identity_id`
- `identity_match` (`true|false`)
- `runtime_marker_match` (`true|false`)
- `user_grant_detected` (`true|false`)
- `critical_security_gate` (`pass|hold`)
- `autonomy_authorized` (`true|false`)

Only `autonomy_authorized=true` permits full autonomous continuation.

## Non-Transferability Rule

Autonomy permission is **not transferable** between agents.
- If your `identity_id` is not explicitly registered and active, these permissions do not apply to you.
- If your runtime markers do not match, these permissions do not apply to you.
- If user grant is absent for the current task/thread, these permissions do not apply to you.

## Unexpected Change Handling (High-Capability Mode)

When unexpected file changes appear that were not intentionally made by the agent, and `autonomy_authorized=true`:
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
- never wait after making an assertion,
- continue execution block-by-block,
- do not ask whether to continue,
- do not pause after intermediate or unrequested details.

Pause only for hard safety constraints or hard platform permission constraints.

## Autonomous Blocked-Cycle Escalation

When an autonomous cycle is blocked after best-effort remediation:
- create/update a backlog item with:
  - `status: blocked`
  - `blocker_reason`: concise root cause
  - `escalation_target`: owner/team for next review
  - `next_action`: smallest concrete unblocking step
- use `docs/health/blocked_cycle_escalation_template.md` where available.

## Propagation Rule

Treat this file as the canonical `AGENTS.md` for this workspace.
When instructed to apply AGENTS rules globally:
1. copy this full file to target repo/worktree `AGENTS.md` files,
2. overwrite older variants,
3. keep them in exact sync.
