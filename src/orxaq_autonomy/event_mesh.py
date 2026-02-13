"""Event-driven autonomy mesh with decentralized nodes and GitHub coordination.

Replaces polling-based supervisor loops with an event-driven architecture where
each node can run independently.  GitHub serves as a shared coordination ledger
via structured JSON files committed to the repository.

Zero external dependencies -- uses only Python stdlib.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Event primitives
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    """Well-known mesh event types."""
    HEARTBEAT = "heartbeat"
    TASK_CLAIMED = "task_claimed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    NODE_JOINED = "node_joined"
    NODE_LEFT = "node_left"
    HEALTH_CHECK = "health_check"
    COORDINATION_SYNC = "coordination_sync"
    REMEDIATION_REQUESTED = "remediation_requested"
    LANE_STALLED = "lane_stalled"


@dataclass(frozen=True)
class MeshEvent:
    """An immutable event emitted by a mesh node."""
    event_type: str
    source_node: str
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            # Deterministic ID from content for deduplication
            raw = f"{self.event_type}:{self.source_node}:{self.timestamp}:{json.dumps(self.payload, sort_keys=True)}"
            eid = hashlib.sha256(raw.encode()).hexdigest()[:16]
            object.__setattr__(self, "event_id", eid)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MeshNode:
    """A decentralized autonomy node that can emit and handle events."""
    node_id: str
    capabilities: list[str] = field(default_factory=list)
    status: str = "idle"
    last_heartbeat: str = ""
    joined_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_stale(self, stale_threshold_sec: int = 300) -> bool:
        """Check if this node's heartbeat is stale."""
        if not self.last_heartbeat:
            return True
        try:
            parsed = datetime.fromisoformat(self.last_heartbeat)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - parsed).total_seconds()
            return age > stale_threshold_sec
        except (ValueError, TypeError):
            return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Event handlers (subscriber pattern)
# ---------------------------------------------------------------------------

EventHandler = Callable[[MeshEvent], None]


