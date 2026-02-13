"""Tests for Issue #12: Lane occupancy rebalancing to prevent lower-tier starvation."""

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.swarm_orchestrator import LaneOccupancy, RoutingTier


class LaneOccupancyTests(unittest.TestCase):
    def test_initial_state_is_empty(self):
        occ = LaneOccupancy()
        self.assertEqual(occ.total_routed(), 1)  # max(1, 0)
        self.assertFalse(occ.is_saturated("L0"))
        self.assertEqual(occ.to_dict()["routed_counts"], {})

    def test_record_routed_tracks_counts(self):
        occ = LaneOccupancy()
        occ.record_routed("L0")
        occ.record_routed("L0")
        occ.record_routed("L1")
        self.assertEqual(occ.routed_counts["L0"], 2)
        self.assertEqual(occ.routed_counts["L1"], 1)

    def test_tier_share_calculation(self):
        occ = LaneOccupancy()
        occ.record_routed("L0")
        occ.record_routed("L0")
        occ.record_routed("L1")
        occ.record_routed("L1")
        self.assertAlmostEqual(occ.tier_share("L0"), 0.5)
        self.assertAlmostEqual(occ.tier_share("L1"), 0.5)

    def test_is_saturated_above_threshold(self):
        occ = LaneOccupancy(tier_saturation_threshold=0.60)
        for _ in range(7):
            occ.record_routed("L2")
        for _ in range(3):
            occ.record_routed("L0")
        # L2 has 70% share, above 60% threshold
        self.assertTrue(occ.is_saturated("L2"))
        self.assertFalse(occ.is_saturated("L0"))

    def test_rebalance_candidates_no_change_when_unsaturated(self):
        occ = LaneOccupancy(tier_saturation_threshold=0.60)
        occ.record_routed("L0")
        occ.record_routed("L1")
        candidates = [RoutingTier.L1_LOCAL_STRONG, RoutingTier.L0_LOCAL_SMALL]
        result = occ.rebalance_candidates(candidates)
        self.assertEqual(result, candidates)

    def test_rebalance_candidates_promotes_unsaturated(self):
        occ = LaneOccupancy(tier_saturation_threshold=0.50)
        for _ in range(8):
            occ.record_routed(RoutingTier.L2_CLOUD_STANDARD)
        for _ in range(2):
            occ.record_routed(RoutingTier.L0_LOCAL_SMALL)
        # L2 is saturated (80%), L0 is not (20%)
        candidates = [RoutingTier.L2_CLOUD_STANDARD, RoutingTier.L0_LOCAL_SMALL, RoutingTier.L1_LOCAL_STRONG]
        result = occ.rebalance_candidates(candidates)
        # L0 and L1 (unsaturated) should come first
        self.assertEqual(result[0], RoutingTier.L0_LOCAL_SMALL)
        self.assertIn(RoutingTier.L2_CLOUD_STANDARD, result)

    def test_rebalance_single_candidate_unchanged(self):
        occ = LaneOccupancy()
        for _ in range(10):
            occ.record_routed("L0")
        result = occ.rebalance_candidates(["L0"])
        self.assertEqual(result, ["L0"])

    def test_rebalance_all_saturated_preserves_order(self):
        occ = LaneOccupancy(tier_saturation_threshold=0.30)
        for _ in range(5):
            occ.record_routed("L0")
        for _ in range(5):
            occ.record_routed("L1")
        # Both at 50%, both above 30% threshold
        result = occ.rebalance_candidates(["L0", "L1"])
        self.assertEqual(result, ["L0", "L1"])

    def test_acquire_release_tracking(self):
        occ = LaneOccupancy()
        occ.acquire("L0")
        occ.acquire("L0")
        self.assertEqual(occ.active_counts["L0"], 2)
        occ.release("L0")
        self.assertEqual(occ.active_counts["L0"], 1)
        occ.release("L0")
        self.assertEqual(occ.active_counts["L0"], 0)
        occ.release("L0")  # should not go negative
        self.assertEqual(occ.active_counts["L0"], 0)

    def test_to_dict_includes_tier_shares(self):
        occ = LaneOccupancy(tier_saturation_threshold=0.65)
        occ.record_routed("L0")
        occ.record_routed("L0")
        occ.record_routed("L1")
        d = occ.to_dict()
        self.assertIn("tier_shares", d)
        self.assertAlmostEqual(d["tier_shares"]["L0"], 2 / 3, places=3)
        self.assertEqual(d["saturation_threshold"], 0.65)


class SwarmOrchestratorLaneIntegrationTests(unittest.TestCase):
    """Integration tests verifying that the SwarmOrchestrator uses lane occupancy."""

    def test_orchestrator_has_lane_occupancy(self):
        from orxaq_autonomy.swarm_orchestrator import SwarmOrchestrator

        orch = SwarmOrchestrator(lane_saturation_threshold=0.55)
        self.assertIsInstance(orch.lane_occupancy, LaneOccupancy)
        self.assertEqual(orch.lane_occupancy.tier_saturation_threshold, 0.55)

    def test_status_snapshot_includes_lane_occupancy(self):
        from orxaq_autonomy.swarm_orchestrator import SwarmOrchestrator

        orch = SwarmOrchestrator()
        with mock.patch.object(orch._network, "check") as net_mock, mock.patch.object(
            orch._lm_client, "health_check"
        ) as lm_mock:
            net_mock.return_value = mock.Mock(to_dict=lambda: {"status": "online"})
            lm_mock.return_value = mock.Mock(to_dict=lambda: {"reachable": True})
            snap = orch.status_snapshot()
        self.assertIn("lane_occupancy", snap)
        self.assertIn("routed_counts", snap["lane_occupancy"])


if __name__ == "__main__":
    unittest.main()
