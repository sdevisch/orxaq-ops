import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.dmn_engine import evaluate_scaling_decision, load_scaling_decision_table


class DmnEngineTests(unittest.TestCase):
    def test_default_table_preserves_scale_up_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            table = load_scaling_decision_table(pathlib.Path(td))
            decision = evaluate_scaling_decision(
                facts={
                    "failed_count": 0,
                    "parallel_groups_at_limit": 1,
                    "started_count": 0,
                    "restarted_count": 0,
                    "scaled_up_count": 0,
                    "scaled_down_count": 0,
                },
                table=table,
            )
            self.assertEqual(decision["action"], "scale_up")
            trace = decision["decision_trace"]
            self.assertTrue(str(trace.get("decision_table_version", "")).strip())
            self.assertGreaterEqual(len(trace.get("matched_rule_ids", [])), 1)
            self.assertTrue(str(trace.get("inputs_hash", "")).strip())
