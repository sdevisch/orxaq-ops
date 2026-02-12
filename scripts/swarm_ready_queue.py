#!/usr/bin/env python3
"""Build a deterministic one-week-ready queue for T1 workers from health artifacts."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return raw if isinstance(raw, dict) else {}


def _refresh_todo_summary(root: Path, summary_path: Path) -> dict[str, Any]:
    checker = (root / "scripts" / "swarm_todo_health_current.py").resolve()
    result = {
        "attempted": True,
        "ok": False,
        "reason": "",
        "returncode": -1,
    }
    if not checker.exists():
        result["reason"] = "todo_health_checker_missing"
        return result
    cmd = [
        sys.executable,
        str(checker),
        "--root",
        str(root),
        "--json",
        "--output-file",
        str(summary_path),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    result["returncode"] = int(proc.returncode)
    if proc.returncode == 0:
        result["ok"] = True
        result["reason"] = ""
    else:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        result["reason"] = stderr[:220] or stdout[:220] or "todo_health_refresh_failed"
    return result


def _task(
    *,
    task_id: str,
    title: str,
    priority: str,
    rationale: str,
    owner_hint: str = "t1",
    acceptance: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "priority_band": priority,
        "owner_hint": owner_hint,
        "status": "todo",
        "rationale": rationale,
        "acceptance_criteria": acceptance or [],
    }


def build_queue(
    root: Path,
    max_items: int,
    *,
    refresh_todo_summary: bool = True,
) -> dict[str, Any]:
    artifacts = root / "artifacts"
    auto_artifacts = artifacts / "autonomy"
    provider_summary = auto_artifacts / "provider_costs" / "summary.json"
    provider_cost_health_summary = auto_artifacts / "provider_cost_health.json"
    connectivity_summary = artifacts / "model_connectivity.json"
    todo_summary = auto_artifacts / "swarm_todo_health" / "current_latest.json"
    t1_policy_summary = auto_artifacts / "t1_basic_model_policy.json"
    privilege_policy_summary = auto_artifacts / "privilege_policy_health.json"
    git_delivery_policy_summary = auto_artifacts / "git_delivery_policy_health.json"
    git_hygiene_summary = auto_artifacts / "git_hygiene_health.json"
    git_hygiene_remediation_summary = auto_artifacts / "git_hygiene_remediation.json"
    backend_upgrade_policy_summary = auto_artifacts / "backend_upgrade_policy_health.json"
    api_interop_policy_summary = auto_artifacts / "api_interop_policy_health.json"
    backlog_control_summary = auto_artifacts / "deterministic_backlog_health.json"
    pr_approval_remediation_summary = auto_artifacts / "pr_approval_remediation.json"

    provider = _load_json(provider_summary)
    provider_cost_health = _load_json(provider_cost_health_summary)
    connectivity = _load_json(connectivity_summary)
    todo_refresh = {
        "attempted": False,
        "ok": True,
        "reason": "disabled",
        "returncode": 0,
    }
    if refresh_todo_summary:
        todo_refresh = _refresh_todo_summary(root, todo_summary)

    todo = _load_json(todo_summary)
    t1_policy = _load_json(t1_policy_summary)
    privilege_policy = _load_json(privilege_policy_summary)
    git_delivery_policy = _load_json(git_delivery_policy_summary)
    git_hygiene = _load_json(git_hygiene_summary)
    git_hygiene_remediation = _load_json(git_hygiene_remediation_summary)
    backend_upgrade_policy = _load_json(backend_upgrade_policy_summary)
    api_interop_policy = _load_json(api_interop_policy_summary)
    backlog_control = _load_json(backlog_control_summary)
    pr_approval_remediation = _load_json(pr_approval_remediation_summary)

    tasks: list[dict[str, Any]] = []

    if refresh_todo_summary and not bool(todo_refresh.get("ok", False)) and not todo:
        tasks.append(
            _task(
                task_id="T1-TODO-HEALTH-REFRESH",
                title="Restore current todo health refresh path",
                priority="P1",
                rationale=(
                    "ready queue could not refresh or load swarm_todo_health current snapshot "
                    f"(reason={str(todo_refresh.get('reason', '')).strip() or 'unknown'})."
                ),
                acceptance=[
                    "swarm_todo_health_current checker runs successfully from ready-queue path.",
                    "current_latest.json is refreshed before queue generation.",
                    "ready queue summary includes current unassigned/stale todo metrics.",
                ],
            )
        )

    provider_ok = bool(provider.get("ok", False))
    provider_health_ok = bool(provider_cost_health.get("ok", provider_ok))
    provider_telemetry_mode = (
        str(provider_cost_health.get("provider_telemetry_mode", "")).strip().lower() or "unknown"
    )
    provider_required = (
        provider_cost_health.get("required_providers", [])
        if isinstance(provider_cost_health.get("required_providers"), list)
        else []
    )
    provider_unconfigured = provider_telemetry_mode == "unconfigured" and len(provider_required) == 0
    provider_health_effective_ok = provider_health_ok or provider_unconfigured

    endpoint_unhealthy = int(connectivity.get("endpoint_unhealthy", 0) or 0)
    endpoints = connectivity.get("endpoints", [])
    if isinstance(endpoints, list):
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            if bool(endpoint.get("ok", False)):
                continue
            endpoint_required = bool(endpoint.get("required", False))
            endpoint_auth_configured = bool(endpoint.get("auth_configured", False))
            # In explicitly unconfigured mode, optional endpoint auth failures are expected noise.
            if provider_unconfigured and not endpoint_required and not endpoint_auth_configured:
                continue
            endpoint_id = str(endpoint.get("id", "unknown")).strip() or "unknown"
            status_code = int(endpoint.get("status_code", 0) or 0)
            reason = str(endpoint.get("error", "")).strip()[:180]
            tasks.append(
                _task(
                    task_id=f"T1-CONN-{endpoint_id}",
                    title=f"Restore connectivity: {endpoint_id}",
                    priority="P1",
                    rationale=f"endpoint unhealthy status={status_code}; {reason}",
                    acceptance=[
                        f"Connectivity check reports {endpoint_id} as healthy.",
                        "Any required credentials/routing changes are documented.",
                        "No unrelated endpoint regressions introduced.",
                    ],
                )
            )

    stale_files = int(
        (todo.get("distributed_todo", {}) if isinstance(todo.get("distributed_todo"), dict) else {}).get("stale_file_count", 0)
        or 0
    )
    if stale_files > 0:
        tasks.append(
            _task(
                task_id="T1-BACKLOG-STALE-CLEANUP",
                title="Resolve stale distributed backlog files",
                priority="P1",
                rationale=f"{stale_files} stale distributed_todo files detected in active scope.",
                acceptance=[
                    "current swarm todo health reports stale_file_count=0.",
                    "Backlog ownership/active swarm assignment is explicit.",
                    "No historical archival files are treated as active scope.",
                ],
            )
        )

    unassigned_active = int(
        (todo.get("distributed_todo", {}) if isinstance(todo.get("distributed_todo"), dict) else {}).get("unassigned_active_task_total", 0)
        or 0
    )
    if unassigned_active > 0:
        tasks.append(
            _task(
                task_id="T1-BACKLOG-ASSIGNMENT",
                title="Assign unowned active backlog tasks",
                priority="P1",
                rationale=f"{unassigned_active} active tasks are currently unassigned.",
                acceptance=[
                    "All active backlog tasks have assigned_swarm and owner hints.",
                    "No active task remains unassigned in current todo health snapshot.",
                ],
            )
        )

    budget_payload = (
        provider_cost_health.get("budget", {})
        if isinstance(provider_cost_health.get("budget"), dict)
        else {}
    )
    budget_enabled = bool(budget_payload.get("enabled", False))
    budget_state = str(budget_payload.get("state", "disabled")).strip().lower() or "disabled"
    budget_daily_spend = float(budget_payload.get("daily_spend_usd", 0.0) or 0.0)
    budget_daily_cap = float(budget_payload.get("daily_budget_usd", 0.0) or 0.0)
    budget_remaining = float(budget_payload.get("daily_remaining_usd", 0.0) or 0.0)
    if not provider_health_effective_ok:
        tasks.append(
            _task(
                task_id="T1-PROVIDER-COST-FRESHNESS",
                title="Restore provider cost telemetry freshness",
                priority="P2",
                rationale="provider cost health artifact reports stale/unknown freshness or policy failure.",
                acceptance=[
                    "provider-cost-health passes without freshness failures.",
                    "Provider summary includes non-stale timestamp metadata.",
                ],
            )
        )
    if budget_enabled and budget_state == "exceeded":
        tasks.append(
            _task(
                task_id="T1-SWARM-BUDGET-CAP-ENFORCEMENT",
                title="Enforce swarm daily budget cap",
                priority="P0",
                rationale=(
                    f"daily spend exceeded cap (today=${budget_daily_spend:.4f}, cap=${budget_daily_cap:.4f}, "
                    f"remaining=${budget_remaining:.4f})."
                ),
                acceptance=[
                    "Daily spend is at or below configured cap in provider_cost_health budget state.",
                    "Non-critical lane scale-up/start actions are held when cap is reached.",
                    "Dashboard clearly reports spend, cap, remaining budget, and hard-stop status.",
                ],
            )
        )
    elif budget_enabled and budget_state == "warning":
        tasks.append(
            _task(
                task_id="T1-SWARM-BUDGET-WARNING-MITIGATION",
                title="Mitigate near-cap swarm spend",
                priority="P1",
                rationale=(
                    f"daily spend near cap (today=${budget_daily_spend:.4f}, cap=${budget_daily_cap:.4f}, "
                    f"remaining=${budget_remaining:.4f})."
                ),
                acceptance=[
                    "Route remaining basic work to cheapest eligible T1 lanes.",
                    "High-cost optional tasks are deferred or throttled until next budget window.",
                    "provider_cost_health budget state returns to ok with positive headroom.",
                ],
            )
        )

    t1_policy_ok = bool(t1_policy.get("ok", True))
    t1_policy_summary_payload = t1_policy.get("summary", {}) if isinstance(t1_policy.get("summary"), dict) else {}
    t1_policy_observability = t1_policy.get("observability", {}) if isinstance(t1_policy.get("observability"), dict) else {}
    t1_violations = int(t1_policy_summary_payload.get("violation_count", 0) or 0)
    t1_observability_ok = bool(t1_policy_observability.get("ok", True))
    t1_latest_metric_age = int(t1_policy_observability.get("latest_metric_age_minutes", -1) or -1)
    if not t1_policy_ok or t1_violations > 0:
        rationale_bits: list[str] = []
        if t1_violations > 0:
            rationale_bits.append(f"{t1_violations} policy violation(s)")
        if not t1_observability_ok:
            rationale_bits.append(f"monitoring weak (observability=false, latest_metric_age_min={t1_latest_metric_age})")
        rationale = "; ".join(rationale_bits) if rationale_bits else "policy report not healthy."
        tasks.append(
            _task(
                task_id="T1-MODEL-POLICY-ENFORCEMENT",
                title="Enforce T1-only basic coding model policy",
                priority="P0",
                rationale=rationale,
                acceptance=[
                    "t1_basic_model_policy report shows violation_count=0.",
                    "Basic coding tasks route only to configured T1 models unless escalated.",
                    "Escalation rationale is present for every non-T1 exception.",
                    "Observability checks remain healthy (fresh metrics, required routing fields present).",
                ],
            )
        )

    privilege_ok = bool(privilege_policy.get("ok", True))
    privilege_summary_payload = privilege_policy.get("summary", {}) if isinstance(privilege_policy.get("summary"), dict) else {}
    privilege_violations = int(privilege_summary_payload.get("violation_count", 0) or 0)
    privilege_scanned_events = int(privilege_summary_payload.get("scanned_events", 0) or 0)
    if not privilege_ok or privilege_violations > 0:
        tasks.append(
            _task(
                task_id="T1-PRIVILEGE-POLICY-ENFORCEMENT",
                title="Enforce non-admin defaults and breakglass controls",
                priority="P0",
                rationale=(
                    f"privilege policy unhealthy (ok={privilege_ok}, violations={privilege_violations})."
                ),
                acceptance=[
                    "privilege_policy_health report shows ok=true and violation_count=0.",
                    "Least-privilege mode is default for swarm and RPA coding lanes.",
                    "Any elevated execution has reason, scope, TTL, rollback proof, and audit trace.",
                ],
            )
        )
    elif privilege_scanned_events == 0:
        tasks.append(
            _task(
                task_id="T1-PRIVILEGE-TELEMETRY-SEED",
                title="Exercise least-privilege telemetry path",
                priority="P1",
                rationale="Privilege policy is green but no execution events were captured in lookback window.",
                acceptance=[
                    "At least one least_privilege execution event is present in privilege_escalations.ndjson.",
                    "privilege_policy_health summary.scanned_events > 0 in next cycle.",
                    "No elevated flags appear without breakglass evidence.",
                ],
            )
        )

    git_delivery_ok = bool(git_delivery_policy.get("ok", True))
    git_delivery_summary_payload = (
        git_delivery_policy.get("summary", {})
        if isinstance(git_delivery_policy.get("summary"), dict)
        else {}
    )
    git_delivery_violations = int(git_delivery_summary_payload.get("violation_count", 0) or 0)
    git_delivery_branch = str(git_delivery_summary_payload.get("branch", "")).strip()
    git_delivery_effective_changed_lines = int(git_delivery_summary_payload.get("effective_changed_lines", 0) or 0)
    git_delivery_max_changed_lines = int(git_delivery_summary_payload.get("max_changed_lines", 0) or 0)
    git_delivery_pr_found = bool(git_delivery_summary_payload.get("pr_found", False))
    git_delivery_pr_approvals = int(git_delivery_summary_payload.get("pr_approvals", 0) or 0)
    if not git_delivery_ok or git_delivery_violations > 0:
        tasks.append(
            _task(
                task_id="T1-GIT-DELIVERY-POLICY-ENFORCEMENT",
                title="Enforce ticket-branch and PR delivery workflow",
                priority="P0",
                rationale=(
                    "git delivery policy unhealthy "
                    f"(ok={git_delivery_ok}, violations={git_delivery_violations}, "
                    f"branch={git_delivery_branch}, changed_lines={git_delivery_effective_changed_lines}/"
                    f"{git_delivery_max_changed_lines}, pr_found={git_delivery_pr_found}, "
                    f"pr_approvals={git_delivery_pr_approvals})."
                ),
                acceptance=[
                    "git_delivery_policy_health report shows ok=true and violation_count=0.",
                    "Active branch is ticket-linked (for example `codex/issue-<id>-topic`).",
                    "Current change block stays under configured threshold (<400 lines) unless explicit override evidence exists.",
                    "PR exists for branch and has review + required approval(s).",
                ],
            )
        )

    git_hygiene_summary_payload = (
        git_hygiene.get("summary", {})
        if isinstance(git_hygiene.get("summary"), dict)
        else {}
    )
    git_hygiene_ok = bool(git_hygiene.get("ok", True))
    git_hygiene_violations = int(git_hygiene_summary_payload.get("violation_count", 0) or 0)
    git_hygiene_total = int(git_hygiene_summary_payload.get("total_branch_count", 0) or 0)
    git_hygiene_max_total = int(git_hygiene_summary_payload.get("max_total_branches", 0) or 0)
    git_hygiene_stale_local = int(git_hygiene_summary_payload.get("stale_local_branch_count", 0) or 0)
    if not git_hygiene_ok or git_hygiene_violations > 0:
        tasks.append(
            _task(
                task_id="T1-GIT-HYGIENE-ENFORCEMENT",
                title="Enforce git branch hygiene controls",
                priority="P1",
                rationale=(
                    "git hygiene policy unhealthy "
                    f"(ok={git_hygiene_ok}, violations={git_hygiene_violations}, "
                    f"total_branches={git_hygiene_total}/{git_hygiene_max_total}, "
                    f"stale_local={git_hygiene_stale_local})."
                ),
                acceptance=[
                    "git_hygiene_health report shows ok=true and violation_count=0.",
                    "Total branch count stays within configured threshold.",
                    "Stale local branches are pruned or archived with transparent audit trail.",
                ],
            )
        )

    remediation_summary = (
        git_hygiene_remediation.get("summary", {})
        if isinstance(git_hygiene_remediation.get("summary"), dict)
        else {}
    )
    remediation_exists = git_hygiene_remediation_summary.exists()
    remediation_ok = bool(git_hygiene_remediation.get("ok", False))
    remediation_remote_stale_prefix = int(
        remediation_summary.get("remote_stale_prefix_count", remediation_summary.get("remote_candidate_count", 0)) or 0
    )
    remediation_local_stale_prefix = int(
        remediation_summary.get("local_stale_prefix_count", remediation_summary.get("local_candidate_count", 0)) or 0
    )
    remediation_remote_candidates = int(remediation_summary.get("remote_candidate_count", 0) or 0)
    remediation_local_candidates = int(remediation_summary.get("local_candidate_count", 0) or 0)
    remediation_remote_blocked_open_pr = int(remediation_summary.get("remote_blocked_open_pr_count", 0) or 0)
    remediation_remote_blocked_unmerged = int(remediation_summary.get("remote_blocked_unmerged_count", 0) or 0)
    remediation_local_blocked_unmerged = int(remediation_summary.get("local_blocked_unmerged_count", 0) or 0)
    remediation_local_blocked_worktree = int(remediation_summary.get("local_blocked_worktree_count", 0) or 0)
    remediation_worktree_prune_removed = int(remediation_summary.get("worktree_prune_removed_count", 0) or 0)
    remediation_worktree_remove_attempted = int(remediation_summary.get("worktree_remove_attempted_count", 0) or 0)
    remediation_worktree_removed = int(remediation_summary.get("worktree_removed_count", 0) or 0)
    remediation_worktree_remove_failed = int(remediation_summary.get("worktree_remove_failed_count", 0) or 0)
    remediation_remote_deleted = int(remediation_summary.get("remote_deleted_count", 0) or 0)
    remediation_local_deleted = int(remediation_summary.get("local_deleted_count", 0) or 0)
    if not remediation_exists:
        tasks.append(
            _task(
                task_id="T1-GIT-HYGIENE-REMEDIATION-INSTRUMENTATION",
                title="Enable deterministic git hygiene remediation artifact",
                priority="P1",
                rationale="git_hygiene_remediation artifact missing from current cycle.",
                acceptance=[
                    "git_hygiene_remediation.json exists under artifacts/autonomy.",
                    "Artifact reports local/remote candidate and deleted branch counts.",
                    "Cycle runs remediation step before git hygiene policy check.",
                ],
            )
        )
    elif not remediation_ok:
        tasks.append(
            _task(
                task_id="T1-GIT-HYGIENE-REMEDIATION-HEALTH",
                title="Restore git hygiene remediation health",
                priority="P1",
                rationale="git_hygiene_remediation reported errors in current cycle.",
                acceptance=[
                    "git_hygiene_remediation summary.error_count equals 0.",
                    "Remediation run succeeds for both orxaq-ops and orxaq repos.",
                    "No open PR head branches are deleted.",
                ],
            )
        )
    elif remediation_remote_candidates > 0 or remediation_local_candidates > 0:
        tasks.append(
            _task(
                task_id="T1-GIT-HYGIENE-REMEDIATION-BACKLOG",
                title="Drain remaining stale branch remediation backlog",
                priority="P1",
                rationale=(
                    "Git hygiene remediation still sees pending branch candidates "
                    f"(remote={remediation_remote_candidates}, local={remediation_local_candidates}, "
                    f"deleted_remote={remediation_remote_deleted}, deleted_local={remediation_local_deleted})."
                ),
                acceptance=[
                    "Candidate counts trend down cycle-over-cycle with deterministic evidence.",
                    "Deleted branch counts are recorded with transparent audit trails.",
                    "Only merged or closed-PR non-open branches are remediated.",
                ],
            )
        )
    if remediation_local_blocked_worktree > 0:
        tasks.append(
            _task(
                task_id="T1-GIT-HYGIENE-WORKTREE-RECONCILE",
                title="Reconcile worktree-bound branch locks",
                priority="P1",
                rationale=(
                    "Git hygiene remediation could not delete some local branches because they are "
                    f"still bound to worktrees (blocked_worktree={remediation_local_blocked_worktree}, "
                    f"worktree_prune_removed={remediation_worktree_prune_removed}, "
                    f"worktree_remove_attempted={remediation_worktree_remove_attempted}, "
                    f"worktree_removed={remediation_worktree_removed}, "
                    f"worktree_remove_failed={remediation_worktree_remove_failed})."
                ),
                acceptance=[
                    "Stale worktree metadata is pruned before remediation runs.",
                    "Clean stale worktrees are removed and branch deletion retried automatically.",
                    "Worktree-bound deletion failures are reduced with deterministic evidence.",
                    "Active lane worktrees remain intact and documented.",
                ],
            )
        )
    if (
        remediation_remote_blocked_open_pr > 0
        or remediation_remote_blocked_unmerged > 0
        or remediation_local_blocked_unmerged > 0
    ):
        tasks.append(
            _task(
                task_id="T1-GIT-HYGIENE-BRANCH-GOVERNANCE",
                title="Triage non-actionable stale branch debt",
                priority="P2",
                rationale=(
                    "Stale branch inventory includes non-actionable branches pending PR/merge governance "
                    f"(remote_open_pr={remediation_remote_blocked_open_pr}, "
                    f"remote_unmerged={remediation_remote_blocked_unmerged}, "
                    f"local_unmerged={remediation_local_blocked_unmerged}, "
                    f"stale_remote_prefix={remediation_remote_stale_prefix}, "
                    f"stale_local_prefix={remediation_local_stale_prefix})."
                ),
                acceptance=[
                    "Open-PR stale heads are tracked with owner and disposition.",
                    "Unmerged stale branches are closed, merged, or explicitly exempted.",
                    "Stale prefix inventory trends down over successive cycles.",
                ],
            )
        )

    backend_upgrade_ok = bool(backend_upgrade_policy.get("ok", True))
    backend_upgrade_summary_payload = (
        backend_upgrade_policy.get("summary", {})
        if isinstance(backend_upgrade_policy.get("summary"), dict)
        else {}
    )
    backend_upgrade_violations = int(backend_upgrade_summary_payload.get("violation_count", 0) or 0)
    backend_upgrade_release_phase = str(backend_upgrade_summary_payload.get("release_phase", "foundation")).strip().lower() or "foundation"
    backend_upgrade_dependency_checks = int(backend_upgrade_summary_payload.get("dependency_checks_passed", 0) or 0)
    backend_upgrade_activation_checks = int(backend_upgrade_summary_payload.get("activation_task_checks_passed", 0) or 0)
    if not backend_upgrade_ok or backend_upgrade_violations > 0:
        tasks.append(
            _task(
                task_id="T1-BACKEND-UPGRADE-POLICY-ENFORCEMENT",
                title="Enforce backend portfolio and upgrade lifecycle policy gates",
                priority="P0",
                rationale=(
                    "backend/upgrade policy unhealthy "
                    f"(ok={backend_upgrade_ok}, violations={backend_upgrade_violations}, "
                    f"release_phase={backend_upgrade_release_phase}, "
                    f"dependency_checks_passed={backend_upgrade_dependency_checks}, "
                    f"activation_checks_passed={backend_upgrade_activation_checks})."
                ),
                acceptance=[
                    "backend_upgrade_policy_health report shows ok=true and violation_count=0.",
                    "Spark, Dask, and Spark+JIT hybrid paths remain policy-covered in user mode.",
                    "Upgrade lifecycle dependencies enforce routing + A/B prerequisites before rollout automation.",
                    "Cloud validation remains explicit-trigger only and auditable.",
                ],
            )
        )

    api_interop_ok = bool(api_interop_policy.get("ok", True))
    api_interop_summary_payload = (
        api_interop_policy.get("summary", {})
        if isinstance(api_interop_policy.get("summary"), dict)
        else {}
    )
    api_interop_violations = int(api_interop_summary_payload.get("violation_count", 0) or 0)
    api_interop_release_phase = str(api_interop_summary_payload.get("release_phase", "foundation")).strip().lower() or "foundation"
    api_interop_dependency_checks = int(api_interop_summary_payload.get("dependency_checks_passed", 0) or 0)
    api_interop_activation_checks = int(api_interop_summary_payload.get("activation_prereq_checks_passed", 0) or 0)
    if not api_interop_ok or api_interop_violations > 0:
        tasks.append(
            _task(
                task_id="T1-API-INTEROP-POLICY-ENFORCEMENT",
                title="Enforce external API interoperability policy gates",
                priority="P0",
                rationale=(
                    "api interoperability policy unhealthy "
                    f"(ok={api_interop_ok}, violations={api_interop_violations}, "
                    f"release_phase={api_interop_release_phase}, "
                    f"dependency_checks_passed={api_interop_dependency_checks}, "
                    f"activation_checks_passed={api_interop_activation_checks})."
                ),
                acceptance=[
                    "api_interop_policy_health report shows ok=true and violation_count=0.",
                    "REST and MCP contract governance remains deterministic and versioned.",
                    "Common standards gates (OpenAPI/JSON Schema/AsyncAPI/CloudEvents) remain policy-covered.",
                    "Security, compatibility, and conformance release gates block unsafe promotions.",
                ],
            )
        )

    backlog_control_ok = bool(backlog_control.get("ok", True))
    backlog_control_payload = backlog_control.get("summary", {}) if isinstance(backlog_control.get("summary"), dict) else {}
    backlog_ready_after = int(backlog_control_payload.get("ready_after", 0) or 0)
    backlog_ready_min = int(backlog_control_payload.get("ready_min", 0) or 0)
    backlog_ready_target = int(backlog_control_payload.get("ready_target", 0) or 0)
    backlog_ready_max = int(backlog_control_payload.get("ready_max", 0) or 0)
    backlog_action_count = int(backlog_control_payload.get("action_count", 0) or 0)
    backlog_updated = bool(backlog_control.get("backlog_updated", False))
    if not backlog_control_ok or backlog_ready_after < backlog_ready_min or backlog_ready_after > backlog_ready_max:
        tasks.append(
            _task(
                task_id="T1-BACKLOG-CONTROL-HEALTH",
                title="Restore deterministic backlog control window",
                priority="P1",
                rationale=(
                    "deterministic backlog controller unhealthy "
                    f"(ok={backlog_control_ok}, ready_after={backlog_ready_after}, "
                    f"window={backlog_ready_min}-{backlog_ready_max}, action_count={backlog_action_count})."
                ),
                acceptance=[
                    "deterministic_backlog_health report shows ok=true.",
                    "ready_after remains within configured min/target/max bounds.",
                    "Backlog amendments are deterministic and marker-driven (no AI routing decisions).",
                ],
            )
        )

    pr_remediation_exists = pr_approval_remediation_summary.exists()
    pr_remediation_ok = bool(pr_approval_remediation.get("ok", False))
    pr_remediation_payload = (
        pr_approval_remediation.get("summary", {})
        if isinstance(pr_approval_remediation.get("summary"), dict)
        else {}
    )
    pr_remediation_open = int(pr_remediation_payload.get("open_prs_seen", 0) or 0)
    pr_remediation_approved = int(pr_remediation_payload.get("approved_count", 0) or 0)
    pr_remediation_self_blocked = int(pr_remediation_payload.get("self_blocked_count", 0) or 0)
    pr_remediation_other_blocked = int(pr_remediation_payload.get("other_blocked_count", 0) or 0)
    if not pr_remediation_exists:
        tasks.append(
            _task(
                task_id="T1-PR-APPROVAL-INSTRUMENTATION",
                title="Enable PR approval remediation instrumentation",
                priority="P1",
                rationale="PR approval remediation artifact is missing for this cycle.",
                acceptance=[
                    "pr_approval_remediation.json exists under artifacts/autonomy.",
                    "Cycle includes open_prs_seen, approved_count, self_blocked_count, and other_blocked_count.",
                    "Artifact refreshes deterministically each cycle.",
                ],
            )
        )
    if pr_remediation_other_blocked > 0:
        tasks.append(
            _task(
                task_id="T1-PR-APPROVAL-REMEDIATION",
                title="Clear non-self PR approval blockers",
                priority="P0",
                rationale=(
                    "PR remediation reports unresolved non-self blockers "
                    f"(other_blocked={pr_remediation_other_blocked}, open_prs={pr_remediation_open})."
                ),
                acceptance=[
                    "pr_approval_remediation summary.other_blocked_count equals 0.",
                    "Eligible non-self-authored PRs are approved or explicitly documented as blocked.",
                    "Any remaining blocker has actionable diagnostic detail.",
                ],
            )
        )
    if pr_remediation_self_blocked > 0:
        tasks.append(
            _task(
                task_id="T1-PR-REVIEWER-CAPACITY",
                title="Provision independent reviewer capacity",
                priority="P1",
                rationale=(
                    "Self-authored PR approvals are blocked by platform policy "
                    f"(self_blocked={pr_remediation_self_blocked}, approved={pr_remediation_approved})."
                ),
                acceptance=[
                    "At least one independent reviewer identity is available for each repo.",
                    "Review routing (teams/CODEOWNERS) is configured deterministically.",
                    "PR remediation rerun shows reduced self_blocked_count over baseline.",
                ],
            )
        )

    # Always keep a deterministic baseline task for gate trend reporting.
    tasks.append(
        _task(
            task_id="T1-GATE-TREND-REPORT",
            title="Publish daily swarm gate trend report",
            priority="P2",
            rationale="Maintain transparency and regression detection for swarm health gates.",
            acceptance=[
                "Report includes strict vs operational health deltas.",
                "Report includes endpoint connectivity and backlog metrics.",
                "Report artifact is generated under artifacts/autonomy.",
            ],
        )
    )

    # Prioritize deterministically by priority band then id.
    prio_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    tasks.sort(key=lambda row: (prio_order.get(str(row.get("priority_band", "P3")), 3), str(row.get("id", ""))))
    if max_items > 0:
        tasks = tasks[:max_items]

    return {
        "schema_version": "ready-queue-week.v1",
        "generated_at_utc": _utc_now_iso(),
        "root_dir": str(root),
        "summary": {
            "task_count": len(tasks),
            "endpoint_unhealthy": endpoint_unhealthy,
            "todo_health_refresh_attempted": bool(todo_refresh.get("attempted", False)),
            "todo_health_refresh_ok": bool(todo_refresh.get("ok", False)),
            "todo_health_refresh_reason": str(todo_refresh.get("reason", "")).strip(),
            "provider_health_ok": provider_health_ok,
            "provider_telemetry_mode": provider_telemetry_mode,
            "provider_health_effective_ok": provider_health_effective_ok,
            "provider_cost_health_ok": provider_health_ok,
            "swarm_budget_enabled": budget_enabled,
            "swarm_budget_state": budget_state,
            "swarm_budget_daily_spend_usd": budget_daily_spend,
            "swarm_budget_daily_cap_usd": budget_daily_cap,
            "swarm_budget_daily_remaining_usd": budget_remaining,
            "stale_file_count": stale_files,
            "unassigned_active_task_total": unassigned_active,
            "t1_policy_ok": t1_policy_ok,
            "t1_policy_violation_count": t1_violations,
            "t1_policy_observability_ok": t1_observability_ok,
            "privilege_policy_ok": privilege_ok,
            "privilege_policy_violation_count": privilege_violations,
            "privilege_policy_scanned_events": privilege_scanned_events,
            "git_delivery_policy_ok": git_delivery_ok,
            "git_delivery_policy_violation_count": git_delivery_violations,
            "git_delivery_policy_branch": git_delivery_branch,
            "git_delivery_policy_effective_changed_lines": git_delivery_effective_changed_lines,
            "git_delivery_policy_max_changed_lines": git_delivery_max_changed_lines,
            "git_delivery_policy_pr_found": git_delivery_pr_found,
            "git_delivery_policy_pr_approvals": git_delivery_pr_approvals,
            "git_hygiene_ok": git_hygiene_ok,
            "git_hygiene_violation_count": git_hygiene_violations,
            "git_hygiene_total_branch_count": git_hygiene_total,
            "git_hygiene_max_total_branches": git_hygiene_max_total,
            "git_hygiene_stale_local_branch_count": git_hygiene_stale_local,
            "git_hygiene_remediation_exists": remediation_exists,
            "git_hygiene_remediation_ok": remediation_ok,
            "git_hygiene_remediation_remote_stale_prefix_count": remediation_remote_stale_prefix,
            "git_hygiene_remediation_local_stale_prefix_count": remediation_local_stale_prefix,
            "git_hygiene_remediation_remote_candidates": remediation_remote_candidates,
            "git_hygiene_remediation_local_candidates": remediation_local_candidates,
            "git_hygiene_remediation_remote_blocked_open_pr_count": remediation_remote_blocked_open_pr,
            "git_hygiene_remediation_remote_blocked_unmerged_count": remediation_remote_blocked_unmerged,
            "git_hygiene_remediation_local_blocked_unmerged_count": remediation_local_blocked_unmerged,
            "git_hygiene_remediation_local_blocked_worktree_count": remediation_local_blocked_worktree,
            "git_hygiene_remediation_worktree_prune_removed_count": remediation_worktree_prune_removed,
            "git_hygiene_remediation_worktree_remove_attempted_count": remediation_worktree_remove_attempted,
            "git_hygiene_remediation_worktree_removed_count": remediation_worktree_removed,
            "git_hygiene_remediation_worktree_remove_failed_count": remediation_worktree_remove_failed,
            "git_hygiene_remediation_remote_deleted": remediation_remote_deleted,
            "git_hygiene_remediation_local_deleted": remediation_local_deleted,
            "backend_upgrade_policy_ok": backend_upgrade_ok,
            "backend_upgrade_policy_violation_count": backend_upgrade_violations,
            "backend_upgrade_release_phase": backend_upgrade_release_phase,
            "backend_upgrade_dependency_checks_passed": backend_upgrade_dependency_checks,
            "backend_upgrade_activation_checks_passed": backend_upgrade_activation_checks,
            "api_interop_policy_ok": api_interop_ok,
            "api_interop_policy_violation_count": api_interop_violations,
            "api_interop_release_phase": api_interop_release_phase,
            "api_interop_dependency_checks_passed": api_interop_dependency_checks,
            "api_interop_activation_checks_passed": api_interop_activation_checks,
            "deterministic_backlog_ok": backlog_control_ok,
            "deterministic_backlog_ready_after": backlog_ready_after,
            "deterministic_backlog_ready_min": backlog_ready_min,
            "deterministic_backlog_ready_target": backlog_ready_target,
            "deterministic_backlog_ready_max": backlog_ready_max,
            "deterministic_backlog_action_count": backlog_action_count,
            "deterministic_backlog_updated": backlog_updated,
            "pr_approval_remediation_exists": pr_remediation_exists,
            "pr_approval_remediation_ok": pr_remediation_ok,
            "pr_approval_open_prs_seen": pr_remediation_open,
            "pr_approval_approved_count": pr_remediation_approved,
            "pr_approval_self_blocked_count": pr_remediation_self_blocked,
            "pr_approval_other_blocked_count": pr_remediation_other_blocked,
        },
        "tasks": tasks,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic ready queue for T1 swarm workers.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--output", default="artifacts/autonomy/ready_queue_week.json", help="Output JSON path.")
    parser.add_argument("--max-items", type=int, default=21, help="Maximum tasks in ready queue.")
    parser.add_argument(
        "--refresh-todo-summary",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Refresh swarm_todo_health current snapshot before queue generation.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = Path(args.root).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    report = build_queue(
        root,
        max_items=max(1, int(args.max_items)),
        refresh_todo_summary=bool(args.refresh_todo_summary),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "task_count": report["summary"]["task_count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
