"""Replay-safe DAG state helpers for coordinator scheduling foundations."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any


VALID_NODE_STATES = {"pending", "ready", "running", "success", "failed", "blocked"}


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except Exception:
        return default


@dataclass(frozen=True)
class DagNode:
    node_id: str
    dependencies: tuple[str, ...]


def validate_dag(nodes: dict[str, DagNode]) -> list[str]:
    errors: list[str] = []
    for node_id, node in nodes.items():
        for dep_id in node.dependencies:
            if dep_id not in nodes:
                errors.append(f"node={node_id} has unknown dependency={dep_id}")
    return errors


def frontier_ready_nodes(nodes: dict[str, DagNode], state: dict[str, dict[str, Any]]) -> list[str]:
    ready: list[str] = []
    for node_id, node in nodes.items():
        node_state = str(state.get(node_id, {}).get("state", "pending")).strip().lower() or "pending"
        if node_state not in {"pending", "ready"}:
            continue
        dependencies_met = True
        for dep_id in node.dependencies:
            dep_state = str(state.get(dep_id, {}).get("state", "pending")).strip().lower() or "pending"
            if dep_state != "success":
                dependencies_met = False
                break
        if dependencies_met:
            ready.append(node_id)
    return sorted(ready)


def replay_safe_claim(
    *,
    dag_state: dict[str, dict[str, Any]],
    node_id: str,
    task_id: str,
    attempt: int,
    leader_epoch: int,
) -> dict[str, Any]:
    entry = dag_state.get(node_id, {})
    previous_key = str(entry.get("claim_key", "")).strip()
    claim_key = f"{task_id}:{max(1, _int_value(attempt, 1))}:{max(0, _int_value(leader_epoch, 0))}"
    if previous_key and previous_key == claim_key and str(entry.get("state", "")).strip().lower() == "running":
        return {"ok": True, "state": "running", "claim_key": claim_key, "deduped": True}
    next_entry = {
        **entry,
        "state": "running",
        "task_id": task_id,
        "attempt": max(1, _int_value(attempt, 1)),
        "leader_epoch": max(0, _int_value(leader_epoch, 0)),
        "claim_key": claim_key,
        "updated_at": _now_iso(),
    }
    dag_state[node_id] = next_entry
    return {"ok": True, "state": "running", "claim_key": claim_key, "deduped": False}


def transition_node_state(
    *,
    dag_state: dict[str, dict[str, Any]],
    node_id: str,
    next_state: str,
    reason: str = "",
) -> dict[str, Any]:
    normalized = str(next_state).strip().lower()
    if normalized not in VALID_NODE_STATES:
        return {"ok": False, "error": "invalid_state", "state": normalized}
    entry = dag_state.get(node_id, {})
    dag_state[node_id] = {
        **entry,
        "state": normalized,
        "reason": str(reason).strip(),
        "updated_at": _now_iso(),
    }
    return {"ok": True, "state": normalized}