class EventBus:
    """In-process pub/sub event bus for mesh events.

    Nodes register handlers for specific event types.  Events are dispatched
    synchronously to keep the implementation simple and dependency-free.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._history: list[MeshEvent] = []
        self._max_history: int = 500

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def publish(self, event: MeshEvent) -> None:
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        for handler in self._handlers.get(event.event_type, []):
            try:
                handler(event)
            except Exception:
                pass  # Fault-tolerant dispatch

    @property
    def history(self) -> list[MeshEvent]:
        return list(self._history)


# ---------------------------------------------------------------------------
# GitHub coordination ledger
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    """A single entry in the GitHub coordination ledger."""
    entry_id: str
    node_id: str
    action: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GitHubLedger:
    """Shared coordination ledger backed by a JSON file in the repository.

    Uses file-based append operations.  In a real deployment, each write
    would be committed and pushed to the shared repository so all nodes
    converge on the same state.
    """

    def __init__(self, ledger_path: Path, *, max_entries: int = 200) -> None:
        self._path = ledger_path
        self._max_entries = max_entries

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> list[LedgerEntry]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        entries: list[LedgerEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            entries.append(LedgerEntry(
                entry_id=str(item.get("entry_id", "")),
                node_id=str(item.get("node_id", "")),
                action=str(item.get("action", "")),
                timestamp=str(item.get("timestamp", "")),
                details=item.get("details", {}) if isinstance(item.get("details"), dict) else {},
            ))
        return entries

    def append(self, entry: LedgerEntry) -> None:
        entries = self.read()
        entries.append(entry)
        if len(entries) > self._max_entries:
            entries = entries[-self._max_entries:]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([e.to_dict() for e in entries], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def clear(self) -> None:
        if self._path.exists():
            self._path.write_text("[]\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Mesh coordinator
# ---------------------------------------------------------------------------

class MeshCoordinator:
    """Coordinates decentralized nodes using an event bus and a shared ledger.

    Each node registers with the coordinator, emits events through the bus,
    and synchronizes state through the GitHub-backed ledger.
    """

    def __init__(
        self,
        *,
        ledger_path: Path,
        stale_threshold_sec: int = 300,
    ) -> None:
        self._bus = EventBus()
        self._ledger = GitHubLedger(ledger_path)
        self._nodes: dict[str, MeshNode] = {}
        self._stale_threshold = stale_threshold_sec

        # Self-subscribe to core event types for bookkeeping
        self._bus.subscribe(EventType.HEARTBEAT, self._on_heartbeat)
        self._bus.subscribe(EventType.NODE_JOINED, self._on_node_joined)
        self._bus.subscribe(EventType.NODE_LEFT, self._on_node_left)
        self._bus.subscribe(EventType.TASK_COMPLETED, self._on_task_completed)
        self._bus.subscribe(EventType.TASK_FAILED, self._on_task_failed)

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def ledger(self) -> GitHubLedger:
        return self._ledger

    @property
    def nodes(self) -> dict[str, MeshNode]:
        return dict(self._nodes)

    # -- node lifecycle -------------------------------------------------------

    def register_node(self, node: MeshNode) -> None:
        node.joined_at = _now_iso()
        node.last_heartbeat = _now_iso()
        self._nodes[node.node_id] = node
        event = MeshEvent(
            event_type=EventType.NODE_JOINED,
            source_node=node.node_id,
            timestamp=_now_iso(),
            payload=node.to_dict(),
        )
        self._bus.publish(event)

    def deregister_node(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)
        event = MeshEvent(
            event_type=EventType.NODE_LEFT,
            source_node=node_id,
            timestamp=_now_iso(),
        )
        self._bus.publish(event)

    def heartbeat(self, node_id: str, payload: dict[str, Any] | None = None) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.last_heartbeat = _now_iso()
            node.status = "active"
        event = MeshEvent(
            event_type=EventType.HEARTBEAT,
            source_node=node_id,
            timestamp=_now_iso(),
            payload=payload or {},
        )
        self._bus.publish(event)

    # -- event emission helpers -----------------------------------------------

    def emit(self, event: MeshEvent) -> None:
        self._bus.publish(event)

    def emit_task_claimed(self, node_id: str, task_id: str) -> None:
        self._bus.publish(MeshEvent(
            event_type=EventType.TASK_CLAIMED,
            source_node=node_id,
            timestamp=_now_iso(),
            payload={"task_id": task_id},
        ))

    def emit_task_completed(self, node_id: str, task_id: str, summary: str = "") -> None:
        self._bus.publish(MeshEvent(
            event_type=EventType.TASK_COMPLETED,
            source_node=node_id,
            timestamp=_now_iso(),
            payload={"task_id": task_id, "summary": summary},
        ))

    def emit_task_failed(self, node_id: str, task_id: str, error: str = "") -> None:
        self._bus.publish(MeshEvent(
            event_type=EventType.TASK_FAILED,
            source_node=node_id,
            timestamp=_now_iso(),
            payload={"task_id": task_id, "error": error},
        ))

    # -- queries --------------------------------------------------------------

    def active_nodes(self) -> list[MeshNode]:
        return [n for n in self._nodes.values() if not n.is_stale(self._stale_threshold)]

    def stale_nodes(self) -> list[MeshNode]:
        return [n for n in self._nodes.values() if n.is_stale(self._stale_threshold)]

    def snapshot(self) -> dict[str, Any]:
        active = self.active_nodes()
        stale = self.stale_nodes()
        return {
            "timestamp": _now_iso(),
            "total_nodes": len(self._nodes),
            "active_nodes": len(active),
            "stale_nodes": len(stale),
            "nodes": {nid: n.to_dict() for nid, n in self._nodes.items()},
            "event_history_size": len(self._bus.history),
            "ledger_entries": len(self._ledger.read()),
        }

    # -- internal handlers ----------------------------------------------------

    def _on_heartbeat(self, event: MeshEvent) -> None:
        node = self._nodes.get(event.source_node)
        if node:
            node.last_heartbeat = event.timestamp

    def _on_node_joined(self, event: MeshEvent) -> None:
        self._ledger.append(LedgerEntry(
            entry_id=event.event_id,
            node_id=event.source_node,
            action="node_joined",
            timestamp=event.timestamp,
            details=event.payload,
        ))

    def _on_node_left(self, event: MeshEvent) -> None:
        self._ledger.append(LedgerEntry(
            entry_id=event.event_id,
            node_id=event.source_node,
            action="node_left",
            timestamp=event.timestamp,
        ))

    def _on_task_completed(self, event: MeshEvent) -> None:
        self._ledger.append(LedgerEntry(
            entry_id=event.event_id,
            node_id=event.source_node,
            action="task_completed",
            timestamp=event.timestamp,
            details=event.payload,
        ))

    def _on_task_failed(self, event: MeshEvent) -> None:
        self._ledger.append(LedgerEntry(
            entry_id=event.event_id,
            node_id=event.source_node,
            action="task_failed",
            timestamp=event.timestamp,
            details=event.payload,
        ))
