"""Tests for Issue #19: Distributed coordinator HA (DMN + DAG + fencing)."""

import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.distributed_coordinator import (
    CoordinatorObserver,
    DAGNode,
    DMNPolicyEngine,
    EpochFence,
    ExecutionDAG,
    FileLease,
    PolicyDecision,
    PolicyRule,
)


class FileLeaseTests(unittest.TestCase):
    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            self.assertTrue(lease.acquire("node-1", ttl_sec=60))
            self.assertEqual(lease.current_holder(), "node-1")
            self.assertTrue(lease.is_held_by("node-1"))
            self.assertTrue(lease.release("node-1"))
            self.assertIsNone(lease.current_holder())

    def test_acquire_fails_when_held_by_another(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            self.assertTrue(lease.acquire("node-1", ttl_sec=600))
            self.assertFalse(lease.acquire("node-2", ttl_sec=60))
            self.assertEqual(lease.current_holder(), "node-1")

    def test_acquire_succeeds_after_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            # Acquire with 0 TTL (already expired)
            self.assertTrue(lease.acquire("node-1", ttl_sec=0))
            # Another node can acquire since lease is expired
            time.sleep(0.01)  # Ensure time passes
            self.assertTrue(lease.acquire("node-2", ttl_sec=60))
            self.assertEqual(lease.current_holder(), "node-2")

    def test_renew_by_holder(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            self.assertTrue(lease.acquire("node-1", ttl_sec=60))
            self.assertTrue(lease.renew("node-1", ttl_sec=120))
            self.assertEqual(lease.current_holder(), "node-1")

    def test_renew_fails_for_non_holder(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            self.assertTrue(lease.acquire("node-1", ttl_sec=60))
            self.assertFalse(lease.renew("node-2", ttl_sec=120))

    def test_release_fails_for_non_holder(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            self.assertTrue(lease.acquire("node-1", ttl_sec=60))
            self.assertFalse(lease.release("node-2"))

    def test_epoch_increments_on_acquire(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            lease.acquire("node-1", ttl_sec=0)
            epoch1 = lease.epoch
            time.sleep(0.01)
            lease.acquire("node-2", ttl_sec=60)
            epoch2 = lease.epoch
            self.assertGreater(epoch2, epoch1)

    def test_no_lease_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "missing.json")
            self.assertIsNone(lease.current_holder())


class EpochFenceTests(unittest.TestCase):
    def test_valid_epoch_passes(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            lease.acquire("node-1", ttl_sec=60)
            fence = EpochFence(lease=lease, holder="node-1")
            ok, reason = fence.validate_epoch(lease.epoch)
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")

    def test_stale_epoch_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            lease.acquire("node-1", ttl_sec=60)
            fence = EpochFence(lease=lease, holder="node-1")
            ok, reason = fence.validate_epoch(lease.epoch - 1)
            self.assertFalse(ok)
            self.assertIn("stale epoch", reason)

    def test_wrong_holder_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            lease.acquire("node-1", ttl_sec=60)
            fence = EpochFence(lease=lease, holder="node-2")
            ok, reason = fence.validate_epoch(lease.epoch)
            self.assertFalse(ok)
            self.assertIn("not held by", reason)

    def test_fenced_execute_runs_on_valid_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            lease.acquire("node-1", ttl_sec=60)
            fence = EpochFence(lease=lease, holder="node-1")
            result = fence.fenced_execute(
                lease.epoch, "test_action", lambda: "success"
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"], "success")
            self.assertFalse(result["fenced"])

    def test_fenced_execute_blocks_on_stale_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            lease.acquire("node-1", ttl_sec=60)
            fence = EpochFence(lease=lease, holder="node-1")
            result = fence.fenced_execute(
                lease.epoch - 1, "test_action", lambda: "should not run"
            )
            self.assertFalse(result["ok"])
            self.assertTrue(result["fenced"])

    def test_fenced_execute_catches_exceptions(self):
        with tempfile.TemporaryDirectory() as td:
            lease = FileLease(path=pathlib.Path(td) / "lease.json")
            lease.acquire("node-1", ttl_sec=60)
            fence = EpochFence(lease=lease, holder="node-1")

            def failing_fn():
                raise ValueError("boom")

            result = fence.fenced_execute(lease.epoch, "fail_action", failing_fn)
            self.assertFalse(result["ok"])
            self.assertFalse(result["fenced"])
            self.assertIn("boom", result["reason"])


class DMNPolicyEngineTests(unittest.TestCase):
    def test_first_matching_rule_wins(self):
        engine = DMNPolicyEngine(rules=[
            PolicyRule(
                rule_id="r1",
                description="block offline merge",
                conditions={"network": "offline"},
                action="block",
                priority=0,
            ),
            PolicyRule(
                rule_id="r2",
                description="allow online",
                conditions={"network": "online"},
                action="allow",
                priority=1,
            ),
        ])
        decision = engine.evaluate({"network": "offline"})
        self.assertEqual(decision.matched_rule, "r1")
        self.assertEqual(decision.action, "block")

    def test_no_match_returns_default_allow(self):
        engine = DMNPolicyEngine(rules=[
            PolicyRule(
                rule_id="r1",
                description="require online",
                conditions={"network": "online"},
                action="proceed",
            ),
        ])
        decision = engine.evaluate({"network": "degraded"})
        self.assertEqual(decision.matched_rule, "")
        self.assertEqual(decision.action, "allow")

    def test_explain_trace_contains_all_evaluations(self):
        engine = DMNPolicyEngine(rules=[
            PolicyRule(rule_id="r1", description="d1", conditions={"a": 1}, action="x", priority=0),
            PolicyRule(rule_id="r2", description="d2", conditions={"a": 2}, action="y", priority=1),
        ])
        decision = engine.evaluate({"a": 2})
        self.assertGreater(len(decision.explain), 0)
        # r1 should be skipped, r2 matched
        self.assertTrue(any("r1" in line and "skip" in line for line in decision.explain))
        self.assertTrue(any("r2" in line and "matched" in line for line in decision.explain))

    def test_add_rule_maintains_priority_order(self):
        engine = DMNPolicyEngine()
        engine.add_rule(PolicyRule(rule_id="low", description="", conditions={}, action="x", priority=10))
        engine.add_rule(PolicyRule(rule_id="high", description="", conditions={}, action="y", priority=0))
        self.assertEqual(engine.rules[0].rule_id, "high")

    def test_empty_conditions_always_match(self):
        engine = DMNPolicyEngine(rules=[
            PolicyRule(rule_id="catch-all", description="", conditions={}, action="default"),
        ])
        decision = engine.evaluate({"any": "value"})
        self.assertEqual(decision.matched_rule, "catch-all")
        self.assertEqual(decision.action, "default")


class ExecutionDAGTests(unittest.TestCase):
    def test_linear_dependency_chain(self):
        dag = ExecutionDAG()
        dag.add_node("a")
        dag.add_node("b", depends_on=["a"])
        dag.add_node("c", depends_on=["b"])

        ready = dag.ready_nodes()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].node_id, "a")

        dag.mark_running("a")
        ready = dag.ready_nodes()
        self.assertEqual(len(ready), 0)

        dag.mark_done("a")
        ready = dag.ready_nodes()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].node_id, "b")

    def test_parallel_execution(self):
        dag = ExecutionDAG()
        dag.add_node("a")
        dag.add_node("b")
        dag.add_node("c", depends_on=["a", "b"])

        ready = dag.ready_nodes()
        self.assertEqual(len(ready), 2)
        ids = {n.node_id for n in ready}
        self.assertEqual(ids, {"a", "b"})

    def test_is_complete(self):
        dag = ExecutionDAG()
        dag.add_node("a")
        dag.add_node("b")
        self.assertFalse(dag.is_complete())
        dag.mark_done("a")
        self.assertFalse(dag.is_complete())
        dag.mark_done("b")
        self.assertTrue(dag.is_complete())

    def test_failed_node_counts_as_complete(self):
        dag = ExecutionDAG()
        dag.add_node("a")
        dag.mark_failed("a", "error")
        self.assertTrue(dag.is_complete())

    def test_causal_gate_check(self):
        dag = ExecutionDAG()
        dag.add_node("a")
        dag.add_node("b", causal_metadata={"requires_nodes_done": ["a"]})

        ok, reason = dag.check_causal_gate("b")
        self.assertFalse(ok)
        self.assertIn("not done", reason)

        dag.mark_done("a")
        ok, reason = dag.check_causal_gate("b")
        self.assertTrue(ok)

    def test_causal_gate_no_metadata(self):
        dag = ExecutionDAG()
        dag.add_node("a")
        ok, reason = dag.check_causal_gate("a")
        self.assertTrue(ok)

    def test_save_and_load_state(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = pathlib.Path(td) / "dag_state.json"

            dag1 = ExecutionDAG()
            dag1.add_node("a")
            dag1.add_node("b", depends_on=["a"])
            dag1.mark_done("a")
            dag1.save_state(state_path)

            self.assertTrue(state_path.exists())

            # Load into a new DAG with same structure
            dag2 = ExecutionDAG()
            dag2.add_node("a")
            dag2.add_node("b", depends_on=["a"])
            dag2.load_state(state_path)

            self.assertEqual(dag2.get_node("a").status, "done")
            self.assertEqual(dag2.get_node("b").status, "pending")
            ready = dag2.ready_nodes()
            self.assertEqual(len(ready), 1)
            self.assertEqual(ready[0].node_id, "b")

    def test_to_dict_structure(self):
        dag = ExecutionDAG()
        dag.add_node("a")
        dag.add_node("b", depends_on=["a"])
        dag.mark_done("a")
        d = dag.to_dict()
        self.assertIn("nodes", d)
        self.assertIn("complete", d)
        self.assertEqual(d["done"], ["a"])
        self.assertEqual(d["pending"], ["b"])


class CoordinatorObserverTests(unittest.TestCase):
    def test_record_events(self):
        obs = CoordinatorObserver()
        obs.record("lease_acquired", {"holder": "node-1"})
        obs.record("dag_scheduled", {"node_id": "a"})
        self.assertEqual(len(obs.events), 2)

    def test_recent_returns_latest(self):
        obs = CoordinatorObserver()
        for i in range(30):
            obs.record("event", {"i": i})
        recent = obs.recent(5)
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[-1]["detail"]["i"], 29)

    def test_max_events_cap(self):
        obs = CoordinatorObserver(max_events=10)
        for i in range(25):
            obs.record("event", {"i": i})
        self.assertEqual(len(obs.events), 10)

    def test_flush_to_file(self):
        with tempfile.TemporaryDirectory() as td:
            obs = CoordinatorObserver()
            obs.record("lease_acquired", {"holder": "n1"})
            path = pathlib.Path(td) / "events.json"
            obs.flush_to_file(path)
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], "coordinator-events.v1")
            self.assertEqual(data["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
