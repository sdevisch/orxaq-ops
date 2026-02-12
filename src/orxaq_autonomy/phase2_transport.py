from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkItem:
    work_id: str
    task_type: str
    risk_level: str
    lane_hint: str
    payload: dict[str, Any]
    idempotency_key: str


class TransportPlugin(ABC):
    name: str

    @abstractmethod
    def send(self, item: WorkItem) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def receive(self) -> list[WorkItem]:
        raise NotImplementedError


class GitTransport(TransportPlugin):
    name = "git"

    def __init__(self) -> None:
        self._queue: list[WorkItem] = []

    def send(self, item: WorkItem) -> dict[str, Any]:
        self._queue.append(item)
        return {"transport": self.name, "accepted": True, "work_id": item.work_id}

    def receive(self) -> list[WorkItem]:
        items = list(self._queue)
        self._queue.clear()
        return items


class MCPTransport(TransportPlugin):
    name = "mcp"

    def __init__(self, *, mode: str = "stdio", bind_host: str = "127.0.0.1", origin: str = "", auth_token: str = "") -> None:
        self.mode = mode
        self.bind_host = bind_host
        self.origin = origin
        self.auth_token = auth_token
        self._queue: list[WorkItem] = []

    def _validate_http_safety(self) -> None:
        if self.mode != "http":
            return
        if self.bind_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MCP HTTP must bind localhost by default")
        if not self.origin:
            raise ValueError("MCP HTTP Origin is required")
        if not self.auth_token:
            raise ValueError("MCP HTTP auth_token is required when exposed")

    def send(self, item: WorkItem) -> dict[str, Any]:
        self._validate_http_safety()
        self._queue.append(item)
        return {"transport": self.name, "mode": self.mode, "accepted": True, "work_id": item.work_id}

    def receive(self) -> list[WorkItem]:
        items = list(self._queue)
        self._queue.clear()
        return items


class EventBusTransport(TransportPlugin):
    name = "event_bus"

    def __init__(self) -> None:
        self._queue: list[WorkItem] = []
        self._seen: set[str] = set()

    def send(self, item: WorkItem) -> dict[str, Any]:
        if not item.idempotency_key:
            raise ValueError("idempotency_key required for at-least-once semantics")
        if item.idempotency_key in self._seen:
            return {"transport": self.name, "accepted": True, "duplicate": True, "work_id": item.work_id}
        self._seen.add(item.idempotency_key)
        self._queue.append(item)
        return {"transport": self.name, "accepted": True, "duplicate": False, "work_id": item.work_id}

    def receive(self) -> list[WorkItem]:
        items = list(self._queue)
        self._queue.clear()
        return items


class TransportRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, TransportPlugin] = {}

    def register(self, plugin: TransportPlugin) -> None:
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> TransportPlugin:
        key = name.strip().lower()
        if key not in self._plugins:
            raise KeyError(f"unknown transport plugin: {name}")
        return self._plugins[key]


def default_transport_registry() -> TransportRegistry:
    registry = TransportRegistry()
    registry.register(GitTransport())
    registry.register(MCPTransport(mode="stdio"))
    registry.register(EventBusTransport())
    return registry
