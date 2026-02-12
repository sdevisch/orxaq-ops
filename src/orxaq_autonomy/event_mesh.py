"""Event-driven autonomy mesh with GitHub-ledger coordination primitives."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _normalize_node_id(value: str) -> str:
    raw = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip().lower())
    normalized = "-".join(part for part in raw.replace("_", "-").split("-") if part)
    return normalized or "node-unknown"


def _default_node_id() -> str:
    host = socket.gethostname().strip() or "host"
    return _normalize_node_id(host)


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _event_fingerprint(
    *,
    node_id: str,
    topic: str,
    event_type: str,
    payload: dict[str, Any],
    event_ts: str,
    causation_id: str = "",
) -> str:
    canonical = json.dumps(
        {
            "node_id": node_id,
            "topic": topic,
            "event_type": event_type,
            "payload": payload,
            "event_ts": event_ts,
            "causation_id": causation_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"evt_{digest[:20]}"


def _command_fingerprint(
    *,
    action: str,
    requested_lane: str,
    target_lane: str,
    target_delta: int,
    reason: str,
    origin_event_id: str,
) -> str:
    canonical = json.dumps(
        {
            "action": action,
            "requested_lane": requested_lane,
            "target_lane": target_lane,
            "target_delta": int(target_delta),
            "reason": reason,
            "origin_event_id": origin_event_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"cmd_{digest[:20]}"


@dataclass(frozen=True)
class EventMeshConfig:
    root_dir: Path
    node_id: str
    events_file: Path
    state_dir: Path
    dispatch_cursor_file: Path
    dispatch_seen_file: Path
    export_seen_file: Path
    coordination_dir: Path
    coordination_outbox_dir: Path
    coordination_nodes_dir: Path
    coordination_leases_dir: Path

    @classmethod
    def from_root(cls, root: Path) -> "EventMeshConfig":
        root = root.resolve()
        env = os.environ
        node_id_raw = str(env.get("ORXAQ_AUTONOMY_NODE_ID", "")).strip()
        node_id = _normalize_node_id(node_id_raw) if node_id_raw else _default_node_id()
        events_file = Path(
            env.get(
                "ORXAQ_AUTONOMY_EVENT_MESH_FILE",
                str(root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"),
            )
        ).resolve()
        state_dir = Path(
            env.get(
                "ORXAQ_AUTONOMY_EVENT_MESH_STATE_DIR",
                str(root / "state" / "event_mesh"),
            )
        ).resolve()
        coordination_dir = Path(
            env.get(
                "ORXAQ_AUTONOMY_GITHUB_COORDINATION_DIR",
                str(root / "state" / "github_coordination" / "event_mesh"),
            )
        ).resolve()
        return cls(
            root_dir=root,
            node_id=node_id,
            events_file=events_file,
            state_dir=state_dir,
            dispatch_cursor_file=state_dir / "dispatch_cursor.json",
            dispatch_seen_file=state_dir / "dispatch_seen.json",
            export_seen_file=state_dir / "export_seen.json",
            coordination_dir=coordination_dir,
            coordination_outbox_dir=coordination_dir / "outbox",
            coordination_nodes_dir=coordination_dir / "nodes",
            coordination_leases_dir=coordination_dir / "leases",
        )


def ensure_event_mesh_layout(config: EventMeshConfig) -> None:
    config.events_file.parent.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.coordination_outbox_dir.mkdir(parents=True, exist_ok=True)
    config.coordination_nodes_dir.mkdir(parents=True, exist_ok=True)
    config.coordination_leases_dir.mkdir(parents=True, exist_ok=True)


def _append_event_line(events_file: Path, event: dict[str, Any]) -> None:
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def read_event_log(events_file: Path) -> list[dict[str, Any]]:
    if not events_file.exists():
        return []
    results: list[dict[str, Any]] = []
    for line in events_file.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            results.append(payload)
    return results


def publish_event(
    config: EventMeshConfig,
    *,
    topic: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    causation_id: str = "",
    source: str = "runtime",
) -> dict[str, Any]:
    ensure_event_mesh_layout(config)
    topic_value = str(topic).strip().lower() or "misc"
    event_type_value = str(event_type).strip().lower() or "generic.event"
    event_payload = payload if isinstance(payload, dict) else {}
    event_ts = _now_iso()
    event_id = _event_fingerprint(
        node_id=config.node_id,
        topic=topic_value,
        event_type=event_type_value,
        payload=event_payload,
        event_ts=event_ts,
        causation_id=causation_id,
    )
    event = {
        "event_id": event_id,
        "timestamp": event_ts,
        "topic": topic_value,
        "event_type": event_type_value,
        "node_id": config.node_id,
        "source": source,
        "causation_id": str(causation_id).strip(),
        "payload": event_payload,
    }
    _append_event_line(config.events_file, event)
    return event


def _load_seen_ids(path: Path) -> set[str]:
    raw = _read_json_file(path, {"event_ids": []})
    values = raw.get("event_ids", []) if isinstance(raw, dict) else []
    out: set[str] = set()
    if isinstance(values, list):
        for item in values:
            text = str(item).strip()
            if text:
                out.add(text)
    return out


def _save_seen_ids(path: Path, event_ids: set[str]) -> None:
    _write_json_atomic(path, {"event_ids": sorted(event_ids), "updated_at": _now_iso()})


def _load_dispatch_cursor(path: Path) -> int:
    raw = _read_json_file(path, {"offset": 0})
    if not isinstance(raw, dict):
        return 0
    value = raw.get("offset", 0)
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _save_dispatch_cursor(path: Path, offset: int) -> None:
    _write_json_atomic(path, {"offset": max(0, int(offset)), "updated_at": _now_iso()})


def _route_selection(payload: dict[str, Any]) -> dict[str, Any]:
    options = payload.get("options", [])
    if not isinstance(options, list):
        options = []
    best: dict[str, Any] | None = None
    for candidate in options:
        if not isinstance(candidate, dict):
            continue
        healthy = bool(candidate.get("healthy", True))
        if not healthy:
            continue
        score_raw = candidate.get("score", 0.0)
        try:
            score = float(score_raw)
        except Exception:
            score = 0.0
        if best is None or score > float(best.get("score", 0.0)):
            best = {
                "lane_id": str(candidate.get("lane_id", "")).strip(),
                "provider": str(candidate.get("provider", "")).strip(),
                "model": str(candidate.get("model", "")).strip(),
                "score": score,
            }
    if best is None:
        return {"selected": False, "reason": "no_healthy_option"}
    return {"selected": True, "selection": best}


def _int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _scaling_decision(payload: dict[str, Any]) -> dict[str, Any]:
    failed_count = max(0, _int_value(payload.get("failed_count", 0), 0))
    started_count = max(0, _int_value(payload.get("started_count", 0), 0))
    restarted_count = max(0, _int_value(payload.get("restarted_count", 0), 0))
    scaled_up_count = max(0, _int_value(payload.get("scaled_up_count", 0), 0))
    scaled_down_count = max(0, _int_value(payload.get("scaled_down_count", 0), 0))
    groups_at_limit = max(0, _int_value(payload.get("parallel_groups_at_limit", 0), 0))

    facts = {
        "failed_count": failed_count,
        "parallel_groups_at_limit": groups_at_limit,
        "started_count": started_count,
        "restarted_count": restarted_count,
        "scaled_up_count": scaled_up_count,
        "scaled_down_count": scaled_down_count,
    }
    decision = {"action": "hold", "reason": "stable", "target_delta": 0, "decision_trace": {}}
    try:
        from .dmn_engine import evaluate_scaling_decision, load_scaling_decision_table

        table = load_scaling_decision_table(Path(str(payload.get("root_dir", "."))))
        decision = evaluate_scaling_decision(facts=facts, table=table)
    except Exception:
        pass
    requested_lane = str(payload.get("requested_lane", "all_enabled")).strip() or "all_enabled"
    return {
        "action": str(decision.get("action", "hold")).strip() or "hold",
        "reason": str(decision.get("reason", "stable")).strip() or "stable",
        "target_delta": _int_value(decision.get("target_delta", 0), 0),
        "decision_trace": decision.get("decision_trace", {}),
        "failed_count": failed_count,
        "parallel_groups_at_limit": groups_at_limit,
        "started_count": started_count,
        "restarted_count": restarted_count,
        "scaled_up_count": scaled_up_count,
        "scaled_down_count": scaled_down_count,
        "requested_lane": requested_lane,
        "leader_epoch": max(0, _int_value(payload.get("leader_epoch", 0), 0)),
        "decision_table_version": str(
            payload.get("decision_table_version", decision.get("decision_trace", {}).get("decision_table_version", "scaling_v1"))
        ).strip()
        or "scaling_v1",
        "execution_dag_id": str(payload.get("execution_dag_id", requested_lane)).strip() or requested_lane,
        "causal_hypothesis_id": str(payload.get("causal_hypothesis_id", "")).strip(),
    }


def _event_followups(event: dict[str, Any], *, config: EventMeshConfig | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    topic = str(event.get("topic", "")).strip().lower()
    event_type = str(event.get("event_type", "")).strip().lower()
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    leader_epoch = max(0, _int_value(payload.get("leader_epoch", 0), 0))
    leader_id = str(payload.get("leader_id", "")).strip()
    decision_table_version = str(payload.get("decision_table_version", "scaling_v1")).strip() or "scaling_v1"
    causal_hypothesis_id = str(payload.get("causal_hypothesis_id", "")).strip()
    local_is_leader = True
    if config is not None:
        try:
            from .leader_lease import LeaderLeaseConfig, acquire_or_renew_lease

            lease = acquire_or_renew_lease(LeaderLeaseConfig.from_root(config.root_dir))
            local_is_leader = bool(lease.get("is_leader", False))
            if leader_epoch <= 0:
                leader_epoch = max(0, _int_value(lease.get("epoch", 0), 0))
            if not leader_id:
                leader_id = str(lease.get("leader_id", "")).strip()
        except Exception:
            local_is_leader = True

    if topic == "monitoring" and event_type == "heartbeat.changed":
        return [
            (
                "scheduling",
                "tick.requested",
                {"reason": "heartbeat_changed", "origin_event_id": event.get("event_id", "")},
            )
        ]
    if topic == "scheduling" and event_type == "task.enqueued":
        task_id = str(payload.get("task_id", "")).strip()
        if not task_id:
            return []
        return [
            (
                "scheduling",
                "task.scheduled",
                {"task_id": task_id, "state": "ready"},
            ),
            (
                "routing",
                "route.requested",
                {
                    "task_id": task_id,
                    "options": payload.get("routing_options", []),
                },
            ),
        ]
    if topic == "routing" and event_type == "route.requested":
        result = _route_selection(payload)
        event_name = "route.selected" if result.get("selected") else "route.blocked"
        return [("routing", event_name, result)]
    if topic == "scheduling" and event_type in {"lanes.ensure.summary", "lanes.start.summary"}:
        requested_lane = str(payload.get("requested_lane", "all_enabled")).strip() or "all_enabled"
        return [
            (
                "scaling",
                "decision.requested",
                {
                    "requested_lane": requested_lane,
                    "started_count": _int_value(payload.get("started_count", 0), 0),
                    "restarted_count": _int_value(payload.get("restarted_count", 0), 0),
                    "scaled_up_count": _int_value(payload.get("scaled_up_count", 0), 0),
                    "scaled_down_count": _int_value(payload.get("scaled_down_count", 0), 0),
                    "failed_count": _int_value(payload.get("failed_count", 0), 0),
                    "parallel_groups_at_limit": _int_value(payload.get("parallel_groups_at_limit", 0), 0),
                    "origin_event_id": event.get("event_id", ""),
                    "leader_epoch": leader_epoch,
                    "leader_id": leader_id,
                    "decision_table_version": decision_table_version,
                    "execution_dag_id": str(payload.get("execution_dag_id", requested_lane)).strip() or requested_lane,
                    "causal_hypothesis_id": causal_hypothesis_id,
                },
            )
        ]
    if topic == "monitoring" and event_type in {"lane.ensure_failed", "lane.start_failed"}:
        requested_lane = str(payload.get("lane_id", "all_enabled")).strip() or "all_enabled"
        return [
            (
                "scaling",
                "decision.requested",
                {
                    "requested_lane": requested_lane,
                    "failed_count": 1,
                    "started_count": 0,
                    "restarted_count": 0,
                    "scaled_up_count": 0,
                    "scaled_down_count": 0,
                    "parallel_groups_at_limit": 0,
                    "origin_event_id": event.get("event_id", ""),
                    "leader_epoch": leader_epoch,
                    "leader_id": leader_id,
                    "decision_table_version": decision_table_version,
                    "execution_dag_id": str(payload.get("execution_dag_id", requested_lane)).strip() or requested_lane,
                    "causal_hypothesis_id": causal_hypothesis_id,
                },
            )
        ]
    if topic == "scaling" and event_type == "decision.requested":
        decision = _scaling_decision({**payload, "root_dir": str(config.root_dir) if config is not None else "."})
        return [("scaling", "decision.made", decision)]
    if topic == "scaling" and event_type == "decision.made":
        if config is not None and not local_is_leader:
            return []
        action = str(payload.get("action", "")).strip().lower()
        if action not in {"scale_up", "scale_down"}:
            return []
        requested_lane = str(payload.get("requested_lane", "all_enabled")).strip() or "all_enabled"
        target_lane = requested_lane if requested_lane not in {"all", "all_enabled"} else ""
        target_delta = _int_value(payload.get("target_delta", 0), 0)
        reason = str(payload.get("reason", "decision")).strip() or "decision"
        origin_event_id = str(event.get("event_id", "")).strip()
        command_id = str(payload.get("command_id", "")).strip() or _command_fingerprint(
            action=action,
            requested_lane=requested_lane,
            target_lane=target_lane,
            target_delta=target_delta,
            reason=reason,
            origin_event_id=origin_event_id,
        )
        decision_table_version = str(payload.get("decision_table_version", "scaling_v1")).strip() or "scaling_v1"
        execution_dag_id = str(payload.get("execution_dag_id", requested_lane)).strip() or requested_lane
        causal_hypothesis_id = str(payload.get("causal_hypothesis_id", "")).strip()
        leader_epoch = max(0, _int_value(payload.get("leader_epoch", 0), 0))
        causal_gate = {
            "required": False,
            "mode": "advisory",
            "allowed": True,
            "status": "not_required",
            "evidence_summary": "not_evaluated",
        }
        try:
            from .causal_decision_bridge import enforce_causal_metadata_gate

            causal_gate = enforce_causal_metadata_gate(
                action=action,
                requested_lane=requested_lane,
                causal_hypothesis_id=causal_hypothesis_id,
            )
        except Exception:
            pass
        if not bool(causal_gate.get("allowed", True)):
            return []
        return [
            (
                "scaling",
                "command.requested",
                {
                    "command_id": command_id,
                    "action": action,
                    "requested_lane": requested_lane,
                    "target_lane": target_lane,
                    "reason": reason,
                    "target_delta": target_delta,
                    "leader_epoch": leader_epoch,
                    "leader_id": leader_id,
                    "decision_table_version": decision_table_version,
                    "execution_dag_id": execution_dag_id,
                    "causal_hypothesis_id": causal_hypothesis_id,
                    "causal_gate": causal_gate,
                    "decision_trace": payload.get("decision_trace", {}),
                    "issued_at_utc": _now_iso(),
                    "origin_decision_event_id": origin_event_id,
                },
            )
        ]
    return []


def dispatch_events(config: EventMeshConfig, *, max_events: int = 128) -> dict[str, Any]:
    ensure_event_mesh_layout(config)
    events = read_event_log(config.events_file)
    cursor = _load_dispatch_cursor(config.dispatch_cursor_file)
    seen = _load_seen_ids(config.dispatch_seen_file)
    followups_emitted = 0
    processed = 0
    limit = max(1, int(max_events))

    index = cursor
    while index < len(events) and processed < limit:
        event = events[index]
        event_id = str(event.get("event_id", "")).strip()
        if event_id and event_id in seen:
            index += 1
            continue
        followups = _event_followups(event, config=config)
        for topic, event_type, payload in followups:
            publish_event(
                config,
                topic=topic,
                event_type=event_type,
                payload=payload,
                causation_id=event_id,
                source="mesh-dispatch",
            )
            followups_emitted += 1
        if event_id:
            seen.add(event_id)
        index += 1
        processed += 1
        events = read_event_log(config.events_file)

    _save_dispatch_cursor(config.dispatch_cursor_file, index)
    _save_seen_ids(config.dispatch_seen_file, seen)
    return {
        "ok": True,
        "node_id": config.node_id,
        "processed_events": processed,
        "followup_events": followups_emitted,
        "cursor_offset": index,
        "event_count": len(events),
    }


def export_events_for_coordination(config: EventMeshConfig, *, max_events: int = 512) -> dict[str, Any]:
    ensure_event_mesh_layout(config)
    events = read_event_log(config.events_file)
    exported_ids = _load_seen_ids(config.export_seen_file)
    exported = 0
    skipped = 0
    limit = max(1, int(max_events))
    node_outbox = config.coordination_outbox_dir / config.node_id
    node_outbox.mkdir(parents=True, exist_ok=True)

    for event in events:
        event_id = str(event.get("event_id", "")).strip()
        if not event_id:
            skipped += 1
            continue
        if event_id in exported_ids:
            continue
        target = node_outbox / f"{event_id}.json"
        _write_json_atomic(target, event)
        exported_ids.add(event_id)
        exported += 1
        if exported >= limit:
            break

    _save_seen_ids(config.export_seen_file, exported_ids)
    return {
        "ok": True,
        "node_id": config.node_id,
        "exported_events": exported,
        "skipped_events": skipped,
        "coordination_outbox": str(node_outbox),
    }


def import_events_from_coordination(config: EventMeshConfig, *, max_events: int = 1024) -> dict[str, Any]:
    ensure_event_mesh_layout(config)
    existing = read_event_log(config.events_file)
    known_ids = {
        str(event.get("event_id", "")).strip()
        for event in existing
        if isinstance(event, dict) and str(event.get("event_id", "")).strip()
    }
    imported = 0
    scanned = 0
    limit = max(1, int(max_events))
    for node_dir in sorted(config.coordination_outbox_dir.glob("*")):
        if not node_dir.is_dir():
            continue
        for event_file in sorted(node_dir.glob("*.json")):
            scanned += 1
            if imported >= limit:
                break
            payload = _read_json_file(event_file, {})
            if not isinstance(payload, dict):
                continue
            event_id = str(payload.get("event_id", "")).strip()
            if not event_id or event_id in known_ids:
                continue
            _append_event_line(config.events_file, payload)
            known_ids.add(event_id)
            imported += 1
        if imported >= limit:
            break
    return {
        "ok": True,
        "node_id": config.node_id,
        "imported_events": imported,
        "scanned_files": scanned,
        "event_count": len(read_event_log(config.events_file)),
    }


def write_node_manifest(config: EventMeshConfig, *, capabilities: list[str] | None = None) -> dict[str, Any]:
    ensure_event_mesh_layout(config)
    payload = {
        "node_id": config.node_id,
        "updated_at": _now_iso(),
        "capabilities": capabilities or ["monitoring", "scheduling", "routing", "coordination"],
        "event_log": str(config.events_file),
        "coordination_outbox": str(config.coordination_outbox_dir / config.node_id),
    }
    target = config.coordination_nodes_dir / f"{config.node_id}.json"
    _write_json_atomic(target, payload)
    return payload


def event_mesh_status(config: EventMeshConfig) -> dict[str, Any]:
    ensure_event_mesh_layout(config)
    events = read_event_log(config.events_file)
    dispatch_cursor = _load_dispatch_cursor(config.dispatch_cursor_file)
    outbox_events = sum(1 for _ in config.coordination_outbox_dir.glob("*/*.json"))
    lease_state: dict[str, Any] = {}
    try:
        from .leader_lease import LeaderLeaseConfig, read_lease_snapshot

        lease_state = read_lease_snapshot(LeaderLeaseConfig.from_root(config.root_dir))
    except Exception:
        lease_state = {}
    command_outcomes: dict[str, int] = {}
    latest_command: dict[str, Any] = {}
    latest_leader_epoch = -1
    command_log_file = config.root_dir / "artifacts" / "autonomy" / "event_mesh" / "commands.ndjson"
    if command_log_file.exists():
        for line in command_log_file.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            outcome = str(item.get("outcome", "")).strip()
            if not outcome:
                continue
            command_outcomes[outcome] = command_outcomes.get(outcome, 0) + 1
            row_epoch = max(-1, _int_value(item.get("leader_epoch", -1), -1))
            if row_epoch > latest_leader_epoch:
                latest_leader_epoch = row_epoch
            latest_command = {
                "timestamp": str(item.get("timestamp", "")).strip(),
                "command_id": str(item.get("command_id", "")).strip(),
                "action": str(item.get("action", "")).strip(),
                "lane_id": str(item.get("lane_id", "")).strip(),
                "outcome": outcome,
                "reason": str(item.get("reason", "")).strip(),
                "leader_epoch": row_epoch,
            }
    return {
        "ok": True,
        "node_id": config.node_id,
        "event_count": len(events),
        "dispatch_cursor_offset": dispatch_cursor,
        "coordination_outbox_files": outbox_events,
        "events_file": str(config.events_file),
        "coordination_dir": str(config.coordination_dir),
        "leader_lease": lease_state,
        "command_outcomes": command_outcomes,
        "latest_leader_epoch": latest_leader_epoch,
        "latest_command": latest_command,
    }
