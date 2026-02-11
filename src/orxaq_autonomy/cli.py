"""CLI entrypoint for reusable Orxaq autonomy package."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .breakglass import (
    close_session as breakglass_close_session,
    open_session as breakglass_open_session,
    status as breakglass_status_snapshot,
)
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
    requested_lane_normalized = requested_lane.lower()
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
        if source_lane and source_lane.lower() != requested_lane_normalized:
            suppressed_sources.append(source)
            continue
        retained_sources.append(source)

    # When a lane-specific source is healthy, treat a failed primary/global
    # source as non-blocking for lane-filtered inspection.
    lane_source_healthy = any(
        str(source.get("lane_id", "")).strip().lower() == requested_lane_normalized
        and bool(source.get("ok", False))
        for source in retained_sources
    )
    if lane_source_healthy:
        lane_scoped_sources: list[dict[str, Any]] = []
        for source in retained_sources:
            source_lane = str(source.get("lane_id", "")).strip()
            if source_lane and source_lane.lower() == requested_lane_normalized:
                lane_scoped_sources.append(source)
                continue
            source_kind = str(source.get("resolved_kind", source.get("kind", ""))).strip().lower()
            if bool(source.get("ok", False)) or source_kind != "primary":
                lane_scoped_sources.append(source)
                continue
            suppressed_sources.append(source)
        retained_sources = lane_scoped_sources

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
        source_kind = str(source.get("resolved_kind", source.get("kind", ""))).strip().lower()
        if source_kind == "primary":
            if any(token in normalized for token in ("primary", "global", "conversation source", "conversations.ndjson")):
                return True
        return False

    suppressed_payload_errors: list[str] = []
    payload_errors = _normalize_error_messages(payload.get("errors", []))
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


def _normalize_error_messages(raw: Any) -> list[str]:
    if isinstance(raw, list):
        values = raw
    elif raw in (None, "", []):
        values = []
    else:
        values = [raw]
    normalized: list[str] = []
    for item in values:
        message = str(item).strip()
        if message:
            normalized.append(message)
    return normalized


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


def _normalize_lane_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    lane = dict(raw)
    lane["id"] = str(lane.get("id", "")).strip() or "unknown"
    lane["owner"] = str(lane.get("owner", "")).strip() or "unknown"
    lane["running"] = bool(lane.get("running", False))
    lane["pid"] = lane.get("pid")
    lane["health"] = str(lane.get("health", "")).strip().lower() or "unknown"
    lane["error"] = str(lane.get("error", "")).strip()
    state_counts = lane.get("state_counts", {})
    lane["state_counts"] = state_counts if isinstance(state_counts, dict) else {}
    try:
        lane["heartbeat_age_sec"] = int(lane.get("heartbeat_age_sec", -1))
    except (TypeError, ValueError):
        lane["heartbeat_age_sec"] = -1
    lane["build_current"] = bool(lane.get("build_current", False))
    return lane


def _resolve_lane_filter(lane_items: list[dict[str, Any]], requested_lane: str) -> str:
    lane_filter = requested_lane.strip()
    if not lane_filter:
        return ""
    known_ids = sorted(
        {
            str(item.get("id", "")).strip()
            for item in lane_items
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
    )
    if lane_filter in known_ids:
        return lane_filter
    folded_matches = [lane_id for lane_id in known_ids if lane_id.lower() == lane_filter.lower()]
    if len(folded_matches) == 1:
        return folded_matches[0]
    return lane_filter


def _filter_lane_status_payload(
    payload: dict[str, Any],
    *,
    requested_lane: str = "",
    lanes_file: Path,
) -> dict[str, Any]:
    lane_filter_raw = requested_lane.strip()
    raw_lane_items = payload.get("lanes", [])
    if not isinstance(raw_lane_items, list):
        raw_lane_items = []
    lane_items = [item for item in (_normalize_lane_entry(raw) for raw in raw_lane_items) if item is not None]
    lane_filter = _resolve_lane_filter(lane_items, lane_filter_raw)
    lane_filter_normalized = lane_filter.lower()
    known_lane_ids = {
        str(item.get("id", "")).strip().lower()
        for item in lane_items
        if str(item.get("id", "")).strip()
    }
    normalized_errors = _normalize_error_messages(payload.get("errors", []))
    suppressed_errors: list[str] = []

    if lane_filter:
        lane_items = [lane for lane in lane_items if str(lane.get("id", "")).strip().lower() == lane_filter_normalized]
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


def _resolve_lane_key(keys: list[str], requested_lane: str) -> str:
    lane_filter = requested_lane.strip()
    if not lane_filter:
        return ""
    if lane_filter in keys:
        return lane_filter
    folded_matches = [lane_id for lane_id in keys if lane_id.lower() == lane_filter.lower()]
    if len(folded_matches) == 1:
        return folded_matches[0]
    return lane_filter


def _resolve_rollup_lane_key(rollup: dict[str, dict[str, Any]], requested_lane: str) -> str:
    keys = [str(lane_id).strip() for lane_id in rollup if str(lane_id).strip()]
    return _resolve_lane_key(keys, requested_lane)


def _lane_conversation_source_health(payload: dict[str, Any], *, lane_id: str) -> dict[str, Any]:
    requested_lane = lane_id.strip()
    resolved_lane = requested_lane
    requested_lane_folded = requested_lane.lower()
    reports = payload.get("sources", [])
    if not isinstance(reports, list):
        reports = []
    lane_reports: list[dict[str, Any]] = []
    for item in reports:
        if not isinstance(item, dict):
            continue
        source_lane = str(item.get("lane_id", "")).strip()
        if source_lane.lower() != requested_lane_folded:
            continue
        lane_reports.append(item)
        if source_lane and resolved_lane.lower() == requested_lane_folded:
            resolved_lane = source_lane
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
        "lane": resolved_lane or requested_lane,
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


def _parse_event_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_local_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def _lane_conversation_rollup(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    reports = payload.get("sources", [])
    if not isinstance(reports, list):
        reports = []
    events = payload.get("events", [])
    if not isinstance(events, list):
        events = []

    by_lane_raw: dict[str, dict[str, Any]] = {}

    def _entry(lane_id: str) -> dict[str, Any]:
        return by_lane_raw.setdefault(
            lane_id,
            {
                "lane_id": lane_id,
                "owner_hints": {},
                "source_count": 0,
                "source_ok": None,
                "source_event_count": 0,
                "source_error_count": 0,
                "missing_count": 0,
                "recoverable_missing_count": 0,
                "fallback_count": 0,
                "observed_event_count": 0,
                "latest_event": {},
            },
        )

    for source in reports:
        if not isinstance(source, dict):
            continue
        lane_id = str(source.get("lane_id", "")).strip()
        if not lane_id:
            continue
        current = _entry(lane_id)
        source_owner = str(source.get("owner", "")).strip() or "unknown"
        if source_owner != "unknown":
            owner_hints = current.get("owner_hints")
            if not isinstance(owner_hints, dict):
                owner_hints = {}
                current["owner_hints"] = owner_hints
            owner_hints[source_owner] = _safe_int(owner_hints.get(source_owner, 0), 0) + 1
        current["source_count"] += 1
        source_ok = bool(source.get("ok", False))
        if current["source_ok"] is None:
            current["source_ok"] = source_ok
        else:
            current["source_ok"] = bool(current["source_ok"]) and source_ok
        current["source_event_count"] += max(0, _safe_int(source.get("event_count", 0), 0))
        if str(source.get("error", "")).strip():
            current["source_error_count"] += 1
        if bool(source.get("missing", False)):
            current["missing_count"] += 1
        if bool(source.get("recoverable_missing", False)):
            current["recoverable_missing_count"] += 1
        if bool(source.get("fallback_used", False)):
            current["fallback_count"] += 1

    for event in events:
        if not isinstance(event, dict):
            continue
        lane_id = str(event.get("lane_id", "")).strip()
        if not lane_id:
            continue
        current = _entry(lane_id)
        current["observed_event_count"] += 1
        event_owner = str(event.get("owner", "")).strip() or "unknown"
        if event_owner != "unknown":
            owner_hints = current.get("owner_hints")
            if not isinstance(owner_hints, dict):
                owner_hints = {}
                current["owner_hints"] = owner_hints
            owner_hints[event_owner] = _safe_int(owner_hints.get(event_owner, 0), 0) + 1
        candidate = {
            "timestamp": str(event.get("timestamp", "")).strip(),
            "owner": event_owner,
            "lane_id": lane_id,
            "task_id": str(event.get("task_id", "")).strip(),
            "event_type": str(event.get("event_type", "")).strip(),
            "content": str(event.get("content", "")).strip(),
            "source": str(event.get("source", "")).strip(),
            "source_kind": str(event.get("source_kind", "")).strip(),
        }
        existing = current.get("latest_event", {})
        existing_ts = _parse_event_timestamp(existing.get("timestamp")) if isinstance(existing, dict) else None
        candidate_ts = _parse_event_timestamp(candidate.get("timestamp"))
        if existing_ts is None and candidate_ts is None:
            # Preserve event sequence when timestamps are malformed.
            should_replace = True
        elif existing_ts is None:
            should_replace = True
        elif candidate_ts is None:
            should_replace = False
        else:
            should_replace = candidate_ts >= existing_ts
        if should_replace:
            current["latest_event"] = candidate

    rollup: dict[str, dict[str, Any]] = {}
    for lane_id, item in by_lane_raw.items():
        source_ok = item.get("source_ok")
        source_state = "unreported"
        if source_ok is True:
            source_state = "ok"
        elif source_ok is False:
            source_state = "error"
        latest_event = item.get("latest_event", {}) if isinstance(item.get("latest_event", {}), dict) else {}
        owner = str(latest_event.get("owner", "")).strip() or "unknown"
        owner_hints = item.get("owner_hints", {})
        if owner == "unknown" and isinstance(owner_hints, dict):
            ordered_hints = sorted(
                (
                    (str(name).strip(), _safe_int(count, 0))
                    for name, count in owner_hints.items()
                    if str(name).strip() and str(name).strip() != "unknown"
                ),
                key=lambda pair: (-pair[1], pair[0]),
            )
            if ordered_hints:
                owner = ordered_hints[0][0]
        rollup[lane_id] = {
            "lane_id": lane_id,
            "owner": owner,
            "event_count": max(
                _safe_int(item.get("source_event_count", 0), 0),
                _safe_int(item.get("observed_event_count", 0), 0),
            ),
            "source_count": _safe_int(item.get("source_count", 0), 0),
            "source_ok": source_ok,
            "source_state": source_state,
            "source_error_count": _safe_int(item.get("source_error_count", 0), 0),
            "missing_count": _safe_int(item.get("missing_count", 0), 0),
            "recoverable_missing_count": _safe_int(item.get("recoverable_missing_count", 0), 0),
            "fallback_count": _safe_int(item.get("fallback_count", 0), 0),
            "latest_event": latest_event,
        }
    return rollup


def _lane_error_mentions_unknown_lane(error: str, lane_id: str) -> bool:
    message = str(error).strip().lower()
    lane = lane_id.strip().lower()
    if not message or not lane:
        return False
    if not message.startswith("unknown lane id "):
        return False
    return lane in message


def _lane_error_mentions_unavailable_lane(error: str, lane_id: str) -> bool:
    message = str(error).strip().lower()
    lane = lane_id.strip().lower()
    if not message or not lane:
        return False
    if not message.startswith("requested lane "):
        return False
    if "is unavailable because lane status sources failed." not in message:
        return False
    return lane in message


def _lane_owner_health_counts(lanes: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, dict[str, int]], int]:
    health_counts: dict[str, int] = {}
    owner_counts: dict[str, dict[str, int]] = {}
    running_count = 0
    healthy_states = {"ok", "paused", "idle"}
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        health = str(lane.get("health", "unknown")).strip().lower() or "unknown"
        health_counts[health] = health_counts.get(health, 0) + 1
        owner = str(lane.get("owner", "unknown")).strip() or "unknown"
        owner_entry = owner_counts.setdefault(owner, {"total": 0, "running": 0, "healthy": 0, "degraded": 0})
        owner_entry["total"] += 1
        if bool(lane.get("running", False)):
            running_count += 1
            owner_entry["running"] += 1
        if health in healthy_states:
            owner_entry["healthy"] += 1
        else:
            owner_entry["degraded"] += 1
    return health_counts, owner_counts, running_count


def _augment_lane_status_with_conversations(
    lane_payload: dict[str, Any],
    conversation_payload: dict[str, Any],
) -> dict[str, Any]:
    lane_items = lane_payload.get("lanes", [])
    if not isinstance(lane_items, list):
        lane_items = []
    rollup = _lane_conversation_rollup(conversation_payload)

    requested_lane_raw = str(lane_payload.get("requested_lane", "all")).strip() or "all"
    requested_lane = requested_lane_raw
    if requested_lane != "all":
        known_lane_ids = sorted(set(rollup) | {str(item.get("id", "")).strip() for item in lane_items if isinstance(item, dict)})
        folded_matches = [lane_id for lane_id in known_lane_ids if lane_id and lane_id.lower() == requested_lane.lower()]
        if len(folded_matches) == 1:
            requested_lane = folded_matches[0]
    enriched_lanes: list[dict[str, Any]] = []
    seen_lanes: set[str] = set()
    for lane in lane_items:
        if not isinstance(lane, dict):
            continue
        lane_id = str(lane.get("id", "")).strip()
        if lane_id:
            seen_lanes.add(lane_id)
        lane_rollup = rollup.get(lane_id, {})
        lane_copy = dict(lane)
        rollup_owner = str(lane_rollup.get("owner", "")).strip() or "unknown"
        if (str(lane_copy.get("owner", "")).strip() or "unknown") == "unknown" and rollup_owner != "unknown":
            lane_copy["owner"] = rollup_owner
        lane_copy["conversation_event_count"] = int(lane_rollup.get("event_count", 0))
        lane_copy["conversation_source_count"] = int(lane_rollup.get("source_count", 0))
        lane_copy["conversation_source_state"] = str(lane_rollup.get("source_state", "unreported"))
        source_ok = lane_rollup.get("source_ok")
        lane_copy["conversation_source_ok"] = source_ok if isinstance(source_ok, bool) else None
        lane_copy["conversation_source_error_count"] = int(lane_rollup.get("source_error_count", 0))
        lane_copy["conversation_source_missing_count"] = int(lane_rollup.get("missing_count", 0))
        lane_copy["conversation_source_recoverable_missing_count"] = int(
            lane_rollup.get("recoverable_missing_count", 0)
        )
        lane_copy["conversation_source_fallback_count"] = int(lane_rollup.get("fallback_count", 0))
        lane_copy["latest_conversation_event"] = (
            lane_rollup.get("latest_event", {})
            if isinstance(lane_rollup.get("latest_event", {}), dict)
            else {}
        )
        enriched_lanes.append(lane_copy)

    recovered_lanes: list[str] = []
    synthesize_lane_ids: list[str] = []
    if requested_lane != "all":
        if requested_lane in rollup and requested_lane not in seen_lanes:
            synthesize_lane_ids.append(requested_lane)
    else:
        lane_source_degraded = (
            not bool(lane_payload.get("ok", True))
            or bool(lane_payload.get("partial", False))
            or bool(lane_payload.get("errors", []))
        )
        if lane_source_degraded:
            synthesize_lane_ids.extend(sorted(lane_id for lane_id in rollup if lane_id not in seen_lanes))

    for lane_id in synthesize_lane_ids:
        lane_rollup = rollup.get(lane_id, {})
        latest_event = lane_rollup.get("latest_event", {})
        if not isinstance(latest_event, dict):
            latest_event = {}
        owner = str(latest_event.get("owner", "")).strip() or ""
        if not owner or owner == "unknown":
            owner = str(lane_rollup.get("owner", "unknown")).strip() or "unknown"
        lane_copy = {
            "id": lane_id,
            "owner": owner,
            "running": False,
            "pid": None,
            "health": "unknown",
            "heartbeat_age_sec": -1,
            "state_counts": {},
            "build_current": False,
            "error": "lane status missing; derived from conversation logs",
            "conversation_lane_fallback": True,
            "conversation_event_count": int(lane_rollup.get("event_count", 0)),
            "conversation_source_count": int(lane_rollup.get("source_count", 0)),
            "conversation_source_state": str(lane_rollup.get("source_state", "unreported")),
            "conversation_source_ok": (
                lane_rollup.get("source_ok")
                if isinstance(lane_rollup.get("source_ok"), bool)
                else None
            ),
            "conversation_source_error_count": int(lane_rollup.get("source_error_count", 0)),
            "conversation_source_missing_count": int(lane_rollup.get("missing_count", 0)),
            "conversation_source_recoverable_missing_count": int(
                lane_rollup.get("recoverable_missing_count", 0)
            ),
            "conversation_source_fallback_count": int(lane_rollup.get("fallback_count", 0)),
            "latest_conversation_event": latest_event,
        }
        enriched_lanes.append(lane_copy)
        recovered_lanes.append(lane_id)

    conversation_errors = conversation_payload.get("errors", [])
    if not isinstance(conversation_errors, list):
        conversation_errors = [str(conversation_errors)] if str(conversation_errors).strip() else []
    normalized_errors = [str(item).strip() for item in conversation_errors if str(item).strip()]
    lane_errors = lane_payload.get("errors", [])
    if not isinstance(lane_errors, list):
        lane_errors = [str(lane_errors)] if str(lane_errors).strip() else []
    combined_lane_errors = [str(item).strip() for item in lane_errors if str(item).strip()]
    if recovered_lanes:
        combined_lane_errors = [
            message
            for message in combined_lane_errors
            if not any(
                _lane_error_mentions_unknown_lane(message, lane_id)
                or _lane_error_mentions_unavailable_lane(message, lane_id)
                for lane_id in recovered_lanes
            )
        ]
        for lane_id in recovered_lanes:
            warning = f"Lane status missing for {lane_id!r}; using conversation-derived fallback."
            if warning not in combined_lane_errors:
                combined_lane_errors.append(warning)

    health_counts, owner_counts, running_count = _lane_owner_health_counts(enriched_lanes)
    lane_partial = bool(lane_payload.get("partial", False))
    conversation_partial = bool(conversation_payload.get("partial", False))
    partial = lane_partial or conversation_partial or bool(combined_lane_errors) or bool(recovered_lanes)
    ok = bool(lane_payload.get("ok", not combined_lane_errors)) and not partial

    out = dict(lane_payload)
    out["lanes"] = enriched_lanes
    out["running_count"] = running_count
    out["total_count"] = len(enriched_lanes)
    out["health_counts"] = health_counts
    out["owner_counts"] = owner_counts
    out["errors"] = combined_lane_errors
    out["partial"] = partial
    out["ok"] = ok
    out["recovered_lanes"] = recovered_lanes
    out["recovered_lane_count"] = len(recovered_lanes)
    out["conversation_by_lane"] = rollup
    out["conversation_partial"] = bool(conversation_payload.get("partial", False))
    out["conversation_ok"] = bool(conversation_payload.get("ok", False))
    out["conversation_errors"] = normalized_errors
    out["requested_lane"] = requested_lane if requested_lane != "all" else "all"
    return out


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
    breakglass_open = sub.add_parser("breakglass-open")
    breakglass_open.add_argument("--scope", action="append", default=[])
    breakglass_open.add_argument("--reason", required=True)
    breakglass_open.add_argument("--ttl-sec", type=int, default=1800)
    breakglass_open.add_argument("--actor", default="")
    breakglass_open.add_argument("--token", default="")
    breakglass_close = sub.add_parser("breakglass-close")
    breakglass_close.add_argument("--reason", default="")
    breakglass_close.add_argument("--actor", default="")
    breakglass_close.add_argument("--token", default="")
    breakglass_close.add_argument("--require-token", action="store_true")
    sub.add_parser("breakglass-status")

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
    lanes_status.add_argument("--with-conversations", action="store_true")
    lanes_status.add_argument("--conversation-lines", type=int, default=200)

    lane_status = sub.add_parser("lane-status")
    lane_status.add_argument("--json", action="store_true")
    lane_status.add_argument("--lane", required=True)
    lane_status.add_argument("--with-conversations", action="store_true")
    lane_status.add_argument("--conversation-lines", type=int, default=200)

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
        if args.command == "breakglass-open":
            payload = breakglass_open_session(
                cfg.root_dir,
                scopes=args.scope,
                reason=args.reason,
                ttl_sec=args.ttl_sec,
                actor=args.actor,
                token=args.token,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload.get("ok", False) else 1
        if args.command == "breakglass-close":
            payload = breakglass_close_session(
                cfg.root_dir,
                actor=args.actor,
                reason=args.reason,
                token=args.token,
                require_token=args.require_token,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload.get("ok", False) else 1
        if args.command == "breakglass-status":
            print(json.dumps(breakglass_status_snapshot(cfg.root_dir), indent=2, sort_keys=True))
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
            resolved_lane = str(lane_payload.get("requested_lane", "")).strip() or requested_lane
            lane_items = lane_payload.get("lanes", [])
            if not isinstance(lane_items, list):
                lane_items = []
            selected = lane_items[:1]
            lane_errors = lane_payload.get("errors", [])
            if not isinstance(lane_errors, list):
                lane_errors = []
            conv_payload = _safe_conversations_snapshot(
                cfg,
                lines=args.lines,
                include_lanes=not args.no_lanes,
                owner=args.owner,
                lane_id=resolved_lane,
                event_type=args.event_type,
                contains=args.contains,
                tail=args.tail,
            )
            lane_conv_rollup = _lane_conversation_rollup(conv_payload)
            resolved_rollup_lane = _resolve_rollup_lane_key(lane_conv_rollup, resolved_lane)
            lane_conv_fallback = lane_conv_rollup.get(resolved_rollup_lane, {})
            if lane_conv_fallback:
                resolved_lane = resolved_rollup_lane
            lane_signal_available = bool(lane_conv_fallback)
            if not selected and any(str(item).strip().startswith("Unknown lane id ") for item in lane_errors):
                if not lane_signal_available:
                    raise RuntimeError(f"Unknown lane id {requested_lane!r}. Update {cfg.lanes_file}.")
            lane_entry: dict[str, Any]
            if selected:
                lane_entry = selected[0]
            else:
                latest_event = lane_conv_fallback.get("latest_event", {})
                if not isinstance(latest_event, dict):
                    latest_event = {}
                fallback_owner = str(latest_event.get("owner", "")).strip()
                if not fallback_owner or fallback_owner.lower() == "unknown":
                    fallback_owner = str(lane_conv_fallback.get("owner", "")).strip() or "unknown"
                lane_entry = {
                    "id": resolved_lane,
                    "owner": fallback_owner,
                    "running": False,
                    "pid": None,
                    "health": "unknown" if lane_signal_available else "error",
                    "error": (
                        "lane status missing; derived from conversation logs"
                        if lane_signal_available
                        else "lane status unavailable"
                    ),
                    "conversation_lane_fallback": lane_signal_available,
                    "conversation_event_count": int(lane_conv_fallback.get("event_count", 0)),
                    "conversation_source_count": int(lane_conv_fallback.get("source_count", 0)),
                    "conversation_source_state": str(lane_conv_fallback.get("source_state", "unreported")),
                    "conversation_source_ok": (
                        lane_conv_fallback.get("source_ok")
                        if isinstance(lane_conv_fallback.get("source_ok"), bool)
                        else None
                    ),
                    "conversation_source_error_count": int(lane_conv_fallback.get("source_error_count", 0)),
                    "conversation_source_missing_count": int(lane_conv_fallback.get("missing_count", 0)),
                    "conversation_source_recoverable_missing_count": int(
                        lane_conv_fallback.get("recoverable_missing_count", 0)
                    ),
                    "conversation_source_fallback_count": int(lane_conv_fallback.get("fallback_count", 0)),
                    "latest_conversation_event": latest_event,
                }
            lane_entry = dict(lane_entry)
            latest_event = lane_conv_fallback.get("latest_event", {})
            if not isinstance(latest_event, dict):
                latest_event = {}
            rollup_owner = str(lane_conv_fallback.get("owner", "")).strip() or "unknown"
            current_owner = str(lane_entry.get("owner", "")).strip() or "unknown"
            if current_owner == "unknown" and rollup_owner != "unknown":
                lane_entry["owner"] = rollup_owner
            if latest_event:
                lane_entry["latest_conversation_event"] = latest_event
            elif not isinstance(lane_entry.get("latest_conversation_event"), dict):
                lane_entry["latest_conversation_event"] = {}
            lane_entry["conversation_event_count"] = _safe_int(
                lane_conv_fallback.get("event_count", lane_entry.get("conversation_event_count", 0)),
                0,
            )
            lane_entry["conversation_source_count"] = _safe_int(
                lane_conv_fallback.get("source_count", lane_entry.get("conversation_source_count", 0)),
                0,
            )
            lane_entry["conversation_source_state"] = (
                str(
                    lane_conv_fallback.get(
                        "source_state",
                        lane_entry.get("conversation_source_state", "unreported"),
                    )
                ).strip()
                or "unreported"
            )
            source_ok = lane_conv_fallback.get("source_ok")
            if isinstance(source_ok, bool):
                lane_entry["conversation_source_ok"] = source_ok
            elif "conversation_source_ok" not in lane_entry:
                lane_entry["conversation_source_ok"] = None
            lane_entry["conversation_source_error_count"] = _safe_int(
                lane_conv_fallback.get("source_error_count", lane_entry.get("conversation_source_error_count", 0)),
                0,
            )
            lane_entry["conversation_source_missing_count"] = _safe_int(
                lane_conv_fallback.get("missing_count", lane_entry.get("conversation_source_missing_count", 0)),
                0,
            )
            lane_entry["conversation_source_recoverable_missing_count"] = _safe_int(
                lane_conv_fallback.get(
                    "recoverable_missing_count",
                    lane_entry.get("conversation_source_recoverable_missing_count", 0),
                ),
                0,
            )
            lane_entry["conversation_source_fallback_count"] = _safe_int(
                lane_conv_fallback.get("fallback_count", lane_entry.get("conversation_source_fallback_count", 0)),
                0,
            )
            lane_entry["conversation_lane_fallback"] = bool(
                lane_entry.get("conversation_lane_fallback", not selected and lane_signal_available)
            )
            lane_payload_ok = bool(lane_payload.get("ok", not lane_errors))
            lane_health = str(lane_entry.get("health", "")).strip().lower()
            conversation_source_health = _lane_conversation_source_health(
                conv_payload,
                lane_id=resolved_lane,
            )
            payload = {
                "requested_lane": resolved_lane,
                "input_lane": requested_lane,
                "lane": lane_entry,
                "lane_errors": lane_errors,
                "suppressed_lane_errors": lane_payload.get("suppressed_errors", []),
                "lane_health_counts": lane_payload.get("health_counts", {}),
                "lane_owner_counts": lane_payload.get("owner_counts", {}),
                "conversations": conv_payload,
                "conversation_source_health": conversation_source_health,
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
            if getattr(args, "with_conversations", False):
                conversation_lines = max(1, min(2000, _safe_int(getattr(args, "conversation_lines", 200), 200)))
                conversation_payload = _safe_conversations_snapshot(
                    cfg,
                    lines=conversation_lines,
                    include_lanes=True,
                    lane_id=args.lane,
                )
                filtered = _augment_lane_status_with_conversations(filtered, conversation_payload)
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
            scaling_counts = (
                filtered.get("scaling_event_counts", {})
                if isinstance(filtered.get("scaling_event_counts", {}), dict)
                else {}
            )
            if scaling_counts:
                print(
                    "scaling_events: "
                    f"up={_safe_int(scaling_counts.get('scale_up', 0), 0)} "
                    f"down={_safe_int(scaling_counts.get('scale_down', 0), 0)} "
                    f"hold={_safe_int(scaling_counts.get('scale_hold', 0), 0)}"
                )
            parallel_capacity = (
                filtered.get("parallel_capacity", {})
                if isinstance(filtered.get("parallel_capacity", {}), dict)
                else {}
            )
            parallel_groups = (
                parallel_capacity.get("groups", [])
                if isinstance(parallel_capacity.get("groups", []), list)
                else []
            )
            normalized_parallel_groups = [item for item in parallel_groups if isinstance(item, dict)]
            if normalized_parallel_groups:
                group_parts = []
                for item in normalized_parallel_groups:
                    group_parts.append(
                        f"{item.get('provider', 'unknown')}:{item.get('model', 'default')}"
                        f"={_safe_int(item.get('running_count', 0), 0)}/"
                        f"{_safe_int(item.get('effective_limit', 1), 1)}"
                    )
                print(f"parallel_capacity: {' | '.join(group_parts)}")
            if filtered.get("errors"):
                print(f"errors: {' | '.join(str(item) for item in filtered['errors'])}")
            if filtered.get("conversation_errors"):
                print(f"conversation_errors: {' | '.join(str(item) for item in filtered['conversation_errors'])}")
            if getattr(args, "with_conversations", False):
                print(
                    "conversation_recovery: "
                    f"recovered_lanes={_safe_int(filtered.get('recovered_lane_count', 0), 0)} "
                    f"conversation_partial={bool(filtered.get('conversation_partial', False))} "
                    f"conversation_ok={bool(filtered.get('conversation_ok', False))}"
                )
            for lane in filtered["lanes"]:
                state = "running" if lane["running"] else "stopped"
                print(
                    f"- {lane['id']} [{lane['owner']}] {state} pid={lane['pid']} "
                    f"health={lane.get('health', 'unknown')} heartbeat_age={lane.get('heartbeat_age_sec', -1)}s"
                )
                if getattr(args, "with_conversations", False):
                    latest = lane.get("latest_conversation_event", {})
                    if not isinstance(latest, dict):
                        latest = {}
                    print(
                        "  conversations: "
                        f"state={lane.get('conversation_source_state', 'unreported')} "
                        f"events={lane.get('conversation_event_count', 0)} "
                        f"latest_ts={_format_local_timestamp(latest.get('timestamp', ''))} "
                        f"latest_owner={latest.get('owner', '') or '-'} "
                        f"latest_type={latest.get('event_type', '') or '-'}"
                    )
                if lane.get("error"):
                    print(f"  error: {lane['error']}")
                if str(lane.get("scaling_mode", "static")).strip().lower() == "npv" or any(
                    _safe_int(lane.get(key, 0), 0) > 0
                    for key in ("scale_up_events", "scale_down_events", "scale_hold_events")
                ):
                    latest_scale = lane.get("latest_scale_event", {})
                    if not isinstance(latest_scale, dict):
                        latest_scale = {}
                    print(
                        "  scaling: "
                        f"mode={str(lane.get('scaling_mode', 'static')).strip() or 'static'} "
                        f"group={str(lane.get('scaling_group', '')).strip() or '-'} "
                        f"rank={_safe_int(lane.get('scaling_rank', 1), 1)} "
                        f"up={_safe_int(lane.get('scale_up_events', 0), 0)} "
                        f"down={_safe_int(lane.get('scale_down_events', 0), 0)} "
                        f"hold={_safe_int(lane.get('scale_hold_events', 0), 0)} "
                        f"latest_type={str(latest_scale.get('event_type', '')).strip() or '-'}"
                    )
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
    except (FileNotFoundError, RuntimeError, ValueError) as err:
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
