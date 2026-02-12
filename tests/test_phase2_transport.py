from __future__ import annotations

import unittest

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


class TestPhase2Transport(unittest.TestCase):
    def test_registry_contains_default_plugins(self) -> None:
        registry = default_transport_registry()
        self.assertEqual(registry.get("git").name, "git")
        self.assertEqual(registry.get("mcp").name, "mcp")
        self.assertEqual(registry.get("event_bus").name, "event_bus")

    def test_mcp_http_requires_localhost_origin_auth(self) -> None:
        plugin = MCPTransport(mode="http", bind_host="0.0.0.0", origin="", auth_token="")
        with self.assertRaises(ValueError):
            plugin.send(_item())

    def test_event_bus_idempotency(self) -> None:
        plugin = EventBusTransport()
        first = plugin.send(_item("k1"))
        second = plugin.send(_item("k1"))
        self.assertIs(first["duplicate"], False)
        self.assertIs(second["duplicate"], True)


if __name__ == "__main__":
    unittest.main()

