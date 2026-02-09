import pathlib
import sys
import json
import tempfile
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
        self.assertIn("conversation_source_errors", html)
        self.assertIn("conversation_source=", html)
        self.assertIn("source_error:", html)
        self.assertIn("suppressed_source_errors", html)
        self.assertIn("stopped=${payload.stopped_count || 0} failed=${failed}", html)
        self.assertIn("convOwner", html)
        self.assertIn("conversationSources", html)
        self.assertIn("conversationPath", html)
        self.assertIn("laneStatusPath", html)
        self.assertIn("include_conversations", html)
        self.assertIn("conversation_lines", html)
        self.assertIn("fallbackLanePayloadFromMonitor", html)
        self.assertIn("lane endpoint:", html)
        self.assertIn("filterFallbackConversationEvents", html)
        self.assertIn("fallbackConversationPayloadFromMonitor", html)
        self.assertIn("fallbackConversationPayloadFromCache", html)
        self.assertIn("lastSuccessfulMonitor", html)
        self.assertIn("lastSuccessfulLanePayload", html)
        self.assertIn("lastSuccessfulConversationPayload", html)
        self.assertIn("lastSuccessfulDawPayload", html)
        self.assertIn("stale cache used", html)
        self.assertIn("using cached snapshot", html)
        self.assertIn("buildConversationSourceMap", html)
        self.assertIn("buildLatestConversationByLane", html)
        self.assertIn("latest_conversation=", html)
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

    def test_safe_monitor_snapshot_reuses_lane_and_conversation_fallbacks(self):
        lane_payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": True,
            "partial": False,
            "errors": [],
            "running_count": 1,
            "total_count": 1,
            "health_counts": {"ok": 1},
            "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
            "lanes": [
                {
                    "id": "lane-a",
                    "owner": "codex",
                    "running": True,
                    "health": "ok",
                    "state_counts": {"pending": 0, "in_progress": 1, "done": 0, "blocked": 0, "unknown": 0},
                }
            ],
        }
        conv_payload = {
            "ok": True,
            "partial": False,
            "errors": [],
            "total_events": 1,
            "owner_counts": {"codex": 1},
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "lane healthy",
                }
            ],
            "sources": [{"lane_id": "lane-a", "ok": True, "event_count": 1}],
        }
        with mock.patch("orxaq_autonomy.dashboard.monitor_snapshot", side_effect=RuntimeError("monitor unavailable")), mock.patch(
            "orxaq_autonomy.dashboard._safe_lane_status_snapshot",
            return_value=lane_payload,
        ), mock.patch(
            "orxaq_autonomy.dashboard._safe_conversations_snapshot",
            return_value=conv_payload,
        ):
            payload = dashboard._safe_monitor_snapshot(mock.Mock())
        self.assertEqual(payload["lanes"]["running_count"], 1)
        self.assertEqual(payload["lanes"]["owner_counts"]["codex"]["running"], 1)
        self.assertEqual(payload["runtime"]["lane_operational_count"], 1)
        self.assertEqual(payload["runtime"]["lane_owner_health"]["codex"]["total"], 1)
        self.assertEqual(payload["conversations"]["recent_events"][0]["lane_id"], "lane-a")
        self.assertEqual(payload["lanes"]["lanes"][0]["conversation_event_count"], 1)
        self.assertEqual(payload["lanes"]["lanes"][0]["conversation_source_state"], "ok")
        self.assertEqual(payload["lanes"]["lanes"][0]["latest_conversation_event"]["event_type"], "status")
        self.assertTrue(payload["diagnostics"]["sources"]["lanes"]["ok"])
        self.assertTrue(payload["diagnostics"]["sources"]["conversations"]["ok"])
        self.assertFalse(payload["diagnostics"]["sources"]["monitor"]["ok"])

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

    def test_safe_lane_status_snapshot_uses_lane_plan_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "state").mkdir(parents=True, exist_ok=True)
            (root / "artifacts" / "autonomy").mkdir(parents=True, exist_ok=True)
            (root / "impl_repo").mkdir(parents=True, exist_ok=True)
            (root / "test_repo").mkdir(parents=True, exist_ok=True)
            (root / "config" / "tasks.json").write_text("[]\n", encoding="utf-8")
            (root / "config" / "objective.md").write_text("objective\n", encoding="utf-8")
            (root / "config" / "codex_result.schema.json").write_text("{}\n", encoding="utf-8")
            (root / "config" / "skill_protocol.json").write_text("{}\n", encoding="utf-8")
            (root / ".env.autonomy").write_text("OPENAI_API_KEY=test\nGEMINI_API_KEY=test\n", encoding="utf-8")
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = dashboard.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.dashboard.lane_status_snapshot",
                side_effect=RuntimeError("lane parse failed"),
            ):
                payload = dashboard._safe_lane_status_snapshot(cfg)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["lanes"][0]["id"], "lane-a")
        self.assertEqual(payload["lanes"][0]["owner"], "codex")
        self.assertEqual(payload["lanes"][0]["health"], "unknown")

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

    def test_filter_lane_status_payload_suppresses_unrelated_lane_errors(self):
        payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": False,
            "partial": True,
            "errors": ["lane-b: heartbeat stale"],
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
                {"id": "lane-b", "owner": "gemini", "running": True, "health": "stale"},
            ],
        }
        filtered = dashboard._filter_lane_status_payload(payload, lane_id="lane-a")
        self.assertTrue(filtered["ok"])
        self.assertFalse(filtered["partial"])
        self.assertEqual(filtered["errors"], [])
        self.assertEqual(filtered["suppressed_errors"], ["lane-b: heartbeat stale"])
        self.assertEqual(filtered["total_count"], 1)

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

    def test_filter_lane_status_payload_keeps_global_errors_for_requested_lane(self):
        payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": False,
            "partial": True,
            "errors": ["lane status source unavailable"],
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
            ],
        }
        filtered = dashboard._filter_lane_status_payload(payload, lane_id="lane-a")
        self.assertFalse(filtered["ok"])
        self.assertTrue(filtered["partial"])
        self.assertEqual(filtered["suppressed_errors"], [])
        self.assertIn("lane status source unavailable", filtered["errors"][0])

    def test_filter_lane_status_payload_keeps_colon_global_errors_for_requested_lane(self):
        payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": False,
            "partial": True,
            "errors": ["lane status source: timeout", "lane-b: heartbeat stale"],
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
                {"id": "lane-b", "owner": "gemini", "running": True, "health": "stale"},
            ],
        }
        filtered = dashboard._filter_lane_status_payload(payload, lane_id="lane-a")
        self.assertFalse(filtered["ok"])
        self.assertTrue(filtered["partial"])
        self.assertEqual(filtered["errors"], ["lane status source: timeout"])
        self.assertEqual(filtered["suppressed_errors"], ["lane-b: heartbeat stale"])

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

    def test_filter_conversation_payload_for_lane_suppresses_unrelated_lane_errors(self):
        payload = {
            "ok": False,
            "partial": True,
            "errors": ["lane-b stream unavailable"],
            "sources": [
                {"lane_id": "", "kind": "primary", "ok": True, "error": "", "event_count": 2},
                {"lane_id": "lane-a", "kind": "lane", "ok": True, "error": "", "event_count": 1},
                {"lane_id": "lane-b", "kind": "lane", "ok": False, "error": "lane-b stream unavailable", "event_count": 0},
            ],
        }
        filtered = dashboard._filter_conversation_payload_for_lane(payload, lane_id="lane-a")
        self.assertTrue(filtered["ok"])
        self.assertFalse(filtered["partial"])
        self.assertEqual(filtered["errors"], [])
        self.assertEqual(len(filtered["sources"]), 2)
        self.assertEqual(filtered["suppressed_source_count"], 1)
        self.assertEqual(filtered["suppressed_source_error_count"], 1)

    def test_filter_conversation_payload_for_lane_suppresses_path_prefixed_lane_errors(self):
        payload = {
            "ok": False,
            "partial": True,
            "errors": ["/tmp/lanes/lane-b/conversations.ndjson: lane-b stream unavailable"],
            "sources": [
                {"lane_id": "", "kind": "primary", "ok": True, "error": "", "event_count": 2},
                {
                    "lane_id": "lane-a",
                    "kind": "lane",
                    "path": "/tmp/lanes/lane-a/conversations.ndjson",
                    "resolved_path": "/tmp/lanes/lane-a/conversations.ndjson",
                    "ok": True,
                    "error": "",
                    "event_count": 1,
                },
                {
                    "lane_id": "lane-b",
                    "kind": "lane",
                    "path": "/tmp/lanes/lane-b/conversations.ndjson",
                    "resolved_path": "/tmp/lanes/lane-b/conversations.ndjson",
                    "ok": False,
                    "error": "lane-b stream unavailable",
                    "event_count": 0,
                },
            ],
        }
        filtered = dashboard._filter_conversation_payload_for_lane(payload, lane_id="lane-a")
        self.assertTrue(filtered["ok"])
        self.assertFalse(filtered["partial"])
        self.assertEqual(filtered["errors"], [])
        self.assertEqual(len(filtered["sources"]), 2)
        self.assertEqual(filtered["suppressed_source_count"], 1)
        self.assertIn(
            "/tmp/lanes/lane-b/conversations.ndjson: lane-b stream unavailable",
            filtered["suppressed_source_errors"],
        )
        self.assertEqual(filtered["suppressed_source_error_count"], 2)

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

    def test_safe_conversations_snapshot_suppresses_unrelated_lane_source_failures(self):
        cfg = mock.Mock()
        cfg.conversation_log_file = pathlib.Path("/tmp/conversations.ndjson")
        source_payload = {
            "total_events": 2,
            "events": [
                {"owner": "codex", "lane_id": "lane-a", "event_type": "status", "content": "alpha"},
                {"owner": "gemini", "lane_id": "lane-b", "event_type": "status", "content": "beta"},
            ],
            "owner_counts": {"codex": 1, "gemini": 1},
            "sources": [
                {"lane_id": "", "kind": "primary", "ok": True, "error": "", "event_count": 2},
                {"lane_id": "lane-a", "kind": "lane", "ok": True, "error": "", "event_count": 1},
                {
                    "lane_id": "lane-b",
                    "kind": "lane",
                    "ok": False,
                    "error": "lane-b stream unavailable",
                    "event_count": 0,
                },
            ],
            "partial": True,
            "ok": False,
            "errors": ["lane-b stream unavailable"],
        }
        with mock.patch("orxaq_autonomy.dashboard.conversations_snapshot", return_value=source_payload):
            payload = dashboard._safe_conversations_snapshot(
                cfg,
                lines=200,
                lane_id="lane-a",
            )
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["partial"])
        self.assertEqual(payload["total_events"], 1)
        self.assertEqual(payload["events"][0]["lane_id"], "lane-a")
        self.assertEqual(payload["suppressed_source_error_count"], 1)
        self.assertEqual(len(payload["sources"]), 2)
        self.assertEqual(payload["errors"], [])

    def test_lane_conversation_rollup_tracks_latest_event_and_source_health(self):
        payload = {
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "older",
                },
                {
                    "timestamp": "2026-01-01T00:00:02+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "message",
                    "content": "newer",
                },
            ],
            "sources": [
                {
                    "lane_id": "lane-a",
                    "ok": False,
                    "error": "lane file unavailable",
                    "event_count": 1,
                    "missing": True,
                    "recoverable_missing": False,
                    "fallback_used": True,
                }
            ],
        }
        rollup = dashboard._lane_conversation_rollup(payload)
        self.assertIn("lane-a", rollup)
        lane_rollup = rollup["lane-a"]
        self.assertEqual(lane_rollup["source_state"], "error")
        self.assertEqual(lane_rollup["source_error_count"], 1)
        self.assertEqual(lane_rollup["missing_count"], 1)
        self.assertEqual(lane_rollup["fallback_count"], 1)
        self.assertEqual(lane_rollup["event_count"], 2)
        self.assertEqual(lane_rollup["latest_event"]["event_type"], "message")
        self.assertEqual(lane_rollup["latest_event"]["content"], "newer")

    def test_lane_conversation_rollup_infers_owner_from_source_when_events_missing_owner(self):
        payload = {
            "events": [],
            "sources": [
                {"lane_id": "lane-a", "owner": "codex", "ok": True, "error": "", "event_count": 0},
            ],
        }
        rollup = dashboard._lane_conversation_rollup(payload)
        self.assertIn("lane-a", rollup)
        self.assertEqual(rollup["lane-a"]["owner"], "codex")

    def test_augment_lane_payload_with_conversation_rollup_embeds_lane_fields(self):
        lane_payload = {
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
                {"id": "lane-b", "owner": "gemini", "running": False, "health": "stopped"},
            ]
        }
        conversation_payload = {
            "ok": False,
            "partial": True,
            "errors": ["lane-b stream unavailable"],
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "ready",
                }
            ],
            "sources": [
                {"lane_id": "lane-a", "ok": True, "error": "", "event_count": 1},
                {"lane_id": "lane-b", "ok": False, "error": "lane-b stream unavailable", "event_count": 0},
            ],
        }
        enriched = dashboard._augment_lane_payload_with_conversation_rollup(lane_payload, conversation_payload)
        lane_a = next(item for item in enriched["lanes"] if item["id"] == "lane-a")
        lane_b = next(item for item in enriched["lanes"] if item["id"] == "lane-b")
        self.assertEqual(lane_a["conversation_source_state"], "ok")
        self.assertEqual(lane_a["conversation_event_count"], 1)
        self.assertEqual(lane_a["latest_conversation_event"]["event_type"], "status")
        self.assertEqual(lane_b["conversation_source_state"], "error")
        self.assertEqual(lane_b["conversation_source_error_count"], 1)
        self.assertEqual(enriched["conversation_errors"], ["lane-b stream unavailable"])
        self.assertTrue(enriched["conversation_partial"])
        self.assertFalse(enriched["conversation_ok"])

    def test_augment_lane_payload_with_conversation_rollup_recovers_missing_lane(self):
        lane_payload = {
            "requested_lane": "lane-a",
            "errors": ["Unknown lane id 'lane-a'. Update /tmp/lanes.json."],
            "lanes": [],
            "health_counts": {},
            "owner_counts": {},
            "ok": False,
            "partial": True,
        }
        conversation_payload = {
            "ok": True,
            "partial": False,
            "errors": [],
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:01+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "ready",
                }
            ],
            "sources": [
                {"lane_id": "lane-a", "ok": True, "error": "", "event_count": 1},
            ],
        }
        enriched = dashboard._augment_lane_payload_with_conversation_rollup(lane_payload, conversation_payload)
        self.assertEqual(enriched["recovered_lane_count"], 1)
        self.assertEqual(enriched["recovered_lanes"], ["lane-a"])
        self.assertEqual(enriched["total_count"], 1)
        lane = enriched["lanes"][0]
        self.assertEqual(lane["id"], "lane-a")
        self.assertEqual(lane["owner"], "codex")
        self.assertTrue(lane["conversation_lane_fallback"])
        self.assertEqual(lane["conversation_source_state"], "ok")
        self.assertEqual(
            enriched["errors"],
            ["Lane status missing for 'lane-a'; using conversation-derived fallback."],
        )
        self.assertTrue(enriched["partial"])
        self.assertFalse(enriched["ok"])

    def test_augment_lane_payload_with_conversation_rollup_recovers_owner_from_source_metadata(self):
        lane_payload = {
            "requested_lane": "lane-a",
            "errors": ["Unknown lane id 'lane-a'. Update /tmp/lanes.json."],
            "lanes": [],
            "health_counts": {},
            "owner_counts": {},
            "ok": False,
            "partial": True,
        }
        conversation_payload = {
            "ok": True,
            "partial": False,
            "errors": [],
            "events": [],
            "sources": [
                {"lane_id": "lane-a", "owner": "codex", "ok": True, "error": "", "event_count": 0},
            ],
        }
        enriched = dashboard._augment_lane_payload_with_conversation_rollup(lane_payload, conversation_payload)
        self.assertEqual(enriched["recovered_lane_count"], 1)
        self.assertEqual(enriched["lanes"][0]["id"], "lane-a")
        self.assertEqual(enriched["lanes"][0]["owner"], "codex")

    def test_augment_lane_payload_with_conversation_rollup_clears_unavailable_error_for_recovered_lane(self):
        lane_payload = {
            "requested_lane": "lane-a",
            "errors": [
                "lane status source unavailable",
                "Requested lane 'lane-a' is unavailable because lane status sources failed.",
            ],
            "lanes": [],
            "health_counts": {},
            "owner_counts": {},
            "ok": False,
            "partial": True,
        }
        conversation_payload = {
            "ok": True,
            "partial": False,
            "errors": [],
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:01+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "ready",
                }
            ],
            "sources": [
                {"lane_id": "lane-a", "ok": True, "error": "", "event_count": 1},
            ],
        }
        enriched = dashboard._augment_lane_payload_with_conversation_rollup(lane_payload, conversation_payload)
        self.assertEqual(enriched["recovered_lane_count"], 1)
        self.assertNotIn(
            "Requested lane 'lane-a' is unavailable because lane status sources failed.",
            enriched["errors"],
        )
        self.assertIn("lane status source unavailable", enriched["errors"])
        self.assertIn(
            "Lane status missing for 'lane-a'; using conversation-derived fallback.",
            enriched["errors"],
        )

    def test_augment_lane_payload_with_conversation_rollup_recovers_partial_missing_lanes(self):
        lane_payload = {
            "requested_lane": "all",
            "errors": ["lane status source unavailable"],
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
            ],
            "health_counts": {"ok": 1},
            "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
            "ok": False,
            "partial": True,
        }
        conversation_payload = {
            "ok": True,
            "partial": False,
            "errors": [],
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:01+00:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "lane-a healthy",
                },
                {
                    "timestamp": "2026-01-01T00:00:02+00:00",
                    "owner": "gemini",
                    "lane_id": "lane-b",
                    "event_type": "status",
                    "content": "lane-b recovered",
                },
            ],
            "sources": [
                {"lane_id": "lane-a", "ok": True, "error": "", "event_count": 1},
                {"lane_id": "lane-b", "ok": True, "error": "", "event_count": 1},
            ],
        }
        enriched = dashboard._augment_lane_payload_with_conversation_rollup(lane_payload, conversation_payload)
        self.assertEqual(enriched["recovered_lane_count"], 1)
        self.assertEqual(enriched["recovered_lanes"], ["lane-b"])
        self.assertEqual(enriched["total_count"], 2)
        lane_b = next(item for item in enriched["lanes"] if item["id"] == "lane-b")
        self.assertEqual(lane_b["owner"], "gemini")
        self.assertTrue(lane_b["conversation_lane_fallback"])
        self.assertIn(
            "Lane status missing for 'lane-b'; using conversation-derived fallback.",
            enriched["errors"],
        )
        self.assertTrue(enriched["partial"])
        self.assertFalse(enriched["ok"])

    def test_augment_lane_payload_with_conversation_rollup_does_not_recover_when_status_is_healthy(self):
        lane_payload = {
            "requested_lane": "all",
            "errors": [],
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
            ],
            "health_counts": {"ok": 1},
            "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
            "ok": True,
            "partial": False,
        }
        conversation_payload = {
            "ok": True,
            "partial": False,
            "errors": [],
            "events": [
                {
                    "timestamp": "2026-01-01T00:00:02+00:00",
                    "owner": "gemini",
                    "lane_id": "lane-b",
                    "event_type": "status",
                    "content": "historical event",
                }
            ],
            "sources": [
                {"lane_id": "lane-b", "ok": True, "error": "", "event_count": 1},
            ],
        }
        enriched = dashboard._augment_lane_payload_with_conversation_rollup(lane_payload, conversation_payload)
        self.assertEqual(enriched["recovered_lane_count"], 0)
        self.assertEqual(enriched["recovered_lanes"], [])
        self.assertEqual(enriched["total_count"], 1)
        self.assertEqual([item["id"] for item in enriched["lanes"]], ["lane-a"])

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
