import pathlib
import sys
from datetime import datetime, timezone
from http import HTTPStatus
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
        self.assertIn("laneOwnerSummary", html)
        self.assertIn("source_errors", html)
        self.assertIn("source_error:", html)
        self.assertIn("stopped=${payload.stopped_count || 0} failed=${failed}", html)
        self.assertIn("convOwner", html)
        self.assertIn("conversationSources", html)
        self.assertIn("conversationPath", html)
        self.assertIn("laneStatusPath", html)
        self.assertIn("filterFallbackConversationEvents", html)
        self.assertIn("FETCH_TIMEOUT_MS", html)
        self.assertIn("timeout after", html)
        self.assertIn("const rawBody = await response.text();", html)
        self.assertIn("HTTP ${response.status}: ${detail}", html)
        self.assertIn("result.payload && result.payload.error", html)

    def test_safe_monitor_snapshot_degrades_on_failure(self):
        with mock.patch("orxaq_autonomy.dashboard.monitor_snapshot", side_effect=RuntimeError("boom")):
            payload = dashboard._safe_monitor_snapshot(mock.Mock())
        self.assertIn("monitor snapshot error", payload["latest_log_line"])
        self.assertFalse(payload["diagnostics"]["ok"])
        self.assertEqual(payload["conversations"]["recent_events"], [])
        self.assertIn("owner_counts", payload["lanes"])
        self.assertIn("lane_owner_health", payload["runtime"])
        self.assertIn("response_metrics", payload)
        self.assertFalse(payload["response_metrics"]["ok"])

    def test_safe_lane_status_snapshot_degrades_on_failure(self):
        cfg = mock.Mock()
        cfg.lanes_file = pathlib.Path("/tmp/lanes.json")
        with mock.patch("orxaq_autonomy.dashboard.lane_status_snapshot", side_effect=RuntimeError("lane parse failed")):
            payload = dashboard._safe_lane_status_snapshot(cfg)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["total_count"], 0)
        self.assertEqual(payload["health_counts"], {})
        self.assertEqual(payload["owner_counts"], {})
        self.assertIn("lane parse failed", payload["errors"][0])

    def test_filter_lane_status_payload_filters_selected_lane(self):
        payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": True,
            "partial": False,
            "errors": [],
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
                {"id": "lane-b", "owner": "gemini", "running": False, "health": "stale"},
            ],
        }
        filtered = dashboard._filter_lane_status_payload(payload, lane_id="lane-a")
        self.assertEqual(filtered["requested_lane"], "lane-a")
        self.assertEqual(filtered["total_count"], 1)
        self.assertEqual(filtered["running_count"], 1)
        self.assertTrue(filtered["ok"])
        self.assertFalse(filtered["partial"])
        self.assertEqual(filtered["health_counts"], {"ok": 1})
        self.assertEqual(filtered["owner_counts"]["codex"]["total"], 1)

    def test_filter_lane_status_payload_reports_unknown_lane(self):
        payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": True,
            "partial": False,
            "errors": [],
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
            ],
        }
        filtered = dashboard._filter_lane_status_payload(payload, lane_id="missing-lane")
        self.assertEqual(filtered["requested_lane"], "missing-lane")
        self.assertEqual(filtered["total_count"], 0)
        self.assertFalse(filtered["ok"])
        self.assertTrue(filtered["partial"])
        self.assertTrue(filtered["errors"])
        self.assertIn("Unknown lane id", filtered["errors"][0])

    def test_safe_conversations_snapshot_degrades_on_failure(self):
        cfg = mock.Mock()
        cfg.conversation_log_file = pathlib.Path("/tmp/conversations.ndjson")
        with mock.patch("orxaq_autonomy.dashboard.conversations_snapshot", side_effect=RuntimeError("bad lane source")):
            payload = dashboard._safe_conversations_snapshot(cfg, lines=200, owner="codex")
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial"])
        self.assertIn("bad lane source", payload["errors"][0])
        self.assertEqual(payload["filters"]["owner"], "codex")
        self.assertEqual(len(payload["sources"]), 1)
        self.assertEqual(payload["sources"][0]["kind"], "primary")
        self.assertFalse(payload["sources"][0]["ok"])
        self.assertEqual(payload["sources"][0]["path"], str(cfg.conversation_log_file))

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

    def test_safe_lane_action_ensure_forwards_lane_id(self):
        cfg = mock.Mock()
        with mock.patch(
            "orxaq_autonomy.dashboard.ensure_lanes_background",
            return_value={"ok": True, "ensured_count": 1, "failed_count": 0},
        ) as ensure:
            payload = dashboard._safe_lane_action(cfg, action="ensure", lane_id="lane-a")
        ensure.assert_called_once_with(cfg, lane_id="lane-a")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["lane"], "lane-a")
        self.assertEqual(payload["action"], "ensure")

    def test_safe_lane_action_start_handles_exceptions(self):
        cfg = mock.Mock()
        with mock.patch("orxaq_autonomy.dashboard.start_lanes_background", side_effect=RuntimeError("spawn failed")):
            payload = dashboard._safe_lane_action(cfg, action="start", lane_id="lane-a")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["lane"], "lane-a")
        self.assertIn("spawn failed", payload["error"])

    def test_safe_lane_action_stop_preserves_failure_status(self):
        cfg = mock.Mock()
        with mock.patch(
            "orxaq_autonomy.dashboard.stop_lanes_background",
            return_value={"ok": False, "stopped_count": 0, "failed_count": 1},
        ):
            payload = dashboard._safe_lane_action(cfg, action="stop", lane_id="lane-a")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["lane"], "lane-a")
        self.assertEqual(payload["action"], "stop")

    def test_safe_lane_action_stop_infers_ok_when_not_provided(self):
        cfg = mock.Mock()
        with mock.patch(
            "orxaq_autonomy.dashboard.stop_lanes_background",
            return_value={"stopped_count": 1, "failed_count": 0},
        ):
            payload = dashboard._safe_lane_action(cfg, action="stop", lane_id="")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["failed_count"], 0)
        self.assertEqual(payload["lane"], "")

    def test_lane_action_http_status_ok_when_action_succeeds(self):
        status = dashboard._lane_action_http_status({"ok": True})
        self.assertEqual(status, HTTPStatus.OK)

    def test_lane_action_http_status_bad_request_for_unsupported_action(self):
        status = dashboard._lane_action_http_status({"ok": False, "error": "unsupported action"})
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)

    def test_lane_action_http_status_not_found_for_unknown_lane(self):
        status = dashboard._lane_action_http_status(
            {"ok": False, "error": "Unknown lane id 'missing-lane'. Update /tmp/lanes.json."}
        )
        self.assertEqual(status, HTTPStatus.NOT_FOUND)

    def test_lane_action_http_status_service_unavailable_for_runtime_error(self):
        status = dashboard._lane_action_http_status({"ok": False, "error": "lane runtime unavailable"})
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)

    def test_safe_daw_snapshot_uses_monitor_fallback_when_monitor_fails(self):
        cfg = mock.Mock()
        now = datetime.now(timezone.utc).isoformat()
        conv_payload = {
            "ok": True,
            "errors": [],
            "events": [
                {
                    "timestamp": now,
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "prompt",
                    "content": "hello",
                }
            ],
        }
        with mock.patch("orxaq_autonomy.dashboard.conversations_snapshot", return_value=conv_payload), mock.patch(
            "orxaq_autonomy.dashboard.monitor_snapshot",
            side_effect=RuntimeError("monitor unavailable"),
        ):
            payload = dashboard._safe_daw_snapshot(cfg, window_sec=120, lines=200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tempo_bpm"], 120)
        self.assertEqual(payload["prompt_midi_events"], 1)
        self.assertTrue(payload["tracks"])


if __name__ == "__main__":
    unittest.main()
