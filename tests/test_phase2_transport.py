from __future__ import annotations

import pytest

from orxaq_autonomy.phase2_transport import MCPTransport, EventBusTransport, WorkItem, default_transport_registry


def _item(key: str = "k1") -> WorkItem:
    return WorkItem(
        work_id="w1",
        task_type="docs",
        risk_level="low",
        lane_hint="L0",
        payload={"x": 1},
        idempotency_key=key,
    )


def test_registry_contains_default_plugins() -> None:
    registry = default_transport_registry()
    assert registry.get("git").name == "git"
    assert registry.get("mcp").name == "mcp"
    assert registry.get("event_bus").name == "event_bus"


def test_mcp_http_requires_localhost_origin_auth() -> None:
    plugin = MCPTransport(mode="http", bind_host="0.0.0.0", origin="", auth_token="")
    with pytest.raises(ValueError):
        plugin.send(_item())


def test_event_bus_idempotency() -> None:
    plugin = EventBusTransport()
    first = plugin.send(_item("k1"))
    second = plugin.send(_item("k1"))
    assert first["duplicate"] is False
    assert second["duplicate"] is True
