"""CLI entrypoint for reusable Orxaq autonomy package."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .context import write_default_skill_protocol
from .dashboard import start_dashboard
from .ide import generate_workspace, open_in_ide
from .manager import (
    ManagerConfig,
    bootstrap_background,
    conversations_snapshot,
    dashboard_status_snapshot,
    ensure_background,
    ensure_dashboard_background,
    health_snapshot,
    install_keepalive,
    lane_status_snapshot,
    keepalive_status,
    lane_status_fallback_snapshot,
    load_lane_specs,
    preflight,
    reset_state,
    render_monitor_text,
    run_foreground,
    ensure_lanes_background,
    start_lanes_background,
    start_dashboard_background,
    start_background,
    status_snapshot,
    stop_lanes_background,
    stop_dashboard_background,
    stop_background,
    supervise_foreground,
    tail_logs,
    tail_dashboard_logs,
    monitor_snapshot,
    uninstall_keepalive,
)


def _apply_conversation_filters(
    payload: dict[str, Any],
    *,
    owner: str = "",
    lane_id: str = "",
    event_type: str = "",
    contains: str = "",
    tail: int = 0,
) -> dict[str, Any]:
    owner_filter = owner.strip().lower()
    lane_filter = lane_id.strip().lower()
    type_filter = event_type.strip().lower()
    contains_filter = contains.strip().lower()
    events = payload.get("events", [])
    if not isinstance(events, list):
        events = []

    filtered: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        owner_value = str(item.get("owner", "")).strip().lower()
        lane_value = str(item.get("lane_id", "")).strip().lower()
        type_value = str(item.get("event_type", "")).strip().lower()
        haystack = " ".join(
            [
                str(item.get("timestamp", "")),
                str(item.get("owner", "")),
                str(item.get("lane_id", "")),
                str(item.get("task_id", "")),
                str(item.get("event_type", "")),
                str(item.get("content", "")),
            ]
        ).lower()
        if owner_filter and owner_value != owner_filter:
            continue
        if lane_filter and lane_value != lane_filter:
            continue
        if type_filter and type_value != type_filter:
            continue
        if contains_filter and contains_filter not in haystack:
            continue
        filtered.append(item)

    tail_count = max(0, int(tail))
    if tail_count:
        filtered = filtered[-tail_count:]

    owner_counts: dict[str, int] = {}
    for event in filtered:
        event_owner = str(event.get("owner", "unknown")).strip() or "unknown"
        owner_counts[event_owner] = owner_counts.get(event_owner, 0) + 1

    result = dict(payload)
    result["events"] = filtered
    result["owner_counts"] = owner_counts
    result["total_events"] = len(filtered)
    result["unfiltered_total_events"] = len(events)
    result["filters"] = {
        "owner": owner.strip(),
        "lane": lane_id.strip(),
        "event_type": event_type.strip(),
        "contains": contains.strip(),
        "tail": tail_count,
    }
    return result


def _filter_conversation_payload_for_lane(
    payload: dict[str, Any],
    *,
    lane_id: str = "",
) -> dict[str, Any]:
    requested_lane = lane_id.strip()
    source_reports = payload.get("sources", [])
    if not isinstance(source_reports, list):
        source_reports = []
    normalized_sources = [item for item in source_reports if isinstance(item, dict)]

    if not requested_lane:
        result = dict(payload)
        result["sources"] = normalized_sources
        result["suppressed_sources"] = []
        result["suppressed_source_count"] = 0
        result["suppressed_source_errors"] = []
        result["suppressed_source_error_count"] = 0
        return result

    retained_sources: list[dict[str, Any]] = []
    suppressed_sources: list[dict[str, Any]] = []
    for source in normalized_sources:
        source_lane = str(source.get("lane_id", "")).strip()
        if source_lane and source_lane != requested_lane:
            suppressed_sources.append(source)
            continue
        retained_sources.append(source)

    retained_errors = [
        str(source.get("error", "")).strip()
        for source in retained_sources
        if str(source.get("error", "")).strip()
    ]
    suppressed_source_errors = [
        str(source.get("error", "")).strip()
        for source in suppressed_sources
        if str(source.get("error", "")).strip()
    ]

    def _lane_error_matches_source(message: str, source: dict[str, Any]) -> bool:
        normalized = message.strip().lower()
        if not normalized:
            return False
        lane = str(source.get("lane_id", "")).strip().lower()
        if lane and re.search(rf"(^|[^a-z0-9_-]){re.escape(lane)}([^a-z0-9_-]|$)", normalized):
            return True
        source_error = str(source.get("error", "")).strip().lower()
        if source_error and source_error in normalized:
            return True
        for key in ("path", "resolved_path"):
            source_path = str(source.get(key, "")).strip().lower()
            if source_path and source_path in normalized:
                return True
        return False

    suppressed_payload_errors: list[str] = []
    payload_errors = payload.get("errors", [])
    if not isinstance(payload_errors, list):
        payload_errors = []
    for entry in payload_errors:
        message = str(entry).strip()
        if not message:
            continue
        if any(_lane_error_matches_source(message, source) for source in suppressed_sources):
            suppressed_payload_errors.append(message)
            continue
        if message not in retained_errors:
            retained_errors.append(message)

    retained_source_failures = any(not bool(source.get("ok", False)) for source in retained_sources)
    partial = retained_source_failures or bool(retained_errors)
    filtered = dict(payload)
    filtered["sources"] = retained_sources
    filtered["errors"] = retained_errors
    filtered["ok"] = not partial
    filtered["partial"] = partial
    filtered["suppressed_sources"] = suppressed_sources
    filtered["suppressed_source_count"] = len(suppressed_sources)
    merged_suppressed_errors: list[str] = []
    for message in [*suppressed_source_errors, *suppressed_payload_errors]:
        if message and message not in merged_suppressed_errors:
            merged_suppressed_errors.append(message)
    filtered["suppressed_source_errors"] = merged_suppressed_errors
    filtered["suppressed_source_error_count"] = len(merged_suppressed_errors)
    return filtered


def _conversation_error_payload(
    cfg: ManagerConfig,
    *,
    error: str,
    owner: str = "",
    lane_id: str = "",
    event_type: str = "",
    contains: str = "",
    tail: int = 0,
) -> dict[str, Any]:
    source_path = Path(cfg.conversation_log_file)
    source_missing = not source_path.exists()
    return {
        "timestamp": "",
        "conversation_files": [str(source_path)],
        "total_events": 0,
        "events": [],
        "owner_counts": {},
        "sources": [
            {
                "path": str(source_path),
                "resolved_path": str(source_path),
                "kind": "primary",
                "resolved_kind": "primary",
                "lane_id": "",
                "owner": "",
                "ok": False,
                "missing": source_missing,
                "recoverable_missing": False,
                "fallback_used": False,
                "error": error,
                "event_count": 0,
            }
        ],
        "partial": True,
        "ok": False,
        "errors": [error],
        "unfiltered_total_events": 0,
        "filters": {
            "owner": owner.strip(),
            "lane": lane_id.strip(),
            "event_type": event_type.strip(),
            "contains": contains.strip(),
            "tail": max(0, int(tail)),
        },
    }


def _safe_conversations_snapshot(
    cfg: ManagerConfig,
    *,
    lines: int,
    include_lanes: bool,
    owner: str = "",
    lane_id: str = "",
    event_type: str = "",
    contains: str = "",
    tail: int = 0,
) -> dict[str, Any]:
    try:
        payload = conversations_snapshot(
            cfg,
            lines=lines,
            include_lanes=include_lanes,
        )
    except Exception as err:
        payload = _conversation_error_payload(
            cfg,
            error=str(err),
            owner=owner,
            lane_id=lane_id,
            event_type=event_type,
            contains=contains,
            tail=tail,
        )
    payload = _filter_conversation_payload_for_lane(payload, lane_id=lane_id)
    return _apply_conversation_filters(
        payload,
        owner=owner,
        lane_id=lane_id,
        event_type=event_type,
        contains=contains,
        tail=tail,
    )


def _lane_status_error_payload(cfg: ManagerConfig, *, error: str) -> dict[str, Any]:
    return {
        "timestamp": "",
        "lanes_file": str(cfg.lanes_file),
        "running_count": 0,
        "total_count": 0,
        "lanes": [],
        "health_counts": {},
        "owner_counts": {},
        "ok": False,
        "partial": True,
        "errors": [error],
    }


def _safe_lane_status_snapshot(cfg: ManagerConfig) -> dict[str, Any]:
    try:
        return lane_status_snapshot(cfg)
    except Exception as err:
        message = str(err)
        try:
            return lane_status_fallback_snapshot(cfg, error=message)
        except Exception:
            return _lane_status_error_payload(cfg, error=message)


def _lane_error_matches_requested_lane(
    error: str,
    requested_lane: str,
    *,
    known_lane_ids: set[str] | None = None,
) -> bool:
    message = str(error).strip()
    lane = requested_lane.strip().lower()
    if not message or not lane:
        return True
    if ":" not in message:
        return True
    prefix = message.split(":", 1)[0].strip().lower()
    if prefix == lane:
        return True
    if known_lane_ids and prefix in known_lane_ids:
        return False
    return True


def _filter_lane_status_payload(
    payload: dict[str, Any],
    *,
    requested_lane: str = "",
    lanes_file: Path,
) -> dict[str, Any]:
    lane_filter = requested_lane.strip()
    lane_items = payload.get("lanes", [])
    if not isinstance(lane_items, list):
        lane_items = []
    known_lane_ids = {
        str(item.get("id", "")).strip().lower()
        for item in lane_items
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    lane_errors = payload.get("errors", [])
    if not isinstance(lane_errors, list):
        lane_errors = []
    normalized_errors = [str(item).strip() for item in lane_errors if str(item).strip()]
    suppressed_errors: list[str] = []

    if lane_filter:
        lane_items = [lane for lane in lane_items if str(lane.get("id", "")).strip() == lane_filter]
        lane_specific_errors = [
            item
            for item in normalized_errors
            if _lane_error_matches_requested_lane(item, lane_filter, known_lane_ids=known_lane_ids)
        ]
        suppressed_errors = [item for item in normalized_errors if item not in lane_specific_errors]
        if lane_items:
            normalized_errors = lane_specific_errors
        else:
            normalized_errors = lane_specific_errors or normalized_errors
            if normalized_errors:
                normalized_errors.append(
                    f"Requested lane {lane_filter!r} is unavailable because lane status sources failed."
                )
            else:
                normalized_errors.append(f"Unknown lane id {lane_filter!r}. Update {lanes_file}.")

    health_counts: dict[str, int] = {}
    owner_counts: dict[str, dict[str, int]] = {}
    healthy_states = {"ok", "paused", "idle"}
    for lane in lane_items:
        if not isinstance(lane, dict):
            continue
        health = str(lane.get("health", "unknown")).strip().lower() or "unknown"
        health_counts[health] = health_counts.get(health, 0) + 1
        owner = str(lane.get("owner", "unknown")).strip() or "unknown"
        owner_entry = owner_counts.setdefault(owner, {"total": 0, "running": 0, "healthy": 0, "degraded": 0})
        owner_entry["total"] += 1
        if bool(lane.get("running", False)):
            owner_entry["running"] += 1
        if health in healthy_states:
            owner_entry["healthy"] += 1
        else:
            owner_entry["degraded"] += 1

    filtered = dict(payload)
    filtered["requested_lane"] = lane_filter or "all"
    filtered["lanes"] = lane_items
    filtered["running_count"] = sum(1 for lane in lane_items if bool(lane.get("running", False)))
    filtered["total_count"] = len(lane_items)
    filtered["health_counts"] = health_counts
    filtered["owner_counts"] = owner_counts
    filtered["errors"] = normalized_errors
    filtered["suppressed_errors"] = suppressed_errors
    if lane_filter and lane_items and not normalized_errors:
        filtered["ok"] = True
        filtered["partial"] = False
    else:
        filtered["ok"] = bool(payload.get("ok", not normalized_errors)) and not bool(normalized_errors)
        filtered["partial"] = bool(payload.get("partial", False)) or bool(normalized_errors)
    return filtered


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lane_conversation_source_health(payload: dict[str, Any], *, lane_id: str) -> dict[str, Any]:
    requested_lane = lane_id.strip()
    reports = payload.get("sources", [])
    if not isinstance(reports, list):
        reports = []
    lane_reports = [
        item
        for item in reports
        if isinstance(item, dict) and str(item.get("lane_id", "")).strip() == requested_lane
    ]
    errors: list[str] = []
    event_count = 0
    missing_count = 0
    recoverable_missing_count = 0
    fallback_count = 0
    ok = True
    for report in lane_reports:
        source_ok = bool(report.get("ok", False))
        ok = ok and source_ok
        event_count += max(0, _safe_int(report.get("event_count", 0), 0))
        if bool(report.get("missing", False)):
            missing_count += 1
        if bool(report.get("recoverable_missing", False)):
            recoverable_missing_count += 1
        if bool(report.get("fallback_used", False)):
            fallback_count += 1
        message = str(report.get("error", "")).strip()
        if message:
            errors.append(message)

    state = "unreported"
    ok_value: bool | None = None
    if lane_reports:
        state = "ok" if ok else "degraded"
        ok_value = ok
    return {
        "lane": requested_lane,
        "state": state,
        "ok": ok_value,
        "reported_sources": len(lane_reports),
        "event_count": event_count,
        "missing_count": missing_count,
        "recoverable_missing_count": recoverable_missing_count,
        "fallback_count": fallback_count,
        "error_count": len(errors),
        "errors": errors,
        "global_partial": bool(payload.get("partial", False)),
        "global_error_count": len(payload.get("errors", [])) if isinstance(payload.get("errors", []), list) else 0,
    }


def _config_from_args(args: argparse.Namespace) -> ManagerConfig:
    root = Path(args.root).resolve()
    env_file = Path(args.env_file).resolve() if args.env_file else None
    return ManagerConfig.from_root(root, env_file_override=env_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orxaq autonomy manager")
    parser.add_argument("--root", default=".", help="orxaq-ops root directory")
    parser.add_argument("--env-file", default="", help="optional path to .env.autonomy")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("supervise")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("ensure")
    sub.add_parser("status")
    sub.add_parser("health")
    metrics = sub.add_parser("metrics")
    metrics.add_argument("--json", action="store_true")
    monitor = sub.add_parser("monitor")
    monitor.add_argument("--json", action="store_true")
    pre = sub.add_parser("preflight")
    pre.add_argument("--allow-dirty", action="store_true")
    sub.add_parser("reset")
    sub.add_parser("logs")
    sub.add_parser("install-keepalive")
    sub.add_parser("uninstall-keepalive")
    sub.add_parser("keepalive-status")

    init_skill = sub.add_parser("init-skill-protocol")
    init_skill.add_argument("--output", default="config/skill_protocol.json")

    workspace = sub.add_parser("workspace")
    workspace.add_argument("--output", default="orxaq-dual-agent.code-workspace")

    open_ide = sub.add_parser("open-ide")
    open_ide.add_argument("--ide", choices=["vscode", "cursor", "pycharm"], default="vscode")
    open_ide.add_argument("--workspace", default="orxaq-dual-agent.code-workspace")

    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--workspace", default="orxaq-dual-agent.code-workspace")
    bootstrap.add_argument("--ide", choices=["vscode", "cursor", "pycharm", "none"], default="vscode")
    bootstrap.add_argument("--require-clean", action="store_true")
    bootstrap.add_argument("--skip-keepalive", action="store_true")

    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--refresh-sec", type=int, default=5)
    dashboard.add_argument("--no-browser", action="store_true")
    dashboard.add_argument("--port-scan", type=int, default=20)

    dashboard_start = sub.add_parser("dashboard-start")
    dashboard_start.add_argument("--host", default="127.0.0.1")
    dashboard_start.add_argument("--port", type=int, default=8765)
    dashboard_start.add_argument("--refresh-sec", type=int, default=5)
    dashboard_start.add_argument("--no-browser", action="store_true")

    dashboard_ensure = sub.add_parser("dashboard-ensure")
    dashboard_ensure.add_argument("--host", default="127.0.0.1")
    dashboard_ensure.add_argument("--port", type=int, default=8765)
    dashboard_ensure.add_argument("--refresh-sec", type=int, default=5)
    dashboard_ensure.add_argument("--no-browser", action="store_true")

    sub.add_parser("dashboard-stop")
    sub.add_parser("dashboard-status")
    dashboard_logs = sub.add_parser("dashboard-logs")
    dashboard_logs.add_argument("--lines", type=int, default=120)

    conversations = sub.add_parser("conversations")
    conversations.add_argument("--lines", type=int, default=200)
    conversations.add_argument("--no-lanes", action="store_true")
    conversations.add_argument("--owner", default="")
    conversations.add_argument("--lane", default="")
    conversations.add_argument("--event-type", default="")
    conversations.add_argument("--contains", default="")
    conversations.add_argument("--tail", type=int, default=0)

    conversation_inspect = sub.add_parser("conversation-inspect")
    conversation_inspect.add_argument("--lines", type=int, default=200)
    conversation_inspect.add_argument("--no-lanes", action="store_true")
    conversation_inspect.add_argument("--owner", default="")
    conversation_inspect.add_argument("--lane", default="")
    conversation_inspect.add_argument("--event-type", default="")
    conversation_inspect.add_argument("--contains", default="")
    conversation_inspect.add_argument("--tail", type=int, default=0)

    lane_inspect = sub.add_parser("lane-inspect")
    lane_inspect.add_argument("--lane", required=True)
    lane_inspect.add_argument("--lines", type=int, default=200)
    lane_inspect.add_argument("--no-lanes", action="store_true")
    lane_inspect.add_argument("--owner", default="")
    lane_inspect.add_argument("--event-type", default="")
    lane_inspect.add_argument("--contains", default="")
    lane_inspect.add_argument("--tail", type=int, default=0)

    lanes_plan = sub.add_parser("lanes-plan")
    lanes_plan.add_argument("--json", action="store_true")

    lanes_status = sub.add_parser("lanes-status")
    lanes_status.add_argument("--json", action="store_true")
    lanes_status.add_argument("--lane", default="")

    lane_status = sub.add_parser("lane-status")
    lane_status.add_argument("--json", action="store_true")
    lane_status.add_argument("--lane", required=True)

    lanes_start = sub.add_parser("lanes-start")
    lanes_start.add_argument("--lane", default="")

    lane_start = sub.add_parser("lane-start")
    lane_start.add_argument("--lane", required=True)

    lanes_ensure = sub.add_parser("lanes-ensure")
    lanes_ensure.add_argument("--json", action="store_true")
    lanes_ensure.add_argument("--lane", default="")

    lane_ensure = sub.add_parser("lane-ensure")
    lane_ensure.add_argument("--json", action="store_true")
    lane_ensure.add_argument("--lane", required=True)

    lanes_stop = sub.add_parser("lanes-stop")
    lanes_stop.add_argument("--lane", default="")

    lane_stop = sub.add_parser("lane-stop")
    lane_stop.add_argument("--lane", required=True)

    args = parser.parse_args(argv)
    cfg = _config_from_args(args)

    try:
        if args.command == "run":
            return run_foreground(cfg)
        if args.command == "supervise":
            return supervise_foreground(cfg)
        if args.command == "start":
            start_background(cfg)
            return 0
        if args.command == "stop":
            stop_background(cfg)
            return 0
        if args.command == "ensure":
            ensure_background(cfg)
            return 0
        if args.command == "status":
            print(json.dumps(status_snapshot(cfg), indent=2, sort_keys=True))
            logs = tail_logs(cfg, latest_run_only=True)
            if logs:
                print("--- logs ---")
                print(logs)
            return 0
        if args.command == "health":
            print(json.dumps(health_snapshot(cfg), indent=2, sort_keys=True))
            return 0
        if args.command == "metrics":
            payload = monitor_snapshot(cfg).get("response_metrics", {})
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(
                json.dumps(
                    {
                        "responses_total": payload.get("responses_total", 0),
                        "quality_score_avg": payload.get("quality_score_avg", 0.0),
                        "first_time_pass_rate": payload.get("first_time_pass_rate", 0.0),
                        "acceptance_pass_rate": payload.get("acceptance_pass_rate", 0.0),
                        "latency_sec_avg": payload.get("latency_sec_avg", 0.0),
                        "prompt_difficulty_score_avg": payload.get("prompt_difficulty_score_avg", 0.0),
                        "cost_usd_total": payload.get("cost_usd_total", 0.0),
                        "cost_usd_avg": payload.get("cost_usd_avg", 0.0),
                        "exact_cost_coverage": payload.get("exact_cost_coverage", 0.0),
                        "tokens_total": payload.get("tokens_total", 0),
                        "token_rate_per_minute": payload.get("token_rate_per_minute", 0.0),
                        "exciting_stat": payload.get("exciting_stat", {}),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            recommendations = payload.get("optimization_recommendations", [])
            if isinstance(recommendations, list) and recommendations:
                print("recommendations:")
                for item in recommendations:
                    print(f"- {item}")
            return 0
        if args.command == "monitor":
            snapshot = monitor_snapshot(cfg)
            if args.json:
                print(json.dumps(snapshot, indent=2, sort_keys=True))
            else:
                print(render_monitor_text(snapshot))
            return 0
        if args.command == "preflight":
            payload = preflight(cfg, require_clean=not args.allow_dirty)
            print(json.dumps(payload, indent=2, sort_keys=True))
            if payload.get("clean", True):
                return 0
            return 1
        if args.command == "reset":
            reset_state(cfg)
            print(f"cleared state file: {cfg.state_file}")
            return 0
        if args.command == "logs":
            print(tail_logs(cfg, lines=200))
            return 0
        if args.command == "install-keepalive":
            label = install_keepalive(cfg)
            print(f"installed keepalive: {label}")
            return 0
        if args.command == "uninstall-keepalive":
            label = uninstall_keepalive(cfg)
            print(f"removed keepalive: {label}")
            return 0
        if args.command == "keepalive-status":
            print(json.dumps(keepalive_status(cfg), indent=2, sort_keys=True))
            return 0
        if args.command == "init-skill-protocol":
            out = (cfg.root_dir / args.output).resolve()
            write_default_skill_protocol(out)
            print(f"wrote skill protocol: {out}")
            return 0
        if args.command == "workspace":
            out = (cfg.root_dir / args.output).resolve()
            created = generate_workspace(cfg.root_dir, cfg.impl_repo, cfg.test_repo, out)
            print(f"workspace generated: {created}")
            return 0
        if args.command == "open-ide":
            ws = (cfg.root_dir / args.workspace).resolve()
            if not ws.exists() and args.ide in {"vscode", "cursor"}:
                generate_workspace(cfg.root_dir, cfg.impl_repo, cfg.test_repo, ws)
            print(open_in_ide(ide=args.ide, root=cfg.root_dir, workspace_file=ws))
            return 0
        if args.command == "bootstrap":
            ide = None if args.ide == "none" else args.ide
            payload = bootstrap_background(
                cfg,
                allow_dirty=not args.require_clean,
                install_keepalive_job=not args.skip_keepalive,
                ide=ide,
                workspace_filename=args.workspace,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload.get("ok") else 1
        if args.command == "dashboard":
            return start_dashboard(
                cfg,
                host=args.host,
                port=args.port,
                refresh_sec=args.refresh_sec,
                open_browser=not args.no_browser,
                port_scan=args.port_scan,
            )
        if args.command == "dashboard-start":
            snapshot = start_dashboard_background(
                cfg,
                host=args.host,
                port=args.port,
                refresh_sec=args.refresh_sec,
                open_browser=not args.no_browser,
            )
            print(json.dumps(snapshot, indent=2, sort_keys=True))
            return 0 if snapshot.get("running") else 1
        if args.command == "dashboard-ensure":
            snapshot = ensure_dashboard_background(
                cfg,
                host=args.host,
                port=args.port,
                refresh_sec=args.refresh_sec,
                open_browser=not args.no_browser,
            )
            print(json.dumps(snapshot, indent=2, sort_keys=True))
            return 0 if snapshot.get("running") else 1
        if args.command == "dashboard-stop":
            snapshot = stop_dashboard_background(cfg)
            print(json.dumps(snapshot, indent=2, sort_keys=True))
            return 0
        if args.command == "dashboard-status":
            print(json.dumps(dashboard_status_snapshot(cfg), indent=2, sort_keys=True))
            return 0
        if args.command == "dashboard-logs":
            print(tail_dashboard_logs(cfg, lines=args.lines))
            return 0
        if args.command in {"conversations", "conversation-inspect"}:
            payload = _safe_conversations_snapshot(
                cfg,
                lines=args.lines,
                include_lanes=not args.no_lanes,
                owner=args.owner,
                lane_id=args.lane,
                event_type=args.event_type,
                contains=args.contains,
                tail=args.tail,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if args.command == "lane-inspect":
            requested_lane = args.lane.strip()
            if not requested_lane:
                raise RuntimeError("lane id is required")
            lane_payload = _filter_lane_status_payload(
                _safe_lane_status_snapshot(cfg),
                requested_lane=requested_lane,
                lanes_file=cfg.lanes_file,
            )
            lane_items = lane_payload.get("lanes", [])
            if not isinstance(lane_items, list):
                lane_items = []
            selected = lane_items[:1]
            lane_errors = lane_payload.get("errors", [])
            if not isinstance(lane_errors, list):
                lane_errors = []
            if not selected and any(str(item).strip().startswith("Unknown lane id ") for item in lane_errors):
                raise RuntimeError(f"Unknown lane id {requested_lane!r}. Update {cfg.lanes_file}.")
            conv_payload = _safe_conversations_snapshot(
                cfg,
                lines=args.lines,
                include_lanes=not args.no_lanes,
                owner=args.owner,
                lane_id=requested_lane,
                event_type=args.event_type,
                contains=args.contains,
                tail=args.tail,
            )
            lane_entry: dict[str, Any]
            if selected:
                lane_entry = selected[0]
            else:
                lane_entry = {
                    "id": requested_lane,
                    "owner": "unknown",
                    "running": False,
                    "pid": None,
                    "health": "error",
                    "error": "lane status unavailable",
                }
            lane_payload_ok = bool(lane_payload.get("ok", not lane_errors))
            lane_health = str(lane_entry.get("health", "")).strip().lower()
            payload = {
                "requested_lane": requested_lane,
                "lane": lane_entry,
                "lane_errors": lane_errors,
                "suppressed_lane_errors": lane_payload.get("suppressed_errors", []),
                "lane_health_counts": lane_payload.get("health_counts", {}),
                "lane_owner_counts": lane_payload.get("owner_counts", {}),
                "conversations": conv_payload,
                "conversation_source_health": _lane_conversation_source_health(
                    conv_payload,
                    lane_id=requested_lane,
                ),
                "partial": (not lane_payload_ok) or bool(conv_payload.get("partial", False)) or bool(lane_errors),
                "ok": (
                    lane_payload_ok
                    and lane_health != "error"
                    and bool(conv_payload.get("ok", False))
                ),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if args.command == "lanes-plan":
            lanes = load_lane_specs(cfg)
            if args.json:
                print(json.dumps({"lanes_file": str(cfg.lanes_file), "lanes": lanes}, indent=2, sort_keys=True))
                return 0
            print(f"lanes_file: {cfg.lanes_file}")
            if not lanes:
                print("No lanes configured.")
                return 0
            for lane in lanes:
                status = "enabled" if lane["enabled"] else "disabled"
                print(f"- {lane['id']} ({lane['owner']}, {status})")
                print(f"  description: {lane['description']}")
                print(f"  impl_repo: {lane['impl_repo']}")
                print(f"  test_repo: {lane['test_repo']}")
                print(f"  tasks_file: {lane['tasks_file']}")
                print(f"  objective_file: {lane['objective_file']}")
                print(f"  exclusive_paths: {', '.join(lane['exclusive_paths']) if lane['exclusive_paths'] else '(none)'}")
            return 0
        if args.command in {"lanes-status", "lane-status"}:
            filtered = _filter_lane_status_payload(
                _safe_lane_status_snapshot(cfg),
                requested_lane=args.lane,
                lanes_file=cfg.lanes_file,
            )
            requested_lane = str(filtered.get("requested_lane", "")).strip()
            if requested_lane and requested_lane != "all":
                if not filtered.get("lanes") and any(
                    str(item).strip().startswith("Unknown lane id ")
                    for item in filtered.get("errors", [])
                ):
                    raise RuntimeError(f"Unknown lane id {requested_lane!r}. Update {cfg.lanes_file}.")
            if args.json:
                print(json.dumps(filtered, indent=2, sort_keys=True))
                return 0
            print(
                json.dumps(
                    {
                        "lanes_file": filtered["lanes_file"],
                        "requested_lane": filtered["requested_lane"],
                        "running_count": filtered["running_count"],
                        "total_count": filtered["total_count"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            if filtered["health_counts"]:
                health_pairs = ", ".join(
                    f"{key}={filtered['health_counts'][key]}"
                    for key in sorted(filtered["health_counts"])
                )
                print(f"health_counts: {health_pairs}")
            if filtered["owner_counts"]:
                owner_parts = []
                for owner in sorted(filtered["owner_counts"]):
                    stats = filtered["owner_counts"][owner]
                    owner_parts.append(
                        f"{owner}(total={stats.get('total', 0)},running={stats.get('running', 0)},"
                        f"healthy={stats.get('healthy', 0)},degraded={stats.get('degraded', 0)})"
                    )
                print(f"owner_counts: {' | '.join(owner_parts)}")
            if filtered.get("errors"):
                print(f"errors: {' | '.join(str(item) for item in filtered['errors'])}")
            for lane in filtered["lanes"]:
                state = "running" if lane["running"] else "stopped"
                print(
                    f"- {lane['id']} [{lane['owner']}] {state} pid={lane['pid']} "
                    f"health={lane.get('health', 'unknown')} heartbeat_age={lane.get('heartbeat_age_sec', -1)}s"
                )
                if lane.get("error"):
                    print(f"  error: {lane['error']}")
            return 0
        if args.command in {"lanes-start", "lane-start"}:
            lane_id = args.lane.strip() or None
            payload = start_lanes_background(cfg, lane_id=lane_id)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload.get("ok", False) else 1
        if args.command in {"lanes-ensure", "lane-ensure"}:
            lane_id = args.lane.strip() or None
            payload = ensure_lanes_background(cfg, lane_id=lane_id)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    json.dumps(
                        {
                            "ensured_count": payload["ensured_count"],
                            "started_count": payload["started_count"],
                            "restarted_count": payload["restarted_count"],
                            "failed_count": payload["failed_count"],
                            "ok": payload["ok"],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            return 0 if payload.get("ok", False) else 1
        if args.command in {"lanes-stop", "lane-stop"}:
            lane_id = args.lane.strip() or None
            payload = stop_lanes_background(cfg, lane_id=lane_id)
            print(json.dumps(payload, indent=2, sort_keys=True))
            stop_ok = bool(payload.get("ok", int(payload.get("failed_count", 0)) == 0))
            return 0 if stop_ok else 1
    except (FileNotFoundError, RuntimeError) as err:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(err),
                    "command": args.command,
                    "root": str(cfg.root_dir),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
