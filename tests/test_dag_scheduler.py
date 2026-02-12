import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.dag_scheduler import DagNode, frontier_ready_nodes, replay_safe_claim, transition_node_state


class DagSchedulerTests(unittest.TestCase):
    def test_frontier_and_replay_safe_claim(self):
        nodes = {
            "a": DagNode(node_id="a", dependencies=()),
            "b": DagNode(node_id="b", dependencies=("a",)),
        }
        state = {"a": {"state": "pending"}, "b": {"state": "pending"}}
        self.assertEqual(frontier_ready_nodes(nodes, state), ["a"])
        claim_1 = replay_safe_claim(dag_state=state, node_id="a", task_id="task-a", attempt=1, leader_epoch=3)
        claim_2 = replay_safe_claim(dag_state=state, node_id="a", task_id="task-a", attempt=1, leader_epoch=3)
        self.assertFalse(claim_1["deduped"])
        self.assertTrue(claim_2["deduped"])
        transition_node_state(dag_state=state, node_id="a", next_state="success", reason="done")
        self.assertEqual(frontier_ready_nodes(nodes, state), ["b"])
