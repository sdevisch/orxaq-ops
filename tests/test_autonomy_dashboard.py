import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import dashboard


class DashboardTests(unittest.TestCase):
    def test_dashboard_html_contains_title_and_refresh(self):
        html = dashboard._dashboard_html(7)
        self.assertIn("Orxaq Autonomy Monitor", html)
        self.assertIn("const REFRESH_MS = 7000", html)
        self.assertIn("/api/monitor", html)
        self.assertIn("/api/conversations", html)
        self.assertIn("Parallel Lanes", html)
        self.assertIn("Conversations", html)
        self.assertIn("Resilience Diagnostics", html)
        self.assertIn("renderDiagnostics", html)
        self.assertIn("lane_agents", html)
        self.assertIn("idle (lane mode)", html)
        self.assertIn("operational", html)
        self.assertIn("fabric", html)

    def test_safe_monitor_snapshot_degrades_on_failure(self):
        with mock.patch("orxaq_autonomy.dashboard.monitor_snapshot", side_effect=RuntimeError("boom")):
            payload = dashboard._safe_monitor_snapshot(mock.Mock())
        self.assertIn("monitor snapshot error", payload["latest_log_line"])
        self.assertFalse(payload["diagnostics"]["ok"])

    def test_safe_lane_status_snapshot_degrades_on_failure(self):
        cfg = mock.Mock()
        cfg.lanes_file = pathlib.Path("/tmp/lanes.json")
        with mock.patch("orxaq_autonomy.dashboard.lane_status_snapshot", side_effect=RuntimeError("lane parse failed")):
            payload = dashboard._safe_lane_status_snapshot(cfg)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["total_count"], 0)
        self.assertIn("lane parse failed", payload["errors"][0])

    def test_safe_conversations_snapshot_degrades_on_failure(self):
        cfg = mock.Mock()
        cfg.conversation_log_file = pathlib.Path("/tmp/conversations.ndjson")
        with mock.patch("orxaq_autonomy.dashboard.conversations_snapshot", side_effect=RuntimeError("bad lane source")):
            payload = dashboard._safe_conversations_snapshot(cfg, lines=200)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial"])
        self.assertIn("bad lane source", payload["errors"][0])


if __name__ == "__main__":
    unittest.main()
