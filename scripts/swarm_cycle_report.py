#!/usr/bin/env python3
"""Generate deterministic swarm cycle report and blocked-cycle escalation items."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _age_sec(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return -1
    try:
        now = datetime.now(UTC).timestamp()
        return max(0, int(now - path.stat().st_mtime))
    except OSError:
        return -1


def _pid_status(pid_file: Path) -> tuple[bool, int]:
    if not pid_file.exists() or not pid_file.is_file():
        return (False, 0)
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip() or "0")
    except Exception:  # noqa: BLE001
        return (False, 0)
    if pid <= 0:
        return (False, 0)
    try:
        os.kill(pid, 0)
        return (True, pid)
    except OSError:
        return (False, pid)


def _quality_gate_state(health: dict[str, Any], gate_name: str) -> bool | None:
    checks = health.get("checks", {}) if isinstance(health.get("checks"), dict) else {}
    gates = checks.get("quality_gates", []) if isinstance(checks.get("quality_gates"), list) else []
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        if str(gate.get("name", "")).strip() == gate_name:
            return _as_bool(gate.get("ok", False), False)
    return None


def _dashboard_http_live(dashboard_meta: dict[str, Any]) -> tuple[bool, str]:
    candidates: list[str] = []
    meta_url = str(dashboard_meta.get("url", "")).strip()
    if meta_url:
        candidates.append(meta_url.rstrip("/"))
    host = str(dashboard_meta.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    port = _as_int(dashboard_meta.get("port", 0), 0)
    if port > 0:
        candidates.append(f"http://{host}:{port}")
    candidates.extend([f"http://{host}:8876", f"http://{host}:8765"])

    dedup: list[str] = []
    seen: set[str] = set()
    for base in candidates:
        if not base or base in seen:
            continue
        dedup.append(base)
        seen.add(base)

    probe_paths = ("/api/version", "/api/health", "/")
    for base in dedup:
        for path in probe_paths:
            url = f"{base}{path}"
            try:
                with urlopen(url, timeout=1.5) as response:  # noqa: S310
                    status = _as_int(getattr(response, "status", 200), 200)
                if 200 <= status < 500:
                    return (True, base)
            except HTTPError as err:
                if 200 <= _as_int(getattr(err, "code", 500), 500) < 500:
                    return (True, base)
            except (URLError, TimeoutError, OSError):
                continue
    return (False, "")


def build_report(root: Path) -> dict[str, Any]:
    ops_root = root.resolve()
    product_root = ops_root.parent / "orxaq"
    snapshot_root = ops_root / "artifacts" / "autonomy" / "health_snapshot"
    strict_snapshot = snapshot_root / "strict.json"
    operational_snapshot = snapshot_root / "operational.json"
    strict_health_path = strict_snapshot if strict_snapshot.exists() else (product_root / "artifacts" / "health.json")
    operational_health_path = (
        operational_snapshot if operational_snapshot.exists() else (product_root / "artifacts" / "health_operational.json")
    )

    paths = {
        "strict_health": strict_health_path,
        "operational_health": operational_health_path,
        "connectivity": ops_root / "artifacts" / "model_connectivity.json",
        "todo_current": ops_root / "artifacts" / "autonomy" / "swarm_todo_health" / "current_latest.json",
        "t1_model_policy": ops_root / "artifacts" / "autonomy" / "t1_basic_model_policy.json",
        "privilege_policy": ops_root / "artifacts" / "autonomy" / "privilege_policy_health.json",
        "git_delivery_policy": ops_root / "artifacts" / "autonomy" / "git_delivery_policy_health.json",
        "git_hygiene": ops_root / "artifacts" / "autonomy" / "git_hygiene_health.json",
        "git_hygiene_remediation": ops_root / "artifacts" / "autonomy" / "git_hygiene_remediation.json",
        "backend_upgrade_policy": ops_root / "artifacts" / "autonomy" / "backend_upgrade_policy_health.json",
        "api_interop_policy": ops_root / "artifacts" / "autonomy" / "api_interop_policy_health.json",
        "deterministic_backlog": ops_root / "artifacts" / "autonomy" / "deterministic_backlog_health.json",
        "pr_approval_remediation": ops_root / "artifacts" / "autonomy" / "pr_approval_remediation.json",
        "ready_queue": ops_root / "artifacts" / "autonomy" / "ready_queue_week.json",
        "provider_summary": ops_root / "artifacts" / "autonomy" / "provider_costs" / "summary.json",
        "provider_cost_health": ops_root / "artifacts" / "autonomy" / "provider_cost_health.json",
        "pr_tier_policy": ops_root / "artifacts" / "autonomy" / "pr_tier_policy_health.json",
        "dashboard_meta": ops_root / "artifacts" / "autonomy" / "dashboard.json",
        "conversations": ops_root / "artifacts" / "autonomy" / "conversations.ndjson",
        "events": ops_root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson",
        "routing_policy": ops_root / "config" / "routellm_policy.local-workhorse.json",
        "lanes_config": ops_root / "config" / "lanes.json",
        "heartbeat": ops_root / "artifacts" / "autonomy" / "heartbeat.json",
        "supervisor_pid": ops_root / "artifacts" / "autonomy" / "supervisor.pid",
        "runner_pid": ops_root / "artifacts" / "autonomy" / "runner.pid",
        "dashboard_pid": ops_root / "artifacts" / "autonomy" / "dashboard.pid",
        "current_todo_pid": ops_root / "artifacts" / "autonomy" / "swarm_todo_health" / "current_health.pid",
    }

    strict_health = _load_json(paths["strict_health"])
    operational_health = _load_json(paths["operational_health"])
    connectivity = _load_json(paths["connectivity"])
    todo_current = _load_json(paths["todo_current"])
    t1_model_policy = _load_json(paths["t1_model_policy"])
    privilege_policy = _load_json(paths["privilege_policy"])
    git_delivery_policy = _load_json(paths["git_delivery_policy"])
    git_hygiene = _load_json(paths["git_hygiene"])
    git_hygiene_remediation = _load_json(paths["git_hygiene_remediation"])
    backend_upgrade_policy = _load_json(paths["backend_upgrade_policy"])
    api_interop_policy = _load_json(paths["api_interop_policy"])
    deterministic_backlog = _load_json(paths["deterministic_backlog"])
    pr_approval_remediation = _load_json(paths["pr_approval_remediation"])
    ready_queue = _load_json(paths["ready_queue"])
    provider_summary = _load_json(paths["provider_summary"])
    provider_cost_health = _load_json(paths["provider_cost_health"])
    pr_tier_policy = _load_json(paths["pr_tier_policy"])
    dashboard_meta = _load_json(paths["dashboard_meta"])
    routing_policy = _load_json(paths["routing_policy"])
    lanes_config = _load_json(paths["lanes_config"])
    heartbeat = _load_json(paths["heartbeat"])

    supervisor_running, supervisor_pid = _pid_status(paths["supervisor_pid"])
    runner_running, runner_pid = _pid_status(paths["runner_pid"])
    dashboard_running_pid, dashboard_pid = _pid_status(paths["dashboard_pid"])
    current_todo_running, current_todo_pid = _pid_status(paths["current_todo_pid"])
    heartbeat_phase = str(heartbeat.get("phase", "")).strip().lower()
    heartbeat_age_sec = _age_sec(paths["heartbeat"])
    runner_effective_live = runner_running or (
        heartbeat_phase in {"completed", "idle_all_done", "task_completed", "task_queue_wait"}
        and heartbeat_age_sec >= 0
        and heartbeat_age_sec <= 900
    )

    strict_pass = _as_bool(strict_health.get("pass_gate", False), False)
    strict_score = _as_int(strict_health.get("score", 0), 0)
    strict_threshold = _as_int(strict_health.get("threshold", 85), 85)
    operational_pass = _as_bool(operational_health.get("pass_gate", False), False)
    operational_score = _as_int(operational_health.get("score", 0), 0)

    endpoint_unhealthy = _as_int(connectivity.get("endpoint_unhealthy", 0), 0)
    endpoint_required_total = _as_int(connectivity.get("endpoint_required_total", 0), 0)
    optional_endpoint_unhealthy = _as_int(connectivity.get("optional_endpoint_unhealthy", 0), 0)

    distributed = todo_current.get("distributed_todo", {}) if isinstance(todo_current.get("distributed_todo"), dict) else {}
    todo_stale = _as_int(distributed.get("stale_file_count", 0), 0)
    todo_unassigned = _as_int(distributed.get("unassigned_active_task_total", 0), 0)
    todo_warning_count = len(todo_current.get("warnings", []) if isinstance(todo_current.get("warnings"), list) else [])
    todo_report_age_sec = _age_sec(paths["todo_current"])

    ready_summary = ready_queue.get("summary", {}) if isinstance(ready_queue.get("summary"), dict) else {}
    ready_task_count = _as_int(ready_summary.get("task_count", 0), 0)

    provider_ok = _as_bool(provider_summary.get("ok", False), False)
    provider_health_ok = _as_bool(provider_cost_health.get("ok", provider_ok), provider_ok)
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
    budget_payload = (
        provider_cost_health.get("budget", {})
        if isinstance(provider_cost_health.get("budget"), dict)
        else {}
    )
    budget_enabled = _as_bool(budget_payload.get("enabled", False), False)
    budget_state = str(budget_payload.get("state", "disabled")).strip().lower() or "disabled"
    budget_hard_stop = _as_bool(budget_payload.get("hard_stop", False), False)
    budget_daily_spend_usd = float(budget_payload.get("daily_spend_usd", 0.0) or 0.0)
    budget_daily_cap_usd = float(budget_payload.get("daily_budget_usd", 0.0) or 0.0)
    budget_daily_remaining_usd = float(budget_payload.get("daily_remaining_usd", 0.0) or 0.0)

    lane_items_payload = lanes_config.get("lanes", []) if isinstance(lanes_config, dict) else lanes_config
    lanes_total_count = 0
    lanes_enabled_count = 0
    lanes_enabled_runnable_count = 0
    lanes_owner_counts: dict[str, int] = {}
    lane_command_by_owner = {
        "codex": str(os.getenv("ORXAQ_AUTONOMY_CODEX_CMD", "codex")).strip() or "codex",
        "gemini": str(os.getenv("ORXAQ_AUTONOMY_GEMINI_CMD", "gemini")).strip() or "gemini",
        "claude": str(os.getenv("ORXAQ_AUTONOMY_CLAUDE_CMD", "claude")).strip() or "claude",
    }
    lane_command_missing_owners: list[str] = []
    lane_command_missing_values: list[str] = []
    if isinstance(lane_items_payload, list):
        for item in lane_items_payload:
            if not isinstance(item, dict):
                continue
            lanes_total_count += 1
            owner = str(item.get("owner", "")).strip().lower() or "unknown"
            lanes_owner_counts[owner] = lanes_owner_counts.get(owner, 0) + 1
            enabled = _as_bool(item.get("enabled", True), True)
            if not enabled:
                continue
            lanes_enabled_count += 1
            command = lane_command_by_owner.get(owner, owner).strip()
            if command:
                lanes_enabled_runnable_count += 1
            else:
                lane_command_missing_owners.append(owner)
                lane_command_missing_values.append(command)
    lane_command_missing_owners = sorted(set(lane_command_missing_owners))
    lane_command_missing_values = sorted(set(lane_command_missing_values))

    pr_tier_summary = pr_tier_policy.get("summary", {}) if isinstance(pr_tier_policy.get("summary"), dict) else {}
    pr_tier_policy_meta = pr_tier_policy.get("policy", {}) if isinstance(pr_tier_policy.get("policy"), dict) else {}
    pr_tier_ok = _as_bool(pr_tier_policy.get("ok", False), False)
    pr_tier_reviewed_prs = _as_int(pr_tier_summary.get("reviewed_prs", 0), 0)
    pr_tier_ratio_base_prs = _as_int(pr_tier_summary.get("ratio_base_prs", 0), 0)
    pr_tier_t1_count = _as_int(pr_tier_summary.get("t1_count", 0), 0)
    pr_tier_escalated_count = _as_int(pr_tier_summary.get("escalated_count", 0), 0)
    pr_tier_unlabeled_count = _as_int(pr_tier_summary.get("unlabeled_count", 0), 0)
    pr_tier_conflict_count = _as_int(pr_tier_summary.get("conflict_count", 0), 0)
    pr_tier_violation_count = _as_int(pr_tier_summary.get("violation_count", 0), 0)
    pr_tier_t1_ratio = _as_float(pr_tier_summary.get("t1_ratio", 0.0), 0.0)
    pr_tier_escalated_ratio = _as_float(pr_tier_summary.get("escalated_ratio", 0.0), 0.0)
    pr_tier_unlabeled_ratio = _as_float(pr_tier_summary.get("unlabeled_ratio", 0.0), 0.0)
    pr_tier_min_t1_ratio = _as_float(pr_tier_policy_meta.get("min_t1_ratio", 0.0), 0.0)

    t1_policy_summary = t1_model_policy.get("summary", {}) if isinstance(t1_model_policy.get("summary"), dict) else {}
    t1_policy_observability = (
        t1_model_policy.get("observability", {}) if isinstance(t1_model_policy.get("observability"), dict) else {}
    )
    t1_policy_ok = _as_bool(t1_model_policy.get("ok", False), False)
    t1_policy_violations = _as_int(t1_policy_summary.get("violation_count", 0), 0)
    t1_policy_basic_tasks = _as_int(t1_policy_summary.get("basic_tasks", 0), 0)
    t1_policy_scanned = _as_int(t1_policy_summary.get("scanned_metrics", 0), 0)
    t1_policy_observability_ok = _as_bool(t1_policy_observability.get("ok", False), False)
    t1_policy_latest_metric_age_minutes = _as_int(t1_policy_observability.get("latest_metric_age_minutes", -1), -1)
    t1_policy_parse_skip_ratio = t1_policy_observability.get("parse_skip_ratio", 0.0)

    privilege_summary = privilege_policy.get("summary", {}) if isinstance(privilege_policy.get("summary"), dict) else {}
    privilege_ok = _as_bool(privilege_policy.get("ok", False), False)
    privilege_violations = _as_int(privilege_summary.get("violation_count", 0), 0)
    privilege_latest_event_age_minutes = _as_int(privilege_summary.get("latest_event_age_minutes", -1), -1)
    privilege_breakglass_events = _as_int(privilege_summary.get("breakglass_events", 0), 0)

    git_delivery_summary = (
        git_delivery_policy.get("summary", {})
        if isinstance(git_delivery_policy.get("summary"), dict)
        else {}
    )
    git_delivery_ok = _as_bool(git_delivery_policy.get("ok", False), False)
    git_delivery_violations = _as_int(git_delivery_summary.get("violation_count", 0), 0)
    git_delivery_branch = str(git_delivery_summary.get("branch", "")).strip()
    git_delivery_ticket_branch_match = _as_bool(git_delivery_summary.get("ticket_branch_match", False), False)
    git_delivery_effective_changed_lines = _as_int(git_delivery_summary.get("effective_changed_lines", 0), 0)
    git_delivery_max_changed_lines = _as_int(git_delivery_summary.get("max_changed_lines", 0), 0)
    git_delivery_pr_found = _as_bool(git_delivery_summary.get("pr_found", False), False)
    git_delivery_pr_approvals = _as_int(git_delivery_summary.get("pr_approvals", 0), 0)

    git_hygiene_summary = (
        git_hygiene.get("summary", {})
        if isinstance(git_hygiene.get("summary"), dict)
        else {}
    )
    git_hygiene_ok = _as_bool(git_hygiene.get("ok", False), False)
    git_hygiene_violations = _as_int(git_hygiene_summary.get("violation_count", 0), 0)
    git_hygiene_local_count = _as_int(git_hygiene_summary.get("local_branch_count", 0), 0)
    git_hygiene_remote_count = _as_int(git_hygiene_summary.get("remote_branch_count", 0), 0)
    git_hygiene_total_count = _as_int(git_hygiene_summary.get("total_branch_count", 0), 0)
    git_hygiene_stale_local_count = _as_int(git_hygiene_summary.get("stale_local_branch_count", 0), 0)
    git_hygiene_max_total = _as_int(git_hygiene_summary.get("max_total_branches", 0), 0)
    git_hygiene_remediation_summary = (
        git_hygiene_remediation.get("summary", {})
        if isinstance(git_hygiene_remediation.get("summary"), dict)
        else {}
    )
    git_hygiene_remediation_artifact_exists = paths["git_hygiene_remediation"].exists()
    git_hygiene_remediation_ok = _as_bool(git_hygiene_remediation.get("ok", False), False)
    git_hygiene_remediation_error_count = _as_int(git_hygiene_remediation_summary.get("error_count", 0), 0)
    git_hygiene_remediation_remote_stale_prefix_count = _as_int(
        git_hygiene_remediation_summary.get("remote_stale_prefix_count", 0), 0
    )
    git_hygiene_remediation_local_stale_prefix_count = _as_int(
        git_hygiene_remediation_summary.get("local_stale_prefix_count", 0), 0
    )
    git_hygiene_remediation_remote_candidates = _as_int(
        git_hygiene_remediation_summary.get("remote_candidate_count", 0), 0
    )
    git_hygiene_remediation_local_candidates = _as_int(
        git_hygiene_remediation_summary.get("local_candidate_count", 0), 0
    )
    git_hygiene_remediation_remote_blocked_open_pr_count = _as_int(
        git_hygiene_remediation_summary.get("remote_blocked_open_pr_count", 0), 0
    )
    git_hygiene_remediation_remote_blocked_unmerged_count = _as_int(
        git_hygiene_remediation_summary.get("remote_blocked_unmerged_count", 0), 0
    )
    git_hygiene_remediation_local_blocked_unmerged_count = _as_int(
        git_hygiene_remediation_summary.get("local_blocked_unmerged_count", 0), 0
    )
    git_hygiene_remediation_local_blocked_worktree_count = _as_int(
        git_hygiene_remediation_summary.get("local_blocked_worktree_count", 0), 0
    )
    git_hygiene_remediation_local_blocked_worktree_dirty_count = _as_int(
        git_hygiene_remediation_summary.get(
            "local_blocked_worktree_dirty_count",
            git_hygiene_remediation_summary.get("local_blocked_worktree_count", 0),
        ),
        0,
    )
    git_hygiene_remediation_dirty_worktree_blocker_count = _as_int(
        git_hygiene_remediation_summary.get("dirty_worktree_blocker_count", 0), 0
    )
    git_hygiene_remediation_dirty_blockers_payload = (
        git_hygiene_remediation.get("dirty_worktree_blockers", [])
        if isinstance(git_hygiene_remediation.get("dirty_worktree_blockers"), list)
        else []
    )
    git_hygiene_remediation_dirty_blocker_branches = [
        str(row.get("branch", "")).strip()
        for row in git_hygiene_remediation_dirty_blockers_payload
        if isinstance(row, dict) and str(row.get("branch", "")).strip()
    ][:5]
    git_hygiene_remediation_local_force_deleted_count = _as_int(
        git_hygiene_remediation_summary.get("local_force_deleted_count", 0), 0
    )
    git_hygiene_remediation_worktree_prune_removed_count = _as_int(
        git_hygiene_remediation_summary.get("worktree_prune_removed_count", 0), 0
    )
    git_hygiene_remediation_worktree_remove_attempted_count = _as_int(
        git_hygiene_remediation_summary.get("worktree_remove_attempted_count", 0), 0
    )
    git_hygiene_remediation_worktree_removed_count = _as_int(
        git_hygiene_remediation_summary.get("worktree_removed_count", 0), 0
    )
    git_hygiene_remediation_worktree_remove_failed_count = _as_int(
        git_hygiene_remediation_summary.get("worktree_remove_failed_count", 0), 0
    )
    git_hygiene_remediation_remote_deleted = _as_int(
        git_hygiene_remediation_summary.get("remote_deleted_count", 0), 0
    )
    git_hygiene_remediation_local_deleted = _as_int(
        git_hygiene_remediation_summary.get("local_deleted_count", 0), 0
    )

    backend_upgrade_summary = (
        backend_upgrade_policy.get("summary", {})
        if isinstance(backend_upgrade_policy.get("summary"), dict)
        else {}
    )
    backend_upgrade_ok = _as_bool(backend_upgrade_policy.get("ok", False), False)
    backend_upgrade_violations = _as_int(backend_upgrade_summary.get("violation_count", 0), 0)
    backend_upgrade_release_phase = str(backend_upgrade_summary.get("release_phase", "foundation")).strip().lower() or "foundation"
    backend_upgrade_dependency_checks = _as_int(backend_upgrade_summary.get("dependency_checks_passed", 0), 0)
    backend_upgrade_activation_checks = _as_int(backend_upgrade_summary.get("activation_task_checks_passed", 0), 0)

    api_interop_summary = (
        api_interop_policy.get("summary", {})
        if isinstance(api_interop_policy.get("summary"), dict)
        else {}
    )
    api_interop_ok = _as_bool(api_interop_policy.get("ok", False), False)
    api_interop_violations = _as_int(api_interop_summary.get("violation_count", 0), 0)
    api_interop_release_phase = str(api_interop_summary.get("release_phase", "foundation")).strip().lower() or "foundation"
    api_interop_dependency_checks = _as_int(api_interop_summary.get("dependency_checks_passed", 0), 0)
    api_interop_activation_checks = _as_int(api_interop_summary.get("activation_prereq_checks_passed", 0), 0)

    backlog_summary = (
        deterministic_backlog.get("summary", {})
        if isinstance(deterministic_backlog.get("summary"), dict)
        else {}
    )
    backlog_ok = _as_bool(deterministic_backlog.get("ok", True), True)
    backlog_ready_after = _as_int(backlog_summary.get("ready_after", 0), 0)
    backlog_ready_min = _as_int(backlog_summary.get("ready_min", 0), 0)
    backlog_ready_target = _as_int(backlog_summary.get("ready_target", 0), 0)
    backlog_ready_max = _as_int(backlog_summary.get("ready_max", 0), 0)
    backlog_done_after = _as_int(backlog_summary.get("done_after", 0), 0)
    backlog_action_count = _as_int(backlog_summary.get("action_count", 0), 0)
    backlog_updated = _as_bool(deterministic_backlog.get("backlog_updated", False), False)
    pr_remediation_summary = (
        pr_approval_remediation.get("summary", {})
        if isinstance(pr_approval_remediation.get("summary"), dict)
        else {}
    )
    pr_remediation_artifact_exists = paths["pr_approval_remediation"].exists()
    pr_remediation_ok = _as_bool(pr_approval_remediation.get("ok", False), False)
    pr_remediation_open_prs_seen = _as_int(pr_remediation_summary.get("open_prs_seen", 0), 0)
    pr_remediation_approved_count = _as_int(pr_remediation_summary.get("approved_count", 0), 0)
    pr_remediation_self_blocked_count = _as_int(pr_remediation_summary.get("self_blocked_count", 0), 0)
    pr_remediation_other_blocked_count = _as_int(pr_remediation_summary.get("other_blocked_count", 0), 0)

    routing = routing_policy.get("routing", {}) if isinstance(routing_policy.get("routing"), dict) else {}
    routing_strategy = str(routing.get("strategy", "")).strip().lower()
    local_first = _as_bool(routing.get("local_first", routing_strategy == "local_first"), routing_strategy == "local_first")
    saturate_local = (
        str(routing.get("local_saturation_policy", "")).strip() == "saturate_before_hosted"
        or _as_bool(routing.get("saturate_local_before_hosted", False), False)
    )

    conversations_ok = paths["conversations"].exists() and paths["conversations"].stat().st_size > 0
    events_ok = paths["events"].exists() and paths["events"].stat().st_size > 0

    unit_tests_ok = _quality_gate_state(strict_health, "unit_tests")
    lint_ok = _quality_gate_state(strict_health, "lint")
    typecheck_ok = _quality_gate_state(strict_health, "typecheck")
    security_scan_ok = _quality_gate_state(strict_health, "security_scan")
    security_audit_ok = _quality_gate_state(strict_health, "security_audit")
    gate_states = [unit_tests_ok, lint_ok, typecheck_ok, security_scan_ok, security_audit_ok]
    evaluated_states = [state for state in gate_states if state is not None]
    rigorous_testing_ok = bool(strict_pass) and all(evaluated_states) if evaluated_states else bool(strict_pass)

    criteria: list[dict[str, Any]] = []

    def add_criterion(
        *,
        criterion_id: str,
        description: str,
        ok: bool,
        evidence: list[str],
        blocker_reason: str,
        next_action: str,
        escalation_target: str = "T1",
    ) -> None:
        criteria.append(
            {
                "id": criterion_id,
                "description": description,
                "ok": bool(ok),
                "evidence": evidence,
                "blocker_reason": blocker_reason if not ok else "",
                "next_action": next_action if not ok else "",
                "escalation_target": escalation_target if not ok else "",
            }
        )

    add_criterion(
        criterion_id="swarm_independent_execution",
        description="Swarm runtime can operate independently and continue execution.",
        ok=supervisor_running and runner_effective_live,
        evidence=[
            f"supervisor_pid={supervisor_pid}",
            f"runner_pid={runner_pid}",
            f"runner_effective_live={runner_effective_live}",
            f"heartbeat_phase={heartbeat_phase}",
            f"heartbeat_age_sec={heartbeat_age_sec}",
            f"ready_queue_task_count={ready_task_count}",
        ],
        blocker_reason="supervisor_or_runner_not_running",
        next_action="restart supervisor/runner and verify heartbeat freshness.",
    )

    add_criterion(
        criterion_id="git_transparency",
        description="Git coordination and trace artifacts are transparent.",
        ok=conversations_ok and events_ok,
        evidence=[
            f"conversations={paths['conversations']}",
            f"events={paths['events']}",
        ],
        blocker_reason="missing_or_empty_coordination_artifacts",
        next_action="restore conversation/event artifact emission and verify non-empty writes.",
    )

    add_criterion(
        criterion_id="pipeline_policy_enforced",
        description="Deterministic pipeline policy and strict gates are enforced.",
        ok=strict_pass,
        evidence=[
            f"strict_score={strict_score}",
            f"strict_threshold={strict_threshold}",
        ],
        blocker_reason="strict_swarm_health_gate_failed",
        next_action="resolve strict gate deductions and re-run strict health.",
    )

    dashboard_http_live, dashboard_http_base = _dashboard_http_live(dashboard_meta)
    dashboard_live = (
        dashboard_running_pid
        or _as_bool(dashboard_meta.get("running", False), False)
        or dashboard_http_live
    )

    add_criterion(
        criterion_id="monitoring_trusted",
        description="Monitoring and dashboard are active and trusted.",
        ok=(
            dashboard_live
            and current_todo_running
            and todo_report_age_sec >= 0
            and todo_report_age_sec <= 7200
        ),
        evidence=[
            f"dashboard_live={dashboard_live}",
            f"dashboard_running={dashboard_meta.get('running', False)}",
            f"dashboard_pid={dashboard_pid}",
            f"dashboard_http_live={dashboard_http_live}",
            f"dashboard_http_base={dashboard_http_base}",
            f"current_todo_pid={current_todo_pid}",
            f"todo_report_age_sec={todo_report_age_sec}",
        ],
        blocker_reason="monitoring_or_dashboard_not_current",
        next_action="restart monitoring daemons and verify fresh health artifacts.",
    )

    add_criterion(
        criterion_id="launch_land_graceful",
        description="Launch/land lifecycle remains graceful under supervision.",
        ok=supervisor_running and runner_effective_live and dashboard_live,
        evidence=[
            f"supervisor_running={supervisor_running}",
            f"runner_running={runner_running}",
            f"runner_effective_live={runner_effective_live}",
            f"heartbeat_phase={heartbeat_phase}",
            f"dashboard_live={dashboard_live}",
            f"dashboard_pid_running={dashboard_running_pid}",
            f"dashboard_http_live={dashboard_http_live}",
        ],
        blocker_reason="lifecycle_process_missing",
        next_action="reconcile daemon PID files and restart missing lifecycle processes.",
    )

    add_criterion(
        criterion_id="local_first_routing",
        description="Local-first routing and saturation preference are active.",
        ok=local_first and saturate_local,
        evidence=[
            f"local_first={local_first}",
            f"local_saturation_policy={routing.get('local_saturation_policy', '')}",
            f"required_endpoint_unhealthy={endpoint_unhealthy}",
            f"optional_endpoint_unhealthy={optional_endpoint_unhealthy}",
            f"required_endpoint_total={endpoint_required_total}",
        ],
        blocker_reason="local_first_policy_not_enforced",
        next_action="enforce local-first routing policy and reduce required endpoint failures.",
    )

    add_criterion(
        criterion_id="swarm_lanes_configured",
        description="Swarm lane control-plane remains configured with at least one enabled runnable lane.",
        ok=lanes_total_count > 0 and lanes_enabled_count > 0 and lanes_enabled_runnable_count > 0,
        evidence=[
            f"lanes_file={paths['lanes_config']}",
            f"lanes_total_count={lanes_total_count}",
            f"lanes_enabled_count={lanes_enabled_count}",
            f"lanes_enabled_runnable_count={lanes_enabled_runnable_count}",
            f"lanes_owner_counts={json.dumps(lanes_owner_counts, sort_keys=True)}",
            f"lane_command_missing_owners={','.join(lane_command_missing_owners)}",
            f"lane_command_missing_values={','.join(lane_command_missing_values)}",
        ],
        blocker_reason="swarm_lanes_not_configured_or_unrunnable",
        next_action=(
            "restore non-empty lanes.json, keep at least one enabled lane with an available owner runtime, "
            "and re-run lanes-status before cycle reporting."
        ),
    )

    add_criterion(
        criterion_id="swarm_budget_guardrails",
        description="Swarm daily budget guardrails and cost health detection are active.",
        ok=provider_health_effective_ok and (not budget_enabled or budget_state in {"ok", "warning"}),
        evidence=[
            f"provider_cost_health_ok={provider_health_ok}",
            f"provider_telemetry_mode={provider_telemetry_mode}",
            f"provider_health_effective_ok={provider_health_effective_ok}",
            f"budget_enabled={budget_enabled}",
            f"budget_state={budget_state}",
            f"budget_hard_stop={budget_hard_stop}",
            f"budget_daily_spend_usd={budget_daily_spend_usd}",
            f"budget_daily_cap_usd={budget_daily_cap_usd}",
            f"budget_daily_remaining_usd={budget_daily_remaining_usd}",
        ],
        blocker_reason="swarm_budget_guardrails_unhealthy",
        next_action=(
            "enforce $100/day swarm cap, pause non-critical lane starts when cap is reached, "
            "and restore provider cost health telemetry before resuming scale-up actions."
        ),
    )

    add_criterion(
        criterion_id="t1_basic_model_policy",
        description="Basic coding tasks use T1 models unless explicitly escalated.",
        ok=t1_policy_ok and t1_policy_violations == 0 and t1_policy_observability_ok,
        evidence=[
            f"t1_policy_ok={t1_policy_ok}",
            f"t1_policy_observability_ok={t1_policy_observability_ok}",
            f"t1_policy_violations={t1_policy_violations}",
            f"t1_policy_basic_tasks={t1_policy_basic_tasks}",
            f"t1_policy_scanned_metrics={t1_policy_scanned}",
            f"t1_policy_latest_metric_age_minutes={t1_policy_latest_metric_age_minutes}",
            f"t1_policy_parse_skip_ratio={t1_policy_parse_skip_ratio}",
        ],
        blocker_reason="t1_basic_model_policy_violation",
        next_action="route basic coding tasks to T1 models, document escalation evidence, and restore T1 telemetry observability health.",
    )

    add_criterion(
        criterion_id="t1_pr_ratio_policy",
        description="PR mix enforces T1-majority delivery and explicit tier labels for escalations.",
        ok=pr_tier_ok and pr_tier_violation_count == 0 and pr_tier_t1_ratio >= pr_tier_min_t1_ratio,
        evidence=[
            f"pr_tier_policy_ok={pr_tier_ok}",
            f"pr_tier_violation_count={pr_tier_violation_count}",
            f"pr_tier_reviewed_prs={pr_tier_reviewed_prs}",
            f"pr_tier_ratio_base_prs={pr_tier_ratio_base_prs}",
            f"pr_tier_t1_count={pr_tier_t1_count}",
            f"pr_tier_escalated_count={pr_tier_escalated_count}",
            f"pr_tier_unlabeled_count={pr_tier_unlabeled_count}",
            f"pr_tier_conflict_count={pr_tier_conflict_count}",
            f"pr_tier_t1_ratio={pr_tier_t1_ratio}",
            f"pr_tier_escalated_ratio={pr_tier_escalated_ratio}",
            f"pr_tier_unlabeled_ratio={pr_tier_unlabeled_ratio}",
            f"pr_tier_min_t1_ratio={pr_tier_min_t1_ratio}",
        ],
        blocker_reason="t1_pr_ratio_policy_violation",
        next_action=(
            "label PRs deterministically by tier, keep the majority of PRs T1-scoped, "
            "and reserve escalated labels for explicitly justified exceptions."
        ),
    )

    add_criterion(
        criterion_id="non_admin_default_with_breakglass",
        description="Swarms and RPA lanes run non-admin by default with tightly controlled temporary breakglass elevation.",
        ok=privilege_ok and privilege_violations == 0,
        evidence=[
            f"privilege_policy_ok={privilege_ok}",
            f"privilege_policy_violations={privilege_violations}",
            f"privilege_policy_latest_event_age_minutes={privilege_latest_event_age_minutes}",
            f"privilege_policy_breakglass_events={privilege_breakglass_events}",
        ],
        blocker_reason="least_privilege_or_breakglass_policy_violation",
        next_action="enforce least-privilege defaults and require full breakglass evidence (reason, scope, TTL, rollback, audit trail) for any elevation.",
    )

    add_criterion(
        criterion_id="ticket_branch_pr_workflow",
        description="Ticket-linked branching, sub-400-line contiguous change blocks, and PR review/approval workflow are enforced.",
        ok=git_delivery_ok and git_delivery_violations == 0,
        evidence=[
            f"git_delivery_policy_ok={git_delivery_ok}",
            f"git_delivery_policy_violations={git_delivery_violations}",
            f"git_delivery_branch={git_delivery_branch}",
            f"git_delivery_ticket_branch_match={git_delivery_ticket_branch_match}",
            f"git_delivery_effective_changed_lines={git_delivery_effective_changed_lines}",
            f"git_delivery_max_changed_lines={git_delivery_max_changed_lines}",
            f"git_delivery_pr_found={git_delivery_pr_found}",
            f"git_delivery_pr_approvals={git_delivery_pr_approvals}",
        ],
        blocker_reason="ticket_branch_pr_workflow_policy_violation",
        next_action=(
            "split work into smaller ticket-linked branch blocks (<400 lines where feasible), "
            "open PRs for each block, and complete review + approval before completion claims."
        ),
    )

    add_criterion(
        criterion_id="git_hygiene_instrumented",
        description="Git branch hygiene instrumentation enforces branch-count and stale-branch controls.",
        ok=git_hygiene_ok and git_hygiene_violations == 0,
        evidence=[
            f"git_hygiene_ok={git_hygiene_ok}",
            f"git_hygiene_violations={git_hygiene_violations}",
            f"git_hygiene_local_count={git_hygiene_local_count}",
            f"git_hygiene_remote_count={git_hygiene_remote_count}",
            f"git_hygiene_total_count={git_hygiene_total_count}",
            f"git_hygiene_stale_local_count={git_hygiene_stale_local_count}",
            f"git_hygiene_max_total={git_hygiene_max_total}",
        ],
        blocker_reason="git_hygiene_policy_violation",
        next_action=(
            "prune stale branches, archive/close merged branches, and keep branch counts under "
            "policy thresholds with continuous hygiene checks."
        ),
    )

    add_criterion(
        criterion_id="git_hygiene_remediation",
        description="Git branch remediation loop runs each cycle and captures deterministic deletion telemetry.",
        ok=git_hygiene_remediation_artifact_exists and git_hygiene_remediation_ok and git_hygiene_remediation_error_count == 0,
        evidence=[
            f"git_hygiene_remediation_artifact_exists={git_hygiene_remediation_artifact_exists}",
            f"git_hygiene_remediation_ok={git_hygiene_remediation_ok}",
            f"git_hygiene_remediation_error_count={git_hygiene_remediation_error_count}",
            f"git_hygiene_remediation_remote_stale_prefix_count={git_hygiene_remediation_remote_stale_prefix_count}",
            f"git_hygiene_remediation_local_stale_prefix_count={git_hygiene_remediation_local_stale_prefix_count}",
            f"git_hygiene_remediation_remote_candidates={git_hygiene_remediation_remote_candidates}",
            f"git_hygiene_remediation_local_candidates={git_hygiene_remediation_local_candidates}",
            f"git_hygiene_remediation_remote_blocked_open_pr_count={git_hygiene_remediation_remote_blocked_open_pr_count}",
            f"git_hygiene_remediation_remote_blocked_unmerged_count={git_hygiene_remediation_remote_blocked_unmerged_count}",
            f"git_hygiene_remediation_local_blocked_unmerged_count={git_hygiene_remediation_local_blocked_unmerged_count}",
            f"git_hygiene_remediation_local_blocked_worktree_count={git_hygiene_remediation_local_blocked_worktree_count}",
            f"git_hygiene_remediation_local_blocked_worktree_dirty_count={git_hygiene_remediation_local_blocked_worktree_dirty_count}",
            f"git_hygiene_remediation_dirty_worktree_blocker_count={git_hygiene_remediation_dirty_worktree_blocker_count}",
            f"git_hygiene_remediation_dirty_blocker_branches={','.join(git_hygiene_remediation_dirty_blocker_branches)}",
            f"git_hygiene_remediation_local_force_deleted_count={git_hygiene_remediation_local_force_deleted_count}",
            f"git_hygiene_remediation_worktree_prune_removed_count={git_hygiene_remediation_worktree_prune_removed_count}",
            f"git_hygiene_remediation_worktree_remove_attempted_count={git_hygiene_remediation_worktree_remove_attempted_count}",
            f"git_hygiene_remediation_worktree_removed_count={git_hygiene_remediation_worktree_removed_count}",
            f"git_hygiene_remediation_worktree_remove_failed_count={git_hygiene_remediation_worktree_remove_failed_count}",
            f"git_hygiene_remediation_remote_deleted={git_hygiene_remediation_remote_deleted}",
            f"git_hygiene_remediation_local_deleted={git_hygiene_remediation_local_deleted}",
        ],
        blocker_reason="git_hygiene_remediation_unhealthy",
        next_action=(
            "rerun deterministic git hygiene remediation, resolve remediation execution errors, "
            "and confirm candidate backlog drains while preserving open PR heads."
        ),
    )

    add_criterion(
        criterion_id="backend_upgrade_policy_ready",
        description="Backend portfolio routing and upgrade lifecycle policy gates are defined and passing.",
        ok=backend_upgrade_ok and backend_upgrade_violations == 0,
        evidence=[
            f"backend_upgrade_policy_ok={backend_upgrade_ok}",
            f"backend_upgrade_policy_violations={backend_upgrade_violations}",
            f"backend_upgrade_release_phase={backend_upgrade_release_phase}",
            f"backend_upgrade_dependency_checks={backend_upgrade_dependency_checks}",
            f"backend_upgrade_activation_checks={backend_upgrade_activation_checks}",
        ],
        blocker_reason="backend_or_upgrade_policy_gate_failed",
        next_action=(
            "fix backend/upgrade policy violations, maintain user-mode-only backend portfolio, "
            "and ensure upgrade automation remains sequenced after routing + A/B readiness."
        ),
    )

    add_criterion(
        criterion_id="api_interop_policy_ready",
        description="External API interoperability standards and conformance policy gates are defined and passing.",
        ok=api_interop_ok and api_interop_violations == 0,
        evidence=[
            f"api_interop_policy_ok={api_interop_ok}",
            f"api_interop_policy_violations={api_interop_violations}",
            f"api_interop_release_phase={api_interop_release_phase}",
            f"api_interop_dependency_checks={api_interop_dependency_checks}",
            f"api_interop_activation_checks={api_interop_activation_checks}",
        ],
        blocker_reason="api_interop_policy_gate_failed",
        next_action=(
            "fix API interoperability policy violations, enforce REST/MCP and standards conformance gates, "
            "and preserve explicit-trigger cloud escalation with audit-ready evidence."
        ),
    )

    add_criterion(
        criterion_id="deterministic_backlog_control",
        description="Backlog ready-window and status transitions are controlled by deterministic instrumentation loops.",
        ok=backlog_ok and backlog_ready_after >= backlog_ready_min and backlog_ready_after <= backlog_ready_max,
        evidence=[
            f"backlog_ok={backlog_ok}",
            f"ready_after={backlog_ready_after}",
            f"ready_min={backlog_ready_min}",
            f"ready_target={backlog_ready_target}",
            f"ready_max={backlog_ready_max}",
            f"done_after={backlog_done_after}",
            f"action_count={backlog_action_count}",
            f"backlog_updated={backlog_updated}",
        ],
        blocker_reason="deterministic_backlog_control_unhealthy",
        next_action=(
            "run deterministic backlog control loop, restore ready-window bounds, "
            "and ensure task completion signals are marker-driven and auditable."
        ),
    )

    add_criterion(
        criterion_id="pr_approval_remediation",
        description="PR approval remediation instrumentation runs each cycle and clears non-self approval blockers.",
        ok=pr_remediation_artifact_exists and pr_remediation_other_blocked_count == 0,
        evidence=[
            f"pr_remediation_artifact_exists={pr_remediation_artifact_exists}",
            f"pr_remediation_ok={pr_remediation_ok}",
            f"pr_remediation_open_prs_seen={pr_remediation_open_prs_seen}",
            f"pr_remediation_approved_count={pr_remediation_approved_count}",
            f"pr_remediation_self_blocked_count={pr_remediation_self_blocked_count}",
            f"pr_remediation_other_blocked_count={pr_remediation_other_blocked_count}",
        ],
        blocker_reason="pr_approval_remediation_unhealthy",
        next_action=(
            "run PR approval remediation, resolve non-self approval failures, and track self-approval blockers "
            "as reviewer-capacity governance backlog."
        ),
    )

    add_criterion(
        criterion_id="rigorous_scoped_testing",
        description="Rigorous scoped testing gates pass for swarm readiness.",
        ok=rigorous_testing_ok,
        evidence=[
            f"unit_tests_ok={unit_tests_ok}",
            f"lint_ok={lint_ok}",
            f"typecheck_ok={typecheck_ok}",
            f"security_scan_ok={security_scan_ok}",
            f"security_audit_ok={security_audit_ok}",
        ],
        blocker_reason="required_quality_or_security_gate_failed",
        next_action="triage lint/typecheck/security gate failures and rerun strict health.",
    )

    failed_criteria = [item for item in criteria if not _as_bool(item.get("ok", False), False)]
    blocked_items: list[dict[str, Any]] = []
    for item in failed_criteria:
        blocked_items.append(
            {
                "status": "blocked",
                "blocker_id": f"BLK-{str(item.get('id', 'unknown')).upper()}",
                "blocker_reason": str(item.get("blocker_reason", "unknown")).strip(),
                "escalation_target": str(item.get("escalation_target", "T1")).strip() or "T1",
                "next_action": str(item.get("next_action", "")).strip(),
                "evidence": item.get("evidence", []),
            }
        )

    if not strict_pass:
        strict_deductions = strict_health.get("deductions", []) if isinstance(strict_health.get("deductions"), list) else []
        for deduction in strict_deductions:
            if not isinstance(deduction, dict):
                continue
            code = str(deduction.get("code", "")).strip()
            reason = str(deduction.get("reason", "")).strip()
            if not code:
                continue
            blocked_items.append(
                {
                    "status": "blocked",
                    "blocker_id": f"BLK-DEDUCTION-{code.upper()}",
                    "blocker_reason": reason or code,
                    "escalation_target": "T1",
                    "next_action": f"Remediate health deduction `{code}` and re-run strict gate.",
                    "evidence": [f"deduction_points={_as_int(deduction.get('points', 0), 0)}"],
                }
            )

    report = {
        "schema_version": "swarm-cycle-report.v1",
        "generated_at_utc": _utc_now_iso(),
        "root_dir": str(ops_root),
        "product_root": str(product_root),
        "summary": {
            "criteria_total": len(criteria),
            "criteria_passed": len(criteria) - len(failed_criteria),
            "criteria_failed": len(failed_criteria),
            "strict_pass_gate": strict_pass,
            "strict_score": strict_score,
            "strict_threshold": strict_threshold,
            "operational_pass_gate": operational_pass,
            "operational_score": operational_score,
            "required_endpoint_unhealthy": endpoint_unhealthy,
            "optional_endpoint_unhealthy": optional_endpoint_unhealthy,
            "todo_stale_file_count": todo_stale,
            "todo_unassigned_active_task_total": todo_unassigned,
            "todo_warning_count": todo_warning_count,
            "ready_queue_task_count": ready_task_count,
            "lanes_total_count": lanes_total_count,
            "lanes_enabled_count": lanes_enabled_count,
            "lanes_enabled_runnable_count": lanes_enabled_runnable_count,
            "lane_command_missing_owners": lane_command_missing_owners,
            "lane_command_missing_values": lane_command_missing_values,
            "provider_summary_ok": provider_ok,
            "provider_cost_health_ok": provider_health_ok,
            "provider_telemetry_mode": provider_telemetry_mode,
            "provider_health_effective_ok": provider_health_effective_ok,
            "swarm_budget_enabled": budget_enabled,
            "swarm_budget_state": budget_state,
            "swarm_budget_hard_stop": budget_hard_stop,
            "swarm_budget_daily_spend_usd": budget_daily_spend_usd,
            "swarm_budget_daily_cap_usd": budget_daily_cap_usd,
            "swarm_budget_daily_remaining_usd": budget_daily_remaining_usd,
            "t1_policy_ok": t1_policy_ok,
            "t1_policy_violation_count": t1_policy_violations,
            "t1_policy_observability_ok": t1_policy_observability_ok,
            "pr_tier_policy_ok": pr_tier_ok,
            "pr_tier_policy_violation_count": pr_tier_violation_count,
            "pr_tier_reviewed_prs": pr_tier_reviewed_prs,
            "pr_tier_ratio_base_prs": pr_tier_ratio_base_prs,
            "pr_tier_t1_count": pr_tier_t1_count,
            "pr_tier_escalated_count": pr_tier_escalated_count,
            "pr_tier_unlabeled_count": pr_tier_unlabeled_count,
            "pr_tier_conflict_count": pr_tier_conflict_count,
            "pr_tier_t1_ratio": pr_tier_t1_ratio,
            "pr_tier_escalated_ratio": pr_tier_escalated_ratio,
            "pr_tier_unlabeled_ratio": pr_tier_unlabeled_ratio,
            "pr_tier_min_t1_ratio": pr_tier_min_t1_ratio,
            "privilege_policy_ok": privilege_ok,
            "privilege_policy_violation_count": privilege_violations,
            "git_delivery_policy_ok": git_delivery_ok,
            "git_delivery_policy_violation_count": git_delivery_violations,
            "git_delivery_effective_changed_lines": git_delivery_effective_changed_lines,
            "git_delivery_pr_found": git_delivery_pr_found,
            "git_hygiene_ok": git_hygiene_ok,
            "git_hygiene_violation_count": git_hygiene_violations,
            "git_hygiene_local_branch_count": git_hygiene_local_count,
            "git_hygiene_remote_branch_count": git_hygiene_remote_count,
            "git_hygiene_total_branch_count": git_hygiene_total_count,
            "git_hygiene_stale_local_branch_count": git_hygiene_stale_local_count,
            "git_hygiene_max_total_branches": git_hygiene_max_total,
            "git_hygiene_remediation_artifact_exists": git_hygiene_remediation_artifact_exists,
            "git_hygiene_remediation_ok": git_hygiene_remediation_ok,
            "git_hygiene_remediation_error_count": git_hygiene_remediation_error_count,
            "git_hygiene_remediation_remote_stale_prefix_count": git_hygiene_remediation_remote_stale_prefix_count,
            "git_hygiene_remediation_local_stale_prefix_count": git_hygiene_remediation_local_stale_prefix_count,
            "git_hygiene_remediation_remote_candidates": git_hygiene_remediation_remote_candidates,
            "git_hygiene_remediation_local_candidates": git_hygiene_remediation_local_candidates,
            "git_hygiene_remediation_remote_blocked_open_pr_count": git_hygiene_remediation_remote_blocked_open_pr_count,
            "git_hygiene_remediation_remote_blocked_unmerged_count": git_hygiene_remediation_remote_blocked_unmerged_count,
            "git_hygiene_remediation_local_blocked_unmerged_count": git_hygiene_remediation_local_blocked_unmerged_count,
            "git_hygiene_remediation_local_blocked_worktree_count": git_hygiene_remediation_local_blocked_worktree_count,
            "git_hygiene_remediation_local_blocked_worktree_dirty_count": git_hygiene_remediation_local_blocked_worktree_dirty_count,
            "git_hygiene_remediation_dirty_worktree_blocker_count": git_hygiene_remediation_dirty_worktree_blocker_count,
            "git_hygiene_remediation_dirty_blocker_branches": git_hygiene_remediation_dirty_blocker_branches,
            "git_hygiene_remediation_local_force_deleted_count": git_hygiene_remediation_local_force_deleted_count,
            "git_hygiene_remediation_worktree_prune_removed_count": git_hygiene_remediation_worktree_prune_removed_count,
            "git_hygiene_remediation_worktree_remove_attempted_count": git_hygiene_remediation_worktree_remove_attempted_count,
            "git_hygiene_remediation_worktree_removed_count": git_hygiene_remediation_worktree_removed_count,
            "git_hygiene_remediation_worktree_remove_failed_count": git_hygiene_remediation_worktree_remove_failed_count,
            "git_hygiene_remediation_remote_deleted": git_hygiene_remediation_remote_deleted,
            "git_hygiene_remediation_local_deleted": git_hygiene_remediation_local_deleted,
            "backend_upgrade_policy_ok": backend_upgrade_ok,
            "backend_upgrade_policy_violation_count": backend_upgrade_violations,
            "backend_upgrade_release_phase": backend_upgrade_release_phase,
            "backend_upgrade_dependency_checks": backend_upgrade_dependency_checks,
            "backend_upgrade_activation_checks": backend_upgrade_activation_checks,
            "api_interop_policy_ok": api_interop_ok,
            "api_interop_policy_violation_count": api_interop_violations,
            "api_interop_release_phase": api_interop_release_phase,
            "api_interop_dependency_checks": api_interop_dependency_checks,
            "api_interop_activation_checks": api_interop_activation_checks,
            "deterministic_backlog_ok": backlog_ok,
            "deterministic_backlog_ready_after": backlog_ready_after,
            "deterministic_backlog_ready_min": backlog_ready_min,
            "deterministic_backlog_ready_target": backlog_ready_target,
            "deterministic_backlog_ready_max": backlog_ready_max,
            "deterministic_backlog_done_after": backlog_done_after,
            "deterministic_backlog_action_count": backlog_action_count,
            "deterministic_backlog_updated": backlog_updated,
            "pr_remediation_ok": pr_remediation_ok,
            "pr_remediation_artifact_exists": pr_remediation_artifact_exists,
            "pr_remediation_open_prs_seen": pr_remediation_open_prs_seen,
            "pr_remediation_approved_count": pr_remediation_approved_count,
            "pr_remediation_self_blocked_count": pr_remediation_self_blocked_count,
            "pr_remediation_other_blocked_count": pr_remediation_other_blocked_count,
        },
        "processes": {
            "supervisor": {"running": supervisor_running, "pid": supervisor_pid},
            "runner": {"running": runner_running, "pid": runner_pid},
            "dashboard": {"running": dashboard_running_pid, "pid": dashboard_pid},
            "todo_current_daemon": {"running": current_todo_running, "pid": current_todo_pid},
        },
        "criteria": criteria,
        "blocked_cycle_escalations": blocked_items,
        "artifacts": {key: str(path) for key, path in paths.items()},
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    criteria = report.get("criteria", []) if isinstance(report.get("criteria"), list) else []
    blocked = report.get("blocked_cycle_escalations", []) if isinstance(report.get("blocked_cycle_escalations"), list) else []
    lines: list[str] = []
    lines.append("# Swarm Cycle Report")
    lines.append("")
    lines.append(f"- Generated UTC: `{report.get('generated_at_utc', '')}`")
    lines.append(f"- Strict health: pass=`{summary.get('strict_pass_gate', False)}` score=`{summary.get('strict_score', 0)}` threshold=`{summary.get('strict_threshold', 0)}`")
    lines.append(f"- Operational health: pass=`{summary.get('operational_pass_gate', False)}` score=`{summary.get('operational_score', 0)}`")
    lines.append(f"- Required endpoint unhealthy: `{summary.get('required_endpoint_unhealthy', 0)}`")
    lines.append(f"- Optional endpoint unhealthy: `{summary.get('optional_endpoint_unhealthy', 0)}`")
    lines.append(f"- Ready queue tasks: `{summary.get('ready_queue_task_count', 0)}`")
    lines.append("")
    lines.append("## Completion Gates")
    lines.append("")
    for item in criteria:
        if not isinstance(item, dict):
            continue
        marker = "PASS" if _as_bool(item.get("ok", False), False) else "FAIL"
        lines.append(f"- [{marker}] `{item.get('id', 'unknown')}`: {item.get('description', '')}")
        evidence = item.get("evidence", []) if isinstance(item.get("evidence"), list) else []
        if evidence:
            lines.append(f"  evidence: {', '.join(str(v) for v in evidence)}")
        if not _as_bool(item.get("ok", False), False):
            lines.append(f"  blocker_reason: {item.get('blocker_reason', '')}")
            lines.append(f"  next_action: {item.get('next_action', '')}")
    lines.append("")
    lines.append("## Blocked Cycle Escalations")
    lines.append("")
    if not blocked:
        lines.append("- none")
    else:
        for item in blocked:
            if not isinstance(item, dict):
                continue
            lines.append(f"- `{item.get('blocker_id', 'unknown')}` status=`{item.get('status', '')}` target=`{item.get('escalation_target', '')}`")
            lines.append(f"  reason: {item.get('blocker_reason', '')}")
            lines.append(f"  next_action: {item.get('next_action', '')}")
    lines.append("")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate swarm cycle report and blocked-cycle escalations.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--output", default="artifacts/autonomy/swarm_cycle_report.json", help="Report JSON path.")
    parser.add_argument("--markdown", default="artifacts/autonomy/swarm_cycle_report.md", help="Report markdown path.")
    parser.add_argument(
        "--blocked-output",
        default="artifacts/autonomy/blocked_cycle_escalations.json",
        help="Blocked-cycle escalation JSON path.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = Path(args.root).expanduser().resolve()
    report_path = Path(args.output).expanduser().resolve()
    markdown_path = Path(args.markdown).expanduser().resolve()
    blocked_path = Path(args.blocked_output).expanduser().resolve()

    report = build_report(root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    blocked_path.parent.mkdir(parents=True, exist_ok=True)

    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")

    blocked_payload = {
        "schema_version": "blocked-cycle-escalation.v1",
        "generated_at_utc": report.get("generated_at_utc", _utc_now_iso()),
        "source_report": str(report_path),
        "blocked_items": report.get("blocked_cycle_escalations", []),
    }
    blocked_path.write_text(json.dumps(blocked_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    print(
        json.dumps(
            {
                "output": str(report_path),
                "markdown": str(markdown_path),
                "blocked_output": str(blocked_path),
                "criteria_failed": summary.get("criteria_failed", 0),
                "strict_pass_gate": summary.get("strict_pass_gate", False),
                "operational_pass_gate": summary.get("operational_pass_gate", False),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
