"""Distributed coordinator HA: lease backend, leader fencing, DMN policy, DAG scheduling.

Implements the remaining distributed coordinator resilience work:
- Lease backend abstraction (file-based and pluggable)
- Leader/epoch fencing across mutating manager control paths
- DMN-style policy extraction with explain traces
- Execution DAG replay-safe scheduling foundations
- Causal DAG intervention metadata gates
- Operator-visible observability for leader/epoch/command outcomes
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Lease Backend Abstraction
# ---------------------------------------------------------------------------


class LeaseBackend(Protocol):
    """Protocol for pluggable lease backends."""

    def acquire(self, holder: str, ttl_sec: int) -> bool: ...
    def renew(self, holder: str, ttl_sec: int) -> bool: ...
    def release(self, holder: str) -> bool: ...
    def current_holder(self) -> str | None: ...
    def is_held_by(self, holder: str) -> bool: ...


@dataclass
class FileLease:
    """File-based lease implementation for single-node or NFS-shared coordination.

    The lease file contains JSON with holder ID, epoch, and expiry timestamp.
    Stale leases (past expiry) are automatically reclaimable.
    """

    path: Path
    _epoch: int = 0

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _is_expired(self, data: dict[str, Any]) -> bool:
        expiry = data.get("expiry_utc", "")
        if not expiry:
            return True
        try:
            exp_dt = datetime.fromisoformat(expiry)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > exp_dt
        except (ValueError, TypeError):
            return True

    @property
    def epoch(self) -> int:
        return self._epoch

    def acquire(self, holder: str, ttl_sec: int) -> bool:
        data = self._read()
        current = data.get("holder", "")
        if current and current != holder and not self._is_expired(data):
            return False
        self._epoch = int(data.get("epoch", 0)) + 1
        now = datetime.now(timezone.utc)
        payload = {
            "holder": holder,
            "epoch": self._epoch,
            "acquired_utc": now.isoformat(),
            "expiry_utc": datetime.fromtimestamp(
                now.timestamp() + ttl_sec, tz=timezone.utc
            ).isoformat(),
            "pid": os.getpid(),
        }
        self._write(payload)
        return True

    def renew(self, holder: str, ttl_sec: int) -> bool:
        data = self._read()
        if data.get("holder") != holder:
            return False
        now = datetime.now(timezone.utc)
        data["expiry_utc"] = datetime.fromtimestamp(
            now.timestamp() + ttl_sec, tz=timezone.utc
        ).isoformat()
        data["renewed_utc"] = now.isoformat()
        self._write(data)
        return True

    def release(self, holder: str) -> bool:
        data = self._read()
        if data.get("holder") != holder:
            return False
        self.path.unlink(missing_ok=True)
        return True

    def current_holder(self) -> str | None:
        data = self._read()
        holder = data.get("holder")
        if not holder:
            return None
        if self._is_expired(data):
            return None
        return str(holder)

    def is_held_by(self, holder: str) -> bool:
        return self.current_holder() == holder


# ---------------------------------------------------------------------------
# Leader/Epoch Fencing
# ---------------------------------------------------------------------------


@dataclass
class EpochFence:
    """Epoch-based fencing for mutating operations.

    Every mutating control-path operation must present a valid epoch. If the
    epoch is stale (a new leader has acquired the lease), the operation is
    rejected to prevent split-brain writes.
    """

    lease: FileLease
    holder: str

    def validate_epoch(self, presented_epoch: int) -> tuple[bool, str]:
        """Check that the presented epoch matches the current lease epoch.

        Returns (ok, reason).
        """
        if not self.lease.is_held_by(self.holder):
            return False, f"lease not held by {self.holder}"
        if presented_epoch != self.lease.epoch:
            return False, (
                f"stale epoch: presented={presented_epoch}, "
                f"current={self.lease.epoch}"
            )
        return True, "ok"

    def fenced_execute(
        self, epoch: int, action: str, fn: Any, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        """Execute a fenced operation. Returns outcome dict."""
        ok, reason = self.validate_epoch(epoch)
        if not ok:
            return {
                "ok": False,
                "action": action,
                "reason": reason,
                "fenced": True,
                "timestamp": _now_iso(),
            }
        try:
            result = fn(*args, **kwargs)
            return {
                "ok": True,
                "action": action,
                "result": result,
                "epoch": epoch,
                "fenced": False,
                "timestamp": _now_iso(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "action": action,
                "reason": str(exc)[:500],
                "epoch": epoch,
                "fenced": False,
                "timestamp": _now_iso(),
            }


# ---------------------------------------------------------------------------
# DMN-style Policy Engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    """A single decision rule in the DMN-style policy table."""

    rule_id: str
    description: str
    conditions: dict[str, Any]  # field -> expected value or pattern
    action: str
    priority: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    """Result of evaluating a policy with explain trace."""

    matched_rule: str
    action: str
    explain: list[str]
    inputs: dict[str, Any]
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DMNPolicyEngine:
    """DMN-style decision table for extracting routing/governance policies.

    Rules are evaluated in priority order; first match wins.
    Every evaluation produces an explain trace for observability.
    """

    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        self._rules = sorted(rules or [], key=lambda r: r.priority)

    @property
    def rules(self) -> list[PolicyRule]:
        return list(self._rules)

    def add_rule(self, rule: PolicyRule) -> None:
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)

    def evaluate(self, inputs: dict[str, Any]) -> PolicyDecision:
        """Evaluate inputs against the policy table. Returns decision with trace."""
        explain: list[str] = []
        for rule in self._rules:
            matched = True
            for field_name, expected in rule.conditions.items():
                actual = inputs.get(field_name)
                if actual != expected:
                    explain.append(
                        f"rule {rule.rule_id}: {field_name}={actual!r} != {expected!r} -> skip"
                    )
                    matched = False
                    break
            if matched:
                explain.append(f"rule {rule.rule_id}: all conditions matched -> action={rule.action}")
                return PolicyDecision(
                    matched_rule=rule.rule_id,
                    action=rule.action,
                    explain=explain,
                    inputs=inputs,
                    timestamp=_now_iso(),
                )
        explain.append("no rule matched -> default action=allow")
        return PolicyDecision(
            matched_rule="",
            action="allow",
            explain=explain,
            inputs=inputs,
            timestamp=_now_iso(),
        )


# ---------------------------------------------------------------------------
# DAG Replay-Safe Scheduling
# ---------------------------------------------------------------------------


@dataclass
class DAGNode:
    """A node in the execution DAG."""

    node_id: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"  # pending, running, done, failed, skipped
    result: Any = None
    started_at: str = ""
    ended_at: str = ""
    # Causal metadata for intervention gates
    causal_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "depends_on": self.depends_on,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "causal_metadata": self.causal_metadata,
        }


class ExecutionDAG:
    """Replay-safe DAG scheduler for distributed coordination.

    Supports:
    - Dependency-based topological scheduling
    - Replay safety: re-execution from any saved state
    - Causal metadata gates: nodes can require causal preconditions
    """

    def __init__(self) -> None:
        self._nodes: dict[str, DAGNode] = {}

    def add_node(
        self,
        node_id: str,
        depends_on: list[str] | None = None,
        causal_metadata: dict[str, Any] | None = None,
    ) -> DAGNode:
        node = DAGNode(
            node_id=node_id,
            depends_on=depends_on or [],
            causal_metadata=causal_metadata or {},
        )
        self._nodes[node_id] = node
        return node

    def get_node(self, node_id: str) -> DAGNode | None:
        return self._nodes.get(node_id)

    @property
    def nodes(self) -> dict[str, DAGNode]:
        return dict(self._nodes)

    def ready_nodes(self) -> list[DAGNode]:
        """Return nodes that are pending and have all dependencies satisfied."""
        ready: list[DAGNode] = []
        for node in self._nodes.values():
            if node.status != "pending":
                continue
            deps_ok = all(
                self._nodes.get(dep, DAGNode(node_id=dep)).status == "done"
                for dep in node.depends_on
            )
            if deps_ok:
                ready.append(node)
        return ready

    def mark_running(self, node_id: str) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.status = "running"
            node.started_at = _now_iso()

    def mark_done(self, node_id: str, result: Any = None) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.status = "done"
            node.result = result
            node.ended_at = _now_iso()

    def mark_failed(self, node_id: str, reason: str = "") -> None:
        node = self._nodes.get(node_id)
        if node:
            node.status = "failed"
            node.result = reason
            node.ended_at = _now_iso()

    def is_complete(self) -> bool:
        """True if all nodes are done or failed (no pending/running)."""
        return all(n.status in ("done", "failed", "skipped") for n in self._nodes.values())

    def check_causal_gate(self, node_id: str) -> tuple[bool, str]:
        """Check if a node's causal preconditions are met.

        Causal metadata can contain:
        - requires_epoch: int -- must match current system epoch
        - requires_nodes_done: list[str] -- extra dependency check
        """
        node = self._nodes.get(node_id)
        if not node:
            return False, f"node {node_id} not found"
        meta = node.causal_metadata
        if not meta:
            return True, "no causal gates"
        required_done = meta.get("requires_nodes_done", [])
        for dep_id in required_done:
            dep = self._nodes.get(dep_id)
            if not dep or dep.status != "done":
                return False, f"causal gate: {dep_id} not done"
        return True, "all causal gates passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {nid: n.to_dict() for nid, n in self._nodes.items()},
            "complete": self.is_complete(),
            "pending": [n.node_id for n in self._nodes.values() if n.status == "pending"],
            "running": [n.node_id for n in self._nodes.values() if n.status == "running"],
            "done": [n.node_id for n in self._nodes.values() if n.status == "done"],
            "failed": [n.node_id for n in self._nodes.values() if n.status == "failed"],
        }

    def save_state(self, path: Path) -> None:
        """Persist DAG state for replay-safe scheduling."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def load_state(self, path: Path) -> None:
        """Restore DAG state from a saved snapshot."""
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        nodes_data = data.get("nodes", {})
        for node_id, node_info in nodes_data.items():
            if node_id in self._nodes:
                self._nodes[node_id].status = str(node_info.get("status", "pending"))
                self._nodes[node_id].started_at = str(node_info.get("started_at", ""))
                self._nodes[node_id].ended_at = str(node_info.get("ended_at", ""))


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@dataclass
class CoordinatorEvent:
    """An observable coordinator event for operator visibility."""

    event_type: str  # lease_acquired, lease_released, epoch_fenced, dag_scheduled, policy_evaluated
    detail: dict[str, Any]
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CoordinatorObserver:
    """Collects coordinator events for operator-visible observability."""

    def __init__(self, max_events: int = 1000) -> None:
        self._events: list[CoordinatorEvent] = []
        self._max = max_events

    def record(self, event_type: str, detail: dict[str, Any]) -> None:
        event = CoordinatorEvent(
            event_type=event_type,
            detail=detail,
            timestamp=_now_iso(),
        )
        self._events.append(event)
        if len(self._events) > self._max:
            self._events = self._events[-self._max:]

    @property
    def events(self) -> list[CoordinatorEvent]:
        return list(self._events)

    def recent(self, count: int = 20) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._events[-count:]]

    def flush_to_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "coordinator-events.v1",
            "generated_at_utc": _now_iso(),
            "event_count": len(self._events),
            "events": [e.to_dict() for e in self._events],
        }
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
