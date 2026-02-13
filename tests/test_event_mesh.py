import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.event_mesh import (
    EventBus,
    EventType,
    GitHubLedger,
    LedgerEntry,
    MeshCoordinator,
    MeshEvent,
    MeshNode,
)


class TestMeshEvent(unittest.TestCase):
    def test_event_generates_deterministic_id(self):
        e1 = MeshEvent(
            event_type=EventType.HEARTBEAT,
            source_node="node-a",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        e2 = MeshEvent(
            event_type=EventType.HEARTBEAT,
            source_node="node-a",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(e1.event_id, e2.event_id)
        self.assertTrue(len(e1.event_id) > 0)

    def test_event_different_payload_different_id(self):
        e1 = MeshEvent(
            event_type=EventType.HEARTBEAT,
            source_node="node-a",
            timestamp="2026-01-01T00:00:00+00:00",
            payload={"key": "val1"},
        )
        e2 = MeshEvent(
            event_type=EventType.HEARTBEAT,
            source_node="node-a",
            timestamp="2026-01-01T00:00:00+00:00",
            payload={"key": "val2"},
        )
        self.assertNotEqual(e1.event_id, e2.event_id)

    def test_event_to_dict(self):
        e = MeshEvent(
            event_type=EventType.TASK_COMPLETED,
            source_node="node-b",
            timestamp="2026-02-01T00:00:00+00:00",
            payload={"task_id": "t-1"},
        )
        d = e.to_dict()
        self.assertEqual(d["event_type"], "task_completed")
        self.assertEqual(d["source_node"], "node-b")
        self.assertEqual(d["payload"]["task_id"], "t-1")


class TestMeshNode(unittest.TestCase):
    def test_node_stale_when_no_heartbeat(self):
        node = MeshNode(node_id="n1")
        self.assertTrue(node.is_stale())

    def test_node_not_stale_with_fresh_heartbeat(self):
        from datetime import datetime, timezone

        node = MeshNode(
            node_id="n1",
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
        )
        self.assertFalse(node.is_stale(stale_threshold_sec=300))

    def test_node_to_dict(self):
        node = MeshNode(node_id="n1", capabilities=["code", "test"])
        d = node.to_dict()
        self.assertEqual(d["node_id"], "n1")
        self.assertEqual(d["capabilities"], ["code", "test"])


class TestEventBus(unittest.TestCase):
    def test_publish_dispatches_to_subscribers(self):
        bus = EventBus()
        received = []
        bus.subscribe(EventType.HEARTBEAT, lambda e: received.append(e))
        event = MeshEvent(
            event_type=EventType.HEARTBEAT,
            source_node="n1",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        bus.publish(event)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].source_node, "n1")

    def test_publish_only_dispatches_to_matching_type(self):
        bus = EventBus()
        heartbeats = []
        tasks = []
        bus.subscribe(EventType.HEARTBEAT, lambda e: heartbeats.append(e))
        bus.subscribe(EventType.TASK_COMPLETED, lambda e: tasks.append(e))
        bus.publish(MeshEvent(
            event_type=EventType.HEARTBEAT,
            source_node="n1",
            timestamp="2026-01-01T00:00:00+00:00",
        ))
        self.assertEqual(len(heartbeats), 1)
        self.assertEqual(len(tasks), 0)

    def test_history_is_maintained(self):
        bus = EventBus()
        for i in range(5):
            bus.publish(MeshEvent(
                event_type=EventType.HEARTBEAT,
                source_node=f"n{i}",
                timestamp=f"2026-01-01T00:00:0{i}+00:00",
            ))
        self.assertEqual(len(bus.history), 5)

    def test_handler_exception_does_not_break_bus(self):
        bus = EventBus()
        calls = []

        def bad_handler(e):
            raise ValueError("oops")

        def good_handler(e):
            calls.append(e)

        bus.subscribe(EventType.HEARTBEAT, bad_handler)
        bus.subscribe(EventType.HEARTBEAT, good_handler)
        bus.publish(MeshEvent(
            event_type=EventType.HEARTBEAT,
            source_node="n1",
            timestamp="2026-01-01T00:00:00+00:00",
        ))
        self.assertEqual(len(calls), 1)


class TestGitHubLedger(unittest.TestCase):
    def test_append_and_read(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "ledger.json"
            ledger = GitHubLedger(path)
            self.assertEqual(ledger.read(), [])
            entry = LedgerEntry(
                entry_id="e1",
                node_id="n1",
                action="task_completed",
                timestamp="2026-01-01T00:00:00+00:00",
                details={"task_id": "t-1"},
            )
            ledger.append(entry)
            entries = ledger.read()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].entry_id, "e1")
            self.assertEqual(entries[0].action, "task_completed")

    def test_ledger_truncates_to_max_entries(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "ledger.json"
            ledger = GitHubLedger(path, max_entries=3)
            for i in range(5):
                ledger.append(LedgerEntry(
                    entry_id=f"e{i}",
                    node_id="n1",
                    action="heartbeat",
                    timestamp=f"2026-01-01T00:00:0{i}+00:00",
                ))
            entries = ledger.read()
            self.assertEqual(len(entries), 3)
            # Should keep the latest entries
            self.assertEqual(entries[0].entry_id, "e2")

    def test_clear_empties_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "ledger.json"
            ledger = GitHubLedger(path)
            ledger.append(LedgerEntry(
                entry_id="e1", node_id="n1", action="test", timestamp="t",
            ))
            self.assertEqual(len(ledger.read()), 1)
            ledger.clear()
            self.assertEqual(len(ledger.read()), 0)

    def test_read_handles_corrupt_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "ledger.json"
            path.write_text("not json!", encoding="utf-8")
            ledger = GitHubLedger(path)
            self.assertEqual(ledger.read(), [])


class TestMeshCoordinator(unittest.TestCase):
    def test_register_and_query_nodes(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = pathlib.Path(td) / "ledger.json"
            coord = MeshCoordinator(ledger_path=ledger_path)
            node_a = MeshNode(node_id="a", capabilities=["code"])
            node_b = MeshNode(node_id="b", capabilities=["test"])
            coord.register_node(node_a)
            coord.register_node(node_b)
            self.assertEqual(len(coord.nodes), 2)
            self.assertEqual(len(coord.active_nodes()), 2)

    def test_heartbeat_updates_node(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = pathlib.Path(td) / "ledger.json"
            coord = MeshCoordinator(ledger_path=ledger_path)
            node = MeshNode(node_id="a")
            coord.register_node(node)
            old_hb = coord.nodes["a"].last_heartbeat
            import time
            time.sleep(0.01)
            coord.heartbeat("a", {"cycle": 5})
            new_hb = coord.nodes["a"].last_heartbeat
            self.assertGreaterEqual(new_hb, old_hb)

    def test_deregister_removes_node(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = pathlib.Path(td) / "ledger.json"
            coord = MeshCoordinator(ledger_path=ledger_path)
            coord.register_node(MeshNode(node_id="a"))
            coord.deregister_node("a")
            self.assertEqual(len(coord.nodes), 0)

    def test_task_events_logged_to_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = pathlib.Path(td) / "ledger.json"
            coord = MeshCoordinator(ledger_path=ledger_path)
            coord.register_node(MeshNode(node_id="a"))
            coord.emit_task_completed("a", "t-1", summary="done")
            coord.emit_task_failed("a", "t-2", error="timeout")
            entries = coord.ledger.read()
            # node_joined + task_completed + task_failed = 3 entries
            self.assertEqual(len(entries), 3)
            actions = [e.action for e in entries]
            self.assertIn("task_completed", actions)
            self.assertIn("task_failed", actions)

    def test_snapshot_returns_expected_keys(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = pathlib.Path(td) / "ledger.json"
            coord = MeshCoordinator(ledger_path=ledger_path)
            coord.register_node(MeshNode(node_id="a"))
            snap = coord.snapshot()
            self.assertIn("total_nodes", snap)
            self.assertIn("active_nodes", snap)
            self.assertIn("stale_nodes", snap)
            self.assertIn("event_history_size", snap)
            self.assertEqual(snap["total_nodes"], 1)

    def test_event_bus_history_captured_by_coordinator(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = pathlib.Path(td) / "ledger.json"
            coord = MeshCoordinator(ledger_path=ledger_path)
            coord.register_node(MeshNode(node_id="a"))
            coord.heartbeat("a")
            coord.emit_task_claimed("a", "t-1")
            # At least node_joined + heartbeat + task_claimed
            self.assertGreaterEqual(len(coord.bus.history), 3)


if __name__ == "__main__":
    unittest.main()
