import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.causal_decision_bridge import enforce_causal_metadata_gate


class CausalDecisionBridgeTests(unittest.TestCase):
    def test_enforced_mode_rejects_disruptive_scale_down_without_hypothesis(self):
        with mock.patch.dict("os.environ", {"ORXAQ_AUTONOMY_CAUSAL_GATE_MODE": "enforced"}, clear=False):
            result = enforce_causal_metadata_gate(
                action="scale_down",
                requested_lane="all_enabled",
                causal_hypothesis_id="",
            )
            self.assertFalse(result["allowed"])
            self.assertEqual(result["status"], "missing_hypothesis_rejected")
