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
        self.assertIn("/api/lanes", html)
        self.assertIn("/api/conversations", html)
        self.assertIn("/api/lanes/action", html)
        self.assertIn("Parallel Lanes", html)
        self.assertIn("Conversations", html)
        self.assertIn("Cost &amp; Quality", html)
        self.assertIn("metricsSummary", html)
        self.assertIn("excitingStat", html)
        self.assertIn("Resilience Diagnostics", html)
        self.assertIn("renderDiagnostics", html)
        self.assertIn("lane_agents", html)
        self.assertIn("idle (lane mode)", html)
        self.assertIn("operational", html)
        self.assertIn("fabric", html)
        self.assertIn("optimization_recommendations", html)
        self.assertIn("laneActionStatus", html)
        self.assertIn("convOwner", html)
        self.assertIn("conversationPath", html)

    def test_safe_monitor_snapshot_degrades_on_failure(self):
        with mock.patch("orxaq_autonomy.dashboard.monitor_snapshot", side_effect=RuntimeError("boom")):
            payload = dashboard._safe_monitor_snapshot(mock.Mock())
        self.assertIn("monitor snapshot error", payload["latest_log_line"])
        self.assertFalse(payload["diagnostics"]["ok"])
        self.assertIn("response_metrics", payload)
        self.assertFalse(payload["response_metrics"]["ok"])

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
            payload = dashboard._safe_conversations_snapshot(cfg, lines=200, owner="codex")
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial"])
        self.assertIn("bad lane source", payload["errors"][0])
        self.assertEqual(payload["filters"]["owner"], "codex")

    def test_apply_conversation_filters_matches_owner_and_lane(self):
        payload = {
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "alpha",
                },
                {
                    "timestamp": "2026-01-01T00:00:01+00:00",
                    "owner": "gemini",
                    "lane_id": "lane-b",
                    "event_type": "message",
                    "content": "beta",
                },
            ],
            "owner_counts": {"codex": 1, "gemini": 1},
            "total_events": 2,
        }
        filtered = dashboard._apply_conversation_filters(
            payload,
            owner="codex",
            lane_id="lane-a",
            event_type="status",
            contains="alp",
            tail=1,
        )
        self.assertEqual(filtered["total_events"], 1)
        self.assertEqual(filtered["unfiltered_total_events"], 2)
        self.assertEqual(filtered["owner_counts"], {"codex": 1})
        self.assertEqual(filtered["events"][0]["lane_id"], "lane-a")

    def test_safe_conversations_snapshot_applies_filters(self):
        cfg = mock.Mock()
        cfg.conversation_log_file = pathlib.Path("/tmp/conversations.ndjson")
        source_payload = {
            "total_events": 2,
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "alpha",
                },
                {
                    "timestamp": "2026-01-01T00:00:01+00:00",
                    "owner": "gemini",
                    "lane_id": "lane-b",
                    "event_type": "message",
                    "content": "beta",
                },
            ],
            "owner_counts": {"codex": 1, "gemini": 1},
            "sources": [],
            "partial": False,
            "ok": True,
            "errors": [],
        }
        with mock.patch("orxaq_autonomy.dashboard.conversations_snapshot", return_value=source_payload):
            payload = dashboard._safe_conversations_snapshot(
                cfg,
                lines=200,
                owner="codex",
                lane_id="lane-a",
                event_type="status",
                contains="alpha",
                tail=5,
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total_events"], 1)
        self.assertEqual(payload["filters"]["owner"], "codex")

    def test_safe_lane_action_returns_structured_error(self):
        cfg = mock.Mock()
        payload = dashboard._safe_lane_action(cfg, action="unknown", lane_id="lane-a")
        self.assertFalse(payload["ok"])
        self.assertIn("unsupported action", payload["error"])

    def test_safe_lane_action_start_handles_exceptions(self):
        cfg = mock.Mock()
        with mock.patch("orxaq_autonomy.dashboard.start_lanes_background", side_effect=RuntimeError("spawn failed")):
            payload = dashboard._safe_lane_action(cfg, action="start", lane_id="lane-a")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["lane"], "lane-a")
        self.assertIn("spawn failed", payload["error"])


if __name__ == "__main__":
    unittest.main()
