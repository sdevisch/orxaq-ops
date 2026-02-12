import pathlib
import sys
import json
import os
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
    def test_is_client_disconnect_error_detects_known_socket_errors(self):
        self.assertTrue(dashboard._is_client_disconnect_error(BrokenPipeError("broken pipe")))
        self.assertTrue(dashboard._is_client_disconnect_error(ConnectionResetError("Connection reset by peer")))
        self.assertTrue(dashboard._is_client_disconnect_error(ConnectionAbortedError("Software caused connection abort")))

    def test_is_client_disconnect_error_detects_message_only_variants(self):
        self.assertTrue(dashboard._is_client_disconnect_error(OSError("Broken pipe")))
        self.assertTrue(dashboard._is_client_disconnect_error(OSError("connection reset by peer")))
        self.assertTrue(dashboard._is_client_disconnect_error(RuntimeError("[Errno 32] Broken pipe")))
        self.assertFalse(dashboard._is_client_disconnect_error(RuntimeError("unexpected dashboard error")))

    def test_dashboard_html_contains_title_and_refresh(self):
        html = dashboard._dashboard_html(7)
        self.assertIn("Orxaq Autonomy Monitor", html)
        self.assertIn("serverVersion", html)
        self.assertIn("trustBanner", html)
        self.assertIn("operatorFocusHero", html)
        self.assertIn("operatorIncident", html)
        self.assertIn("operatorActions", html)
        self.assertIn("operatorActionButtons", html)
        self.assertIn("advancedToggle", html)
        self.assertIn("nocToggle", html)
        self.assertIn("operatorFocus", html)
        self.assertIn("Live Command Deck", html)
        self.assertIn("commandDeckStatus", html)
        self.assertIn("commandDeckMode", html)
        self.assertIn("commandDeckSpend", html)
        self.assertIn("renderCommandDeck", html)
        self.assertIn("renderCommandDeckModes", html)
        self.assertIn("renderTrustBanner", html)
        self.assertIn("renderOperatorFocus", html)
        self.assertIn("degradedLaneCount", html)
        self.assertIn("const REFRESH_MS = 7000", html)
        self.assertIn("/api/monitor", html)
        self.assertIn("/api/lanes", html)
        self.assertIn("/api/conversations", html)
        self.assertIn("/api/lanes/action", html)
        self.assertIn("/api/watchdog", html)
        self.assertIn("/api/collab-runtime", html)
        self.assertIn("/api/version", html)
        self.assertIn("Routing Monitor", html)
        self.assertIn('data-tab="routing"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn('role="tab"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn('aria-controls="panelOverview"', html)
        self.assertIn('aria-controls="panelRouting"', html)
        self.assertIn('aria-labelledby="tabOverview"', html)
        self.assertIn('aria-labelledby="tabRouting"', html)
        self.assertIn("Parallel Lanes", html)
        self.assertIn("Conversations", html)
        self.assertIn("Autonomous PID Watchdog", html)
        self.assertIn("Collaborative Agent Runtime", html)
        self.assertIn("Routing Overview", html)
        self.assertIn("Provider Routing Health", html)
        self.assertIn("Lane Router Configuration", html)
        self.assertIn("Recent Routing Decisions", html)
        self.assertIn("routingSummary", html)
        self.assertIn("routingDecisionFeed", html)
        self.assertIn("routingProviderBody", html)
        self.assertIn("routingLaneBody", html)
        self.assertIn("routingEstimatedTokens", html)
        self.assertIn("routingBlendedCostPerM", html)
        self.assertIn("Blended Est. $ / 1M", html)
        self.assertIn("collabSummary", html)
        self.assertIn("collabAnomaly", html)
        self.assertIn("collabTableBody", html)
        self.assertIn("live-indicator", html)
        self.assertIn("signalLedMarkup", html)
        self.assertIn("sparklineMarkup", html)
        self.assertIn("attentionBadgeMarkup", html)
        self.assertIn("watchdogSummary", html)
        self.assertIn("watchdogEvents", html)
        self.assertIn("Cost &amp; Quality", html)
        self.assertIn("metricsSummary", html)
        self.assertIn("metricsEconomics", html)
        self.assertIn("metricsCostWindows", html)
        self.assertIn("metricsFreshness", html)
        self.assertIn("metricsSplit", html)
        self.assertIn("metricsTrend", html)
        self.assertIn("excitingStat", html)
        self.assertIn("cost_windows_usd", html)
        self.assertIn("swarm_daily_budget", html)
        self.assertIn("swarmBudget", html)
        self.assertIn("provider_cost_30d", html)
        self.assertIn("model_cost_30d", html)
        self.assertIn("data_freshness", html)
        self.assertIn("source_of_truth", html)
        self.assertIn("authoritative_cost_available", html)
        self.assertIn("Est. $ / 1M", html)
        self.assertIn("Resilience Diagnostics", html)
        self.assertIn("renderDiagnostics", html)
        self.assertIn("lane_agents", html)
        self.assertIn("idle (lane mode)", html)
        self.assertIn("operational", html)
        self.assertIn("fabric", html)
        self.assertIn("optimization_recommendations", html)
        self.assertIn("laneActionStatus", html)
        self.assertIn("laneStatus", html)
        self.assertIn("laneOwnerSummary", html)
        self.assertIn("source_errors", html)
        self.assertIn("conversation_source_errors", html)
        self.assertIn("recovered: ${recoveredLanes}", html)
        self.assertIn("conversation_source=", html)
        self.assertIn("source_error:", html)
        self.assertIn("suppressed_source_errors", html)
        self.assertIn("stopped=${payload.stopped_count || 0} failed=${failed}", html)
        self.assertIn("status failed:", html)
        self.assertIn("recovered=${recovered}", html)
        self.assertIn("inspectLaneStatus", html)
        self.assertIn("convOwner", html)
        self.assertIn("conversationSources", html)
        self.assertIn("conversationPath", html)
        self.assertIn("laneStatusPath", html)
        self.assertIn("include_conversations", html)
        self.assertIn("conversation_lines", html)
        self.assertIn("fallbackLanePayloadFromMonitor", html)
        self.assertIn("lane endpoint:", html)
        self.assertIn("filterFallbackConversationEvents", html)
        self.assertIn("filterFallbackConversationSources", html)
        self.assertIn("fallbackConversationPayloadFromMonitor", html)
        self.assertIn("fallbackConversationPayloadFromCache", html)
        self.assertIn("lastSuccessfulMonitor", html)
        self.assertIn("lastSuccessfulLanePayload", html)
        self.assertIn("lastSuccessfulConversationPayload", html)
        self.assertIn("lastSuccessfulDawPayload", html)
        self.assertIn("lastSuccessfulWatchdogPayload", html)
        self.assertIn("lastSuccessfulCollabPayload", html)
        self.assertIn("lastSuccessfulRoutingPayload", html)
        self.assertIn("ROUTING_DECISION_TAIL", html)
        self.assertIn("renderWatchdog", html)
        self.assertIn("renderCollaboratorRuntime", html)
        self.assertIn("renderRouting", html)
        self.assertIn("routingDecisionPath", html)
        self.assertIn("routing_decisions_endpoint", html)
        self.assertIn("setActiveTab", html)
        self.assertIn("initTabs", html)
        self.assertIn("confirmLaneAction", html)
        self.assertIn('health: String(byId("laneFilterHealth").value || "all").trim().toLowerCase()', html)
        self.assertIn("window.confirm(`Confirm stop for ${scope}?`)", html)
        self.assertIn("focus-incident", html)
        self.assertIn("focus-action-btn", html)
        self.assertIn("advanced-card", html)
        self.assertIn("advanced-mode", html)
        self.assertIn("noc-mode", html)
        self.assertIn("critical-card", html)
        self.assertIn("runFocusAction", html)
        self.assertIn("applyViewModes", html)
        self.assertIn("syncModeQueryParam", html)
        self.assertIn('url.searchParams.set("mode", "noc")', html)
        self.assertIn("critical operator attention required", html)
        self.assertIn("actionPlan.slice(0, 3)", html)
        self.assertIn('const isForward = key === "ArrowRight" || key === "ArrowDown";', html)
        self.assertIn('const isBackward = key === "ArrowLeft" || key === "ArrowUp";', html)
        self.assertIn('const isActivate = key === " " || key === "Enter" || key === "Spacebar";', html)
        self.assertIn("panel.hidden = !isActive;", html)
        self.assertIn("watchdog endpoint unavailable", html)
        self.assertIn("collaboration runtime endpoint unavailable", html)
        self.assertIn("routing decisions endpoint unavailable", html)
        self.assertIn("USER_TIMEZONE", html)
        self.assertIn("formatTimestamp", html)
        self.assertIn("stale cache used", html)
        self.assertIn("using cached snapshot", html)
        self.assertIn("suppressed_source_count", html)
        self.assertIn("buildConversationSourceMap", html)
        self.assertIn("buildLatestConversationByLane", html)
        self.assertIn("eventTimestampInfo", html)
        self.assertIn("latest_conversation=", html)
        self.assertIn("FETCH_TIMEOUT_MS", html)
        self.assertIn("timeout after", html)
        self.assertIn("const rawBody = await response.text();", html)
        self.assertIn("HTTP ${response.status}: ${detail}", html)
        self.assertIn("result.payload && result.payload.error", html)
        self.assertIn("Completed (24h)", html)
        self.assertIn("completed_24h", html)
        self.assertIn("completed24hSummary", html)
        self.assertIn("todoToggle", html)
        self.assertIn("todoVisibility", html)
        self.assertIn("toggleTodoVisibility", html)
        self.assertIn("active_watch_visible:", html)
        self.assertIn('<option value="all" selected>health: all</option>', html)
        self.assertIn('for="laneTarget">Lane target</label>', html)
        self.assertIn('for="laneFilterOwner">Owner filter</label>', html)
        self.assertIn('for="laneFilterHealth">Health filter</label>', html)
        self.assertIn('for="laneSortBy">Sort lanes</label>', html)
        self.assertIn('for="laneFilterText">Lane text filter</label>', html)
        self.assertIn('for="convOwner">Owner</label>', html)
        self.assertIn('for="convLane">Lane ID</label>', html)
        self.assertIn('for="convType">Event type</label>', html)
        self.assertIn('for="convTail">Tail events</label>', html)
        self.assertIn('for="convContains">Contains text</label>', html)

    def test_dashboard_version_payload_contains_build_identity(self):
        cfg = mock.Mock()
        cfg.root_dir = pathlib.Path("/tmp/orxaq-ops")
        with mock.patch("orxaq_autonomy.dashboard._dashboard_build_id", return_value="abc123def456"), mock.patch.dict(
            os.environ,
            {"ORXAQ_DASHBOARD_VERSION_SECRET": "test-secret"},
            clear=False,
        ):
            payload = dashboard._dashboard_version_payload(
                cfg,
                requested_port=8765,
                bound_port=8766,
                refresh_sec=5,
                started_at="2026-02-10T00:00:00+00:00",
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["build_id"], "abc123def456")
        self.assertEqual(payload["root_dir"], "/tmp/orxaq-ops")
        self.assertEqual(payload["requested_port"], 8765)
        self.assertEqual(payload["bound_port"], 8766)
        self.assertEqual(payload["refresh_sec"], 5)
        self.assertEqual(payload["started_at"], "2026-02-10T00:00:00+00:00")
        self.assertEqual(len(payload["signature"]), 16)

    def test_dashboard_html_fallback_lane_filters_match_case_insensitively(self):
        html = dashboard._dashboard_html(7)
        self.assertIn("const requestedLaneLower = requestedLaneRaw.toLowerCase();", html)
        self.assertIn("toLowerCase() === resolvedRequestedLaneLower", html)
        self.assertIn("const laneFilterLower = laneFilter.toLowerCase();", html)
        self.assertIn("sourceLane.toLowerCase() !== laneFilterLower", html)
        self.assertIn("sourceLane.toLowerCase() === laneFilterLower", html)

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
        self.assertIn("swarm_daily_budget", payload["response_metrics"])
        self.assertEqual(payload["progress"]["completed_last_24h"], 0)
        self.assertEqual(payload["progress"]["completed_last_24h_unique_tasks"], 0)
        self.assertEqual(payload["progress"]["completed_last_24h_by_owner"], {})

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

    def test_safe_monitor_snapshot_preserves_status_when_monitor_source_fails(self):
        lane_payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": True,
            "partial": False,
            "errors": [],
            "running_count": 0,
            "total_count": 1,
            "health_counts": {"idle": 1},
            "owner_counts": {"codex": {"total": 1, "running": 0, "healthy": 1, "degraded": 0}},
            "lanes": [
                {
                    "id": "lane-a",
                    "owner": "codex",
                    "running": False,
                    "health": "idle",
                    "state_counts": {"pending": 1, "in_progress": 0, "done": 0, "blocked": 0, "unknown": 0},
                }
            ],
        }
        conv_payload = {
            "ok": True,
            "partial": False,
            "errors": [],
            "total_events": 0,
            "owner_counts": {},
            "events": [],
            "sources": [],
        }
        status_payload = {
            "supervisor_running": True,
            "runner_running": True,
            "heartbeat_age_sec": 14,
            "heartbeat_stale_threshold_sec": 180,
            "runner_pid": 321,
            "supervisor_pid": 654,
        }
        with mock.patch("orxaq_autonomy.dashboard.monitor_snapshot", side_effect=RuntimeError("monitor unavailable")), mock.patch(
            "orxaq_autonomy.dashboard.status_snapshot",
            return_value=status_payload,
        ), mock.patch(
            "orxaq_autonomy.dashboard._safe_lane_status_snapshot",
            return_value=lane_payload,
        ), mock.patch(
            "orxaq_autonomy.dashboard._safe_conversations_snapshot",
            return_value=conv_payload,
        ):
            payload = dashboard._safe_monitor_snapshot(mock.Mock())
        self.assertTrue(payload["status"]["supervisor_running"])
        self.assertTrue(payload["status"]["runner_running"])
        self.assertEqual(payload["status"]["heartbeat_age_sec"], 14)
        self.assertEqual(payload["status"]["runner_pid"], 321)
        self.assertTrue(payload["runtime"]["primary_runner_running"])
        self.assertTrue(payload["runtime"]["effective_agents_running"])
        self.assertTrue(payload["diagnostics"]["sources"]["status"]["ok"])

    def test_safe_monitor_snapshot_sorts_fallback_conversations_by_utc_timestamp(self):
        lane_payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": False,
            "partial": True,
            "errors": ["lane status unavailable"],
            "running_count": 0,
            "total_count": 0,
            "health_counts": {},
            "owner_counts": {},
            "lanes": [],
        }
        conv_payload = {
            "ok": False,
            "partial": True,
            "errors": ["primary conversation stream degraded"],
            "total_events": 4,
            "owner_counts": {"codex": 2, "gemini": 1, "claude": 1},
            "events": [
                {
                    "timestamp": "abc-invalid-a",
                    "owner": "claude",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "invalid-first",
                },
                {
                    "timestamp": "2026-01-01T00:45:00+00:00",
                    "owner": "gemini",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "later",
                },
                {
                    "timestamp": "2026-01-01T01:30:00+01:00",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "earlier",
                },
                {
                    "timestamp": "definitely-invalid-z",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "invalid-last",
                },
            ],
            "sources": [
                {
                    "kind": "primary",
                    "resolved_kind": "primary",
                    "lane_id": "",
                    "owner": "",
                    "ok": False,
                    "missing": False,
                    "recoverable_missing": False,
                    "fallback_used": False,
                    "error": "primary stream unavailable",
                    "event_count": 0,
                },
                {
                    "kind": "lane",
                    "resolved_kind": "lane_events",
                    "lane_id": "lane-a",
                    "owner": "codex",
                    "ok": True,
                    "missing": True,
                    "recoverable_missing": True,
                    "fallback_used": True,
                    "error": "",
                    "event_count": 3,
                },
            ],
        }
        with mock.patch("orxaq_autonomy.dashboard.monitor_snapshot", side_effect=RuntimeError("monitor unavailable")), mock.patch(
            "orxaq_autonomy.dashboard._safe_lane_status_snapshot",
            return_value=lane_payload,
        ), mock.patch(
            "orxaq_autonomy.dashboard._safe_conversations_snapshot",
            return_value=conv_payload,
        ):
            payload = dashboard._safe_monitor_snapshot(mock.Mock())
        self.assertEqual(payload["conversations"]["recent_events"][-1]["content"], "later")
        self.assertEqual(payload["conversations"]["latest"]["content"], "later")
        self.assertEqual(payload["conversations"]["source_error_count"], 1)
        self.assertEqual(payload["conversations"]["source_missing_count"], 1)
        self.assertEqual(payload["conversations"]["source_recoverable_missing_count"], 1)
        self.assertEqual(payload["conversations"]["source_fallback_count"], 1)

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

    def test_filter_lane_status_payload_matches_lane_case_insensitively(self):
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
        filtered = dashboard._filter_lane_status_payload(payload, lane_id="LANE-A")
        self.assertEqual(filtered["requested_lane"], "lane-a")
        self.assertEqual(filtered["total_count"], 1)
        self.assertEqual(filtered["lanes"][0]["id"], "lane-a")
        self.assertTrue(filtered["ok"])
        self.assertFalse(filtered["partial"])

    def test_filter_lane_status_payload_normalizes_missing_lane_fields(self):
        payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": True,
            "partial": False,
            "errors": [],
            "lanes": [
                {"id": "lane-a"},
                "bad-entry",
                {"owner": "gemini", "running": 1},
            ],
        }
        filtered = dashboard._filter_lane_status_payload(payload, lane_id="")
        self.assertEqual(filtered["total_count"], 2)
        self.assertEqual(filtered["lanes"][0]["id"], "lane-a")
        self.assertEqual(filtered["lanes"][0]["owner"], "unknown")
        self.assertEqual(filtered["lanes"][0]["health"], "unknown")
        self.assertEqual(filtered["lanes"][0]["heartbeat_age_sec"], -1)
        self.assertEqual(filtered["lanes"][1]["id"], "unknown")
        self.assertEqual(filtered["lanes"][1]["owner"], "gemini")

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

    def test_filter_lane_status_payload_normalizes_scalar_errors(self):
        payload = {
            "lanes_file": "/tmp/lanes.json",
            "ok": False,
            "partial": True,
            "errors": "lane status source unavailable",
            "lanes": [
                {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
            ],
        }
        filtered = dashboard._filter_lane_status_payload(payload, lane_id="lane-a")
        self.assertFalse(filtered["ok"])
        self.assertTrue(filtered["partial"])
        self.assertEqual(filtered["suppressed_errors"], [])
        self.assertEqual(filtered["errors"], ["lane status source unavailable"])

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

    def test_safe_watchdog_snapshot_degrades_when_state_file_missing(self):
        cfg = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg.artifacts_dir = root
            state_file = root / "missing-state.json"
            history_file = root / "missing-history.ndjson"
            with mock.patch.dict(
                "os.environ",
                {
                    "ORXAQ_AUTONOMY_PROCESS_WATCHDOG_STATE_FILE": str(state_file),
                    "ORXAQ_AUTONOMY_PROCESS_WATCHDOG_HISTORY_FILE": str(history_file),
                },
            ):
                payload = dashboard._safe_watchdog_snapshot(cfg, events=5)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial"])
        self.assertFalse(payload["state_exists"])
        self.assertFalse(payload["history_exists"])
        self.assertEqual(payload["total_processes"], 0)
        self.assertEqual(payload["problematic_count"], 0)
        self.assertIn("watchdog state file not found", payload["errors"][0])

    def test_safe_watchdog_snapshot_reads_state_and_recent_events(self):
        cfg = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg.artifacts_dir = root
            state_file = root / "watchdog-state.json"
            history_file = root / "watchdog-history.ndjson"
            state_file.write_text(
                json.dumps(
                    {
                        "runs_total": 7,
                        "last_run_at": "2026-01-01T00:00:04Z",
                        "processes": {
                            "orxaq-dashboard": {
                                "last_pid": 222,
                                "last_status": "restart_failed",
                                "last_checked_at": "2026-01-01T00:00:04Z",
                                "last_restart_at": "2026-01-01T00:00:03Z",
                                "last_restart_rc": 1,
                                "last_reason": "bind error",
                                "checks_total": 5,
                                "healthy_checks": 2,
                                "unhealthy_checks": 3,
                                "restart_attempts": 3,
                                "restart_successes": 1,
                                "restart_failures": 2,
                            },
                            "orxaq-supervisor": {
                                "last_pid": 111,
                                "last_status": "healthy",
                                "last_checked_at": "2026-01-01T00:00:04Z",
                                "checks_total": 5,
                                "healthy_checks": 5,
                                "unhealthy_checks": 0,
                                "restart_attempts": 0,
                                "restart_successes": 0,
                                "restart_failures": 0,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            history_file.write_text(
                "\n".join(
                    [
                        '{"time":"2026-01-01T00:00:02Z","id":"orxaq-supervisor","status":"healthy","pid":111,"reason":"","restart_returncode":null}',
                        "not-json",
                        '{"time":"2026-01-01T00:00:03Z","id":"orxaq-dashboard","status":"restarted","pid":333,"reason":"restarted","restart_returncode":0}',
                        '{"time":"2026-01-01T00:00:04Z","id":"orxaq-dashboard","status":"restart_failed","pid":222,"reason":"bind error","restart_returncode":1}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "ORXAQ_AUTONOMY_PROCESS_WATCHDOG_STATE_FILE": str(state_file),
                    "ORXAQ_AUTONOMY_PROCESS_WATCHDOG_HISTORY_FILE": str(history_file),
                },
            ):
                payload = dashboard._safe_watchdog_snapshot(cfg, events=2)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["partial"])
        self.assertTrue(payload["state_exists"])
        self.assertTrue(payload["history_exists"])
        self.assertEqual(payload["runs_total"], 7)
        self.assertEqual(payload["total_processes"], 2)
        self.assertEqual(payload["healthy_count"], 1)
        self.assertEqual(payload["problematic_count"], 1)
        self.assertEqual(payload["problematic_ids"], ["orxaq-dashboard"])
        self.assertEqual(payload["restart_attempts_total"], 3)
        self.assertEqual(payload["restart_successes_total"], 1)
        self.assertEqual(payload["restart_failures_total"], 2)
        self.assertEqual(payload["state_file"], str(state_file.resolve()))
        self.assertEqual(payload["history_file"], str(history_file.resolve()))
        self.assertEqual(len(payload["recent_events"]), 2)
        self.assertEqual(payload["recent_events"][0]["status"], "restarted")
        self.assertEqual(payload["recent_events"][1]["status"], "restart_failed")

    def test_safe_watchdog_snapshot_prefers_newer_artifacts_state_file(self):
        cfg = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            home_dir = root / "home"
            artifacts_dir = root / "artifacts"
            home_watchdog_dir = home_dir / ".codex" / "autonomy"
            home_watchdog_dir.mkdir(parents=True)
            artifacts_dir.mkdir(parents=True)
            cfg.artifacts_dir = artifacts_dir

            home_state = home_watchdog_dir / "process-watchdog-state.json"
            artifacts_state = artifacts_dir / "process-watchdog-state.json"
            home_history = home_watchdog_dir / "process-watchdog-history.ndjson"
            artifacts_history = artifacts_dir / "process-watchdog-history.ndjson"

            home_state.write_text(
                json.dumps(
                    {
                        "runs_total": 1,
                        "last_run_at": "2026-01-01T00:00:01Z",
                        "processes": {
                            "orxaq-dashboard": {"last_pid": 101, "last_status": "healthy"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            artifacts_state.write_text(
                json.dumps(
                    {
                        "runs_total": 2,
                        "last_run_at": "2026-01-01T00:00:02Z",
                        "processes": {
                            "orxaq-dashboard": {"last_pid": 202, "last_status": "restarted"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            home_history.write_text(
                '{"time":"2026-01-01T00:00:01Z","id":"orxaq-dashboard","status":"healthy","pid":101}\n',
                encoding="utf-8",
            )
            artifacts_history.write_text(
                '{"time":"2026-01-01T00:00:02Z","id":"orxaq-dashboard","status":"restarted","pid":202}\n',
                encoding="utf-8",
            )

            os.utime(home_state, (1_700_000_000, 1_700_000_000))
            os.utime(home_history, (1_700_000_000, 1_700_000_000))
            os.utime(artifacts_state, (1_700_000_060, 1_700_000_060))
            os.utime(artifacts_history, (1_700_000_060, 1_700_000_060))

            with mock.patch("orxaq_autonomy.dashboard.Path.home", return_value=home_dir), mock.patch.dict(
                "os.environ",
                {},
                clear=True,
            ):
                payload = dashboard._safe_watchdog_snapshot(cfg, events=5)

        self.assertEqual(payload["state_file"], str(artifacts_state.resolve()))
        self.assertEqual(payload["history_file"], str(artifacts_history.resolve()))
        self.assertEqual(payload["runs_total"], 2)
        self.assertEqual(payload["processes"][0]["pid"], 202)
        self.assertEqual(payload["recent_events"][0]["pid"], 202)

    def test_safe_watchdog_snapshot_reads_underscore_manager_watchdog_files(self):
        cfg = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            home_dir = root / "home"
            artifacts_dir = root / "artifacts"
            home_watchdog_dir = home_dir / ".codex" / "autonomy"
            home_watchdog_dir.mkdir(parents=True)
            artifacts_dir.mkdir(parents=True)
            cfg.artifacts_dir = artifacts_dir

            home_state = home_watchdog_dir / "process-watchdog-state.json"
            home_history = home_watchdog_dir / "process-watchdog-history.ndjson"
            artifacts_state = artifacts_dir / "process_watchdog_state.json"
            artifacts_history = artifacts_dir / "process_watchdog_history.ndjson"

            home_state.write_text(
                json.dumps(
                    {
                        "runs_total": 1,
                        "last_run_at": "2026-01-01T00:00:01Z",
                        "processes": {"legacy-home": {"last_pid": 11, "last_status": "healthy"}},
                    }
                ),
                encoding="utf-8",
            )
            home_history.write_text(
                '{"time":"2026-01-01T00:00:01Z","id":"legacy-home","status":"healthy","pid":11}\n',
                encoding="utf-8",
            )
            artifacts_state.write_text(
                json.dumps(
                    {
                        "checks_total": 3,
                        "updated_at": "2026-01-01T00:00:03Z",
                        "processes": {
                            "runner": {
                                "last_pid": 303,
                                "last_status": "healthy",
                                "last_detail": "runner idle",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            artifacts_history.write_text(
                '{"time":"2026-01-01T00:00:03Z","id":"runner","status":"healthy","pid":303}\n',
                encoding="utf-8",
            )

            os.utime(home_state, (1_700_000_000, 1_700_000_000))
            os.utime(home_history, (1_700_000_000, 1_700_000_000))
            os.utime(artifacts_state, (1_700_000_180, 1_700_000_180))
            os.utime(artifacts_history, (1_700_000_180, 1_700_000_180))

            with mock.patch("orxaq_autonomy.dashboard.Path.home", return_value=home_dir), mock.patch.dict(
                "os.environ",
                {},
                clear=True,
            ):
                payload = dashboard._safe_watchdog_snapshot(cfg, events=5)

        self.assertEqual(payload["state_file"], str(artifacts_state.resolve()))
        self.assertEqual(payload["history_file"], str(artifacts_history.resolve()))
        self.assertEqual(payload["runs_total"], 3)
        self.assertEqual(payload["processes"][0]["id"], "runner")
        self.assertEqual(payload["processes"][0]["pid"], 303)
        self.assertEqual(payload["processes"][0]["reason"], "runner idle")

    def test_watchdog_path_prefers_newer_home_history_file(self):
        cfg = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            home_dir = root / "home"
            artifacts_dir = root / "artifacts"
            home_watchdog_dir = home_dir / ".codex" / "autonomy"
            home_watchdog_dir.mkdir(parents=True)
            artifacts_dir.mkdir(parents=True)
            cfg.artifacts_dir = artifacts_dir

            state_path = artifacts_dir / "process-watchdog-state.json"
            state_path.write_text(json.dumps({"runs_total": 1, "processes": {}}), encoding="utf-8")
            sibling_history = artifacts_dir / "process-watchdog-history.ndjson"
            home_history = home_watchdog_dir / "process-watchdog-history.ndjson"
            sibling_history.write_text("{}", encoding="utf-8")
            home_history.write_text("{}", encoding="utf-8")
            os.utime(sibling_history, (1_700_000_000, 1_700_000_000))
            os.utime(home_history, (1_700_000_120, 1_700_000_120))

            with mock.patch("orxaq_autonomy.dashboard.Path.home", return_value=home_dir), mock.patch.dict(
                "os.environ",
                {},
                clear=True,
            ):
                selected = dashboard._watchdog_history_path(cfg, state_path)

        self.assertEqual(selected, home_history.resolve())

    def test_safe_collab_runtime_snapshot_includes_runtime_and_signal_fields(self):
        cfg = mock.Mock()
        lane_payload = {
            "ok": True,
            "partial": False,
            "errors": [],
            "lanes": [
                {
                    "id": "codex-governance",
                    "owner": "codex",
                    "pid": 4242,
                    "running": True,
                    "health": "ok",
                    "tasks_file": "/tmp/tasks.json",
                    "events_file": "/tmp/events.ndjson",
                    "description": "lane description",
                    "last_event": {"timestamp": "2026-01-01T00:00:00+00:00"},
                    "meta": {"started_at": "2026-01-01T00:00:00+00:00"},
                    "impl_repo": "/tmp/repo",
                    "exclusive_paths": ["src/"],
                }
            ],
        }
        with mock.patch("orxaq_autonomy.dashboard._safe_lane_status_snapshot", return_value=lane_payload), mock.patch(
            "orxaq_autonomy.dashboard._resolve_lane_work_title",
            return_value=("Upgrade governance dashboard for collaboration observability", "governance-task"),
        ), mock.patch(
            "orxaq_autonomy.dashboard._lane_running_age_seconds",
            return_value=540,
        ), mock.patch(
            "orxaq_autonomy.dashboard._lane_health_confirmation",
            return_value=("2026-01-01T00:08:55Z", 5),
        ), mock.patch(
            "orxaq_autonomy.dashboard._lane_commit_count_last_hour",
            return_value=4,
        ), mock.patch(
            "orxaq_autonomy.dashboard._lane_commit_velocity_metrics",
            return_value={
                "commits_last_hour_from_bins": 4,
                "commit_bins_5m": [0, 0, 0, 0, 1, 1, 0, 0, 0, 1, 0, 1],
                "commit_bins_max": 1,
                "commit_velocity_level": 0.4,
                "latest_commit_at": "2026-01-01T00:08:57Z",
                "latest_commit_age_sec": 3,
            },
        ), mock.patch(
            "orxaq_autonomy.dashboard._lane_signal_metrics",
            return_value={
                "latest_signal_at": "2026-01-01T00:08:58Z",
                "latest_signal_age_sec": 2,
                "latest_task_done_at": "2026-01-01T00:08:54Z",
                "latest_task_done_age_sec": 6,
                "latest_push_at": "2026-01-01T00:08:56Z",
                "latest_push_age_sec": 4,
                "signal_events_15s": 3,
                "signal_events_60s": 9,
                "signal_events_300s": 11,
                "recent_error_events_10m": 0,
                "signal_bins_5m": [0, 0, 0, 1, 0, 1, 1, 2, 3, 1, 0, 1],
                "signal_bins_max": 3,
                "signal_level": 0.82,
                "live_state": "thinking",
                "live_label": "thinking",
            },
        ):
            payload = dashboard._safe_collab_runtime_snapshot(cfg)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["partial"])
        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertEqual(row["lane_id"], "codex-governance")
        self.assertEqual(row["ai"], "codex")
        self.assertEqual(row["pid"], 4242)
        self.assertEqual(row["work_title"], "Upgrade governance dashboard for collaboration observability")
        self.assertEqual(row["task_id"], "governance-task")
        self.assertEqual(row["running_age_sec"], 540)
        self.assertEqual(row["commits_last_hour"], 4)
        self.assertEqual(row["latest_task_done_age_sec"], 6)
        self.assertEqual(row["latest_push_age_sec"], 4)
        self.assertEqual(row["latest_commit_age_sec"], 3)
        self.assertEqual(row["commit_bins_5m"][4], 1)
        self.assertEqual(row["signal_bins_5m"][8], 3)
        self.assertEqual(row["live_state"], "thinking")
        self.assertAlmostEqual(row["signal_level"], 0.82, places=3)
        self.assertEqual(row["attention_level"], "ok")
        self.assertEqual(row["attention_score"], 0)
        self.assertEqual(payload["summary"]["total_rows"], 1)
        self.assertEqual(payload["summary"]["running_rows"], 1)
        self.assertEqual(payload["summary"]["thinking_rows"], 1)
        self.assertEqual(payload["summary"]["commits_last_hour_total"], 4)
        self.assertEqual(payload["summary"]["attention_rows"], 0)
        self.assertEqual(payload["summary"]["latest_task_done_at"], "2026-01-01T00:08:54Z")
        self.assertEqual(payload["summary"]["latest_push_at"], "2026-01-01T00:08:56Z")
        self.assertEqual(payload["summary"]["latest_commit_at"], "2026-01-01T00:08:57Z")
        self.assertGreaterEqual(payload["summary"]["latest_task_done_age_sec"], 0)
        self.assertGreaterEqual(payload["summary"]["latest_push_age_sec"], 0)
        self.assertGreaterEqual(payload["summary"]["latest_commit_age_sec"], 0)

    def test_lane_signal_metrics_filters_shared_conversation_events_by_lane_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            events_path = root / "lane-a-events.ndjson"
            conversation_path = root / "shared-conversations.ndjson"
            events_path.write_text(
                (
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:10+00:00",
                            "event_type": "task_done",
                        }
                    )
                    + "\n"
                ),
                encoding="utf-8",
            )
            conversation_path.write_text(
                (
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:11+00:00",
                            "lane_id": "lane-a",
                            "event_type": "agent_output",
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:11+00:00",
                            "lane_id": "lane-b",
                            "event_type": "agent_output",
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:11+00:00",
                            "event_type": "agent_output",
                        }
                    )
                    + "\n"
                ),
                encoding="utf-8",
            )
            lane = {
                "id": "lane-a",
                "running": True,
                "health": "ok",
                "events_file": str(events_path),
                "meta": {"conversation_log_file": str(conversation_path)},
            }
            now = datetime(2026, 1, 1, 0, 0, 20, tzinfo=timezone.utc)
            metrics = dashboard._lane_signal_metrics(lane, now)
        self.assertEqual(metrics["signal_events_15s"], 2)
        self.assertEqual(metrics["signal_events_60s"], 2)
        self.assertEqual(metrics["signal_events_300s"], 2)
        self.assertEqual(metrics["latest_task_done_at"], "2026-01-01T00:00:10Z")

    def test_lane_signal_metrics_skips_unscoped_events_from_non_lane_specific_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            shared_events = root / "shared-events.ndjson"
            shared_events.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:11+00:00",
                        "event_type": "agent_output",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane = {
                "id": "lane-a",
                "running": True,
                "health": "ok",
                "events_file": str(shared_events),
            }
            now = datetime(2026, 1, 1, 0, 0, 20, tzinfo=timezone.utc)
            metrics = dashboard._lane_signal_metrics(lane, now)
        self.assertEqual(metrics["signal_events_15s"], 0)
        self.assertEqual(metrics["signal_events_60s"], 0)
        self.assertEqual(metrics["signal_events_300s"], 0)
        self.assertEqual(metrics["latest_signal_at"], "")

    def test_lane_attention_metrics_marks_offline_lane_as_critical(self):
        metrics = dashboard._lane_attention_metrics(
            {
                "running": False,
                "health": "stopped_unexpected",
                "latest_health_confirmation_age_sec": 620,
                "latest_signal_age_sec": -1,
                "latest_task_done_age_sec": -1,
                "latest_push_age_sec": -1,
                "commits_last_hour": 2,
                "signal_events_60s": 0,
                "recent_error_events_10m": 2,
                "live_state": "offline",
            }
        )
        self.assertEqual(metrics["attention_level"], "critical")
        self.assertGreaterEqual(metrics["attention_score"], 70)
        self.assertIn("process offline", metrics["attention_message"])

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

    def test_filter_conversation_payload_for_lane_normalizes_scalar_errors(self):
        payload = {
            "ok": False,
            "partial": True,
            "errors": "lane-a source lagging",
            "sources": [
                {"lane_id": "lane-a", "kind": "lane", "ok": True, "error": "", "event_count": 1},
            ],
        }
        filtered = dashboard._filter_conversation_payload_for_lane(payload, lane_id="lane-a")
        self.assertFalse(filtered["ok"])
        self.assertTrue(filtered["partial"])
        self.assertEqual(filtered["errors"], ["lane-a source lagging"])
        self.assertEqual(filtered["suppressed_source_error_count"], 0)

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

    def test_safe_conversations_snapshot_suppresses_primary_failure_when_lane_source_is_healthy(self):
        cfg = mock.Mock()
        cfg.conversation_log_file = pathlib.Path("/tmp/conversations.ndjson")
        source_payload = {
            "total_events": 1,
            "events": [
                {"owner": "codex", "lane_id": "lane-a", "event_type": "status", "content": "alpha"},
            ],
            "owner_counts": {"codex": 1},
            "sources": [
                {
                    "lane_id": "",
                    "kind": "primary",
                    "resolved_kind": "primary",
                    "path": "/tmp/conversations.ndjson",
                    "resolved_path": "/tmp/conversations.ndjson",
                    "ok": False,
                    "error": "primary stream unavailable",
                    "event_count": 0,
                },
                {
                    "lane_id": "lane-a",
                    "kind": "lane",
                    "resolved_kind": "lane",
                    "path": "/tmp/lanes/lane-a/conversations.ndjson",
                    "resolved_path": "/tmp/lanes/lane-a/conversations.ndjson",
                    "ok": True,
                    "error": "",
                    "event_count": 1,
                },
            ],
            "partial": True,
            "ok": False,
            "errors": ["/tmp/conversations.ndjson: primary stream unavailable"],
        }
        with mock.patch("orxaq_autonomy.dashboard.conversations_snapshot", return_value=source_payload):
            payload = dashboard._safe_conversations_snapshot(cfg, lines=200, lane_id="lane-a")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["partial"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["total_events"], 1)
        self.assertEqual(payload["suppressed_source_count"], 1)
        self.assertEqual(len(payload["sources"]), 1)
        self.assertEqual(payload["sources"][0]["lane_id"], "lane-a")
        self.assertIn("primary stream unavailable", " ".join(payload["suppressed_source_errors"]))

    def test_safe_conversations_snapshot_lane_filter_matches_sources_case_insensitively(self):
        cfg = mock.Mock()
        cfg.conversation_log_file = pathlib.Path("/tmp/conversations.ndjson")
        source_payload = {
            "total_events": 1,
            "events": [
                {"owner": "codex", "lane_id": "lane-a", "event_type": "status", "content": "alpha"},
            ],
            "owner_counts": {"codex": 1},
            "sources": [
                {
                    "lane_id": "",
                    "kind": "primary",
                    "resolved_kind": "primary",
                    "path": "/tmp/conversations.ndjson",
                    "resolved_path": "/tmp/conversations.ndjson",
                    "ok": False,
                    "error": "primary stream unavailable",
                    "event_count": 0,
                },
                {
                    "lane_id": "lane-a",
                    "kind": "lane",
                    "resolved_kind": "lane",
                    "path": "/tmp/lanes/lane-a/conversations.ndjson",
                    "resolved_path": "/tmp/lanes/lane-a/conversations.ndjson",
                    "ok": True,
                    "error": "",
                    "event_count": 1,
                },
            ],
            "partial": True,
            "ok": False,
            "errors": ["/tmp/conversations.ndjson: primary stream unavailable"],
        }
        with mock.patch("orxaq_autonomy.dashboard.conversations_snapshot", return_value=source_payload):
            payload = dashboard._safe_conversations_snapshot(cfg, lines=200, lane_id="LANE-A")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["partial"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["total_events"], 1)
        self.assertEqual(payload["suppressed_source_count"], 1)
        self.assertEqual(len(payload["sources"]), 1)
        self.assertEqual(payload["sources"][0]["lane_id"], "lane-a")
        self.assertEqual(payload["filters"]["lane"], "LANE-A")

    def test_safe_conversations_snapshot_suppresses_generic_primary_error_when_lane_source_is_healthy(self):
        cfg = mock.Mock()
        cfg.conversation_log_file = pathlib.Path("/tmp/conversations.ndjson")
        source_payload = {
            "total_events": 1,
            "events": [
                {"owner": "codex", "lane_id": "lane-a", "event_type": "status", "content": "alpha"},
            ],
            "owner_counts": {"codex": 1},
            "sources": [
                {
                    "lane_id": "",
                    "kind": "primary",
                    "resolved_kind": "primary",
                    "path": "/tmp/conversations.ndjson",
                    "resolved_path": "/tmp/conversations.ndjson",
                    "ok": False,
                    "error": "read timeout",
                    "event_count": 0,
                },
                {
                    "lane_id": "lane-a",
                    "kind": "lane",
                    "resolved_kind": "lane",
                    "path": "/tmp/lanes/lane-a/conversations.ndjson",
                    "resolved_path": "/tmp/lanes/lane-a/conversations.ndjson",
                    "ok": True,
                    "error": "",
                    "event_count": 1,
                },
            ],
            "partial": True,
            "ok": False,
            "errors": ["conversation source unavailable"],
        }
        with mock.patch("orxaq_autonomy.dashboard.conversations_snapshot", return_value=source_payload):
            payload = dashboard._safe_conversations_snapshot(cfg, lines=200, lane_id="lane-a")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["partial"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["total_events"], 1)
        self.assertEqual(payload["suppressed_source_count"], 1)
        self.assertEqual(payload["suppressed_source_error_count"], 2)
        self.assertIn("conversation source unavailable", payload["suppressed_source_errors"])

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

    def test_lane_conversation_rollup_uses_event_sequence_when_timestamps_invalid(self):
        payload = {
            "events": [
                {
                    "timestamp": "z-invalid",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "older-invalid",
                },
                {
                    "timestamp": "a-invalid",
                    "owner": "codex",
                    "lane_id": "lane-a",
                    "event_type": "status",
                    "content": "newer-invalid",
                },
            ],
            "sources": [],
        }
        rollup = dashboard._lane_conversation_rollup(payload)
        self.assertIn("lane-a", rollup)
        self.assertEqual(rollup["lane-a"]["latest_event"]["content"], "newer-invalid")

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

    def test_augment_lane_payload_with_conversation_rollup_recovers_missing_lane_case_insensitively(self):
        lane_payload = {
            "requested_lane": "LANE-A",
            "errors": ["Unknown lane id 'LANE-A'. Update /tmp/lanes.json."],
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
        self.assertEqual(enriched["requested_lane"], "lane-a")
        self.assertEqual(enriched["recovered_lane_count"], 1)
        self.assertEqual(enriched["recovered_lanes"], ["lane-a"])
        self.assertEqual(enriched["lanes"][0]["id"], "lane-a")
        self.assertEqual(enriched["lanes"][0]["owner"], "codex")

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
        self.assertEqual(payload["supported_actions"], ["status", "ensure", "start", "stop"])

    def test_safe_lane_action_status_uses_lane_and_conversation_rollup(self):
        cfg = mock.Mock()
        lane_snapshot = {"ok": True, "lanes": []}
        filtered_lane_payload = {"ok": True, "partial": False, "errors": [], "lanes": []}
        conversation_payload = {"ok": True, "partial": False, "errors": [], "events": [], "sources": []}
        augmented_payload = {"ok": False, "partial": True, "errors": ["lane status source unavailable"], "lanes": []}
        with mock.patch(
            "orxaq_autonomy.dashboard._safe_lane_status_snapshot",
            return_value=lane_snapshot,
        ), mock.patch(
            "orxaq_autonomy.dashboard._filter_lane_status_payload",
            return_value=filtered_lane_payload,
        ) as filter_lane, mock.patch(
            "orxaq_autonomy.dashboard._safe_conversations_snapshot",
            return_value=conversation_payload,
        ) as conversations, mock.patch(
            "orxaq_autonomy.dashboard._augment_lane_payload_with_conversation_rollup",
            return_value=augmented_payload,
        ) as augment:
            payload = dashboard._safe_lane_action(cfg, action="status", lane_id="lane-a")
        filter_lane.assert_called_once_with(lane_snapshot, lane_id="lane-a")
        conversations.assert_called_once_with(
            cfg,
            lines=200,
            include_lanes=True,
            lane_id="lane-a",
        )
        augment.assert_called_once_with(filtered_lane_payload, conversation_payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["action"], "status")
        self.assertEqual(payload["lane"], "lane-a")

    def test_safe_lane_action_status_reports_resolved_lane(self):
        cfg = mock.Mock()
        with mock.patch(
            "orxaq_autonomy.dashboard._safe_lane_status_snapshot",
            return_value={"ok": True, "lanes": []},
        ), mock.patch(
            "orxaq_autonomy.dashboard._filter_lane_status_payload",
            return_value={"ok": True, "partial": False, "errors": [], "lanes": [], "requested_lane": "lane-a"},
        ), mock.patch(
            "orxaq_autonomy.dashboard._safe_conversations_snapshot",
            return_value={"ok": True, "partial": False, "errors": [], "events": [], "sources": []},
        ), mock.patch(
            "orxaq_autonomy.dashboard._augment_lane_payload_with_conversation_rollup",
            return_value={"ok": True, "partial": False, "errors": [], "lanes": [], "requested_lane": "lane-a"},
        ):
            payload = dashboard._safe_lane_action(cfg, action="status", lane_id="LANE-A")
        self.assertEqual(payload["lane"], "lane-a")
        self.assertEqual(payload["requested_lane"], "lane-a")

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

    def test_lane_action_http_status_ok_for_status_action_when_partial(self):
        status = dashboard._lane_action_http_status(
            {
                "action": "status",
                "ok": False,
                "partial": True,
                "errors": ["lane status source unavailable"],
            }
        )
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

    def test_distributed_todo_owner_hint_falls_back_to_assigned_branch(self):
        yaml_body = """\
cycle_id: cycle-1
generated_utc: "2026-02-10T00:00:00+00:00"
tasks:
- id: T1
  parent_id: null
  mece_bucket: Flow
  title: Codex branch assignment without owner in swarm label
  ai_level_required_1_10: 6
  cost_tier_target: mid
  priority_band: P1
  assigned_swarm: swarm-alpha
  assigned_branch: codex/swarm-alpha
  status: todo
- id: T2
  parent_id: null
  mece_bucket: Flow
  title: Claude explicit swarm
  ai_level_required_1_10: 7
  cost_tier_target: mid
  priority_band: P0
  assigned_swarm: swarm-claude-medium-observability
  assigned_branch: claude/swarm-collab
  status: doing
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            todo_file = pathlib.Path(tmp_dir) / "distributed_todo.yaml"
            todo_file.write_text(yaml_body, encoding="utf-8")
            lane_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "lanes": [
                    {"id": "lane-codex", "owner": "codex", "running": True, "health": "ok"},
                    {"id": "lane-claude", "owner": "claude", "running": True, "health": "ok"},
                ],
            }
            with mock.patch(
                "orxaq_autonomy.dashboard._resolve_distributed_todo_file",
                return_value=todo_file,
            ), mock.patch(
                "orxaq_autonomy.dashboard._safe_lane_status_snapshot",
                return_value=lane_payload,
            ), mock.patch(
                "orxaq_autonomy.dashboard._distributed_todo_git_events",
                return_value=[],
            ), mock.patch(
                "orxaq_autonomy.dashboard._distributed_todo_routing_events",
                return_value=[],
            ):
                payload = dashboard._safe_distributed_todo_snapshot(mock.Mock())

        active_by_id = {item["id"]: item for item in payload["active_requests"]}
        self.assertEqual(active_by_id["T1"]["owner_hint"], "codex")
        self.assertEqual(active_by_id["T1"]["live_lane_matches"], 1)
        self.assertEqual(active_by_id["T2"]["owner_hint"], "claude")
        self.assertEqual(active_by_id["T2"]["live_lane_matches"], 1)
        self.assertEqual(payload["summary"]["active_watch_live_covered_count"], 2)

    def test_distributed_todo_active_watch_summary_not_truncated_by_ui_limit(self):
        lines = [
            "cycle_id: cycle-2",
            'generated_utc: "2026-02-10T00:00:00+00:00"',
            "tasks:",
        ]
        for idx in range(45):
            lines.extend(
                [
                    f"- id: T{idx}",
                    "  parent_id: null",
                    "  mece_bucket: Flow",
                    f"  title: Task {idx}",
                    "  ai_level_required_1_10: 6",
                    "  cost_tier_target: mid",
                    "  priority_band: P1",
                    "  assigned_swarm: swarm-alpha",
                    "  assigned_branch: codex/swarm-alpha",
                    "  status: todo",
                ]
            )
        yaml_body = "\n".join(lines) + "\n"
        with tempfile.TemporaryDirectory() as tmp_dir:
            todo_file = pathlib.Path(tmp_dir) / "distributed_todo.yaml"
            todo_file.write_text(yaml_body, encoding="utf-8")
            lane_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "lanes": [
                    {"id": "lane-codex", "owner": "codex", "running": True, "health": "ok"},
                ],
            }
            with mock.patch(
                "orxaq_autonomy.dashboard._resolve_distributed_todo_file",
                return_value=todo_file,
            ), mock.patch(
                "orxaq_autonomy.dashboard._safe_lane_status_snapshot",
                return_value=lane_payload,
            ), mock.patch(
                "orxaq_autonomy.dashboard._distributed_todo_git_events",
                return_value=[],
            ), mock.patch(
                "orxaq_autonomy.dashboard._distributed_todo_routing_events",
                return_value=[],
            ):
                payload = dashboard._safe_distributed_todo_snapshot(mock.Mock())

        self.assertEqual(payload["summary"]["active_watch_total"], 45)
        self.assertEqual(payload["summary"]["active_watch_visible_count"], 40)
        self.assertEqual(payload["summary"]["active_watch_hidden_count"], 5)
        self.assertEqual(payload["summary"]["active_watch_live_covered_count"], 45)
        self.assertEqual(payload["summary"]["active_watch_live_uncovered_count"], 0)
        self.assertEqual(len(payload["active_requests"]), 40)
        self.assertEqual(len(payload["active_requests_all"]), 45)

    def test_parse_distributed_todo_accepts_tasks_where_id_is_not_first_key(self):
        yaml_body = """\
cycle_id: cycle-order
generated_utc: "2026-02-10T00:00:00+00:00"
tasks:
- parent_id: ROOT
  id: T-order
  status: todo
  priority_band: P2
  ai_level_required_1_10: 4
  cost_tier_target: cheap
  mece_bucket: Ordering
  assigned_swarm: swarm-alpha
  assigned_branch: codex/swarm-order
  title: Task id appears after parent_id
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            todo_file = pathlib.Path(tmp_dir) / "distributed_todo.yaml"
            todo_file.write_text(yaml_body, encoding="utf-8")
            parsed = dashboard._parse_distributed_todo_yaml(todo_file)
        self.assertEqual(parsed["cycle_id"], "cycle-order")
        self.assertEqual(len(parsed["tasks"]), 1)
        task = parsed["tasks"][0]
        self.assertEqual(task["id"], "T-order")
        self.assertEqual(task["parent_id"], "ROOT")
        self.assertEqual(task["assigned_branch"], "codex/swarm-order")

    def test_distributed_todo_live_lane_matches_vary_by_lane_signals(self):
        yaml_body = """\
cycle_id: cycle-live
generated_utc: "2026-02-10T00:00:00+00:00"
tasks:
- id: T-governance
  parent_id: null
  mece_bucket: Flow
  title: Governance lane task
  ai_level_required_1_10: 6
  cost_tier_target: mid
  priority_band: P1
  assigned_swarm: swarm-alpha
  assigned_branch: codex/swarm-governance
  status: todo
- id: T-dashboard
  parent_id: null
  mece_bucket: Flow
  title: Dashboard lane task
  ai_level_required_1_10: 6
  cost_tier_target: mid
  priority_band: P1
  assigned_swarm: swarm-golf
  assigned_branch: codex/swarm-dashboard
  status: todo
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            todo_file = pathlib.Path(tmp_dir) / "distributed_todo.yaml"
            todo_file.write_text(yaml_body, encoding="utf-8")
            lane_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "lanes": [
                    {
                        "id": "codex-governance",
                        "owner": "codex",
                        "running": True,
                        "health": "ok",
                        "description": "governance implementation lane",
                    },
                    {
                        "id": "codex-dashboard",
                        "owner": "codex",
                        "running": True,
                        "health": "ok",
                        "description": "dashboard implementation lane",
                    },
                    {
                        "id": "codex-swarm-dashboard",
                        "owner": "codex",
                        "running": True,
                        "health": "ok",
                        "description": "dashboard implementation lane",
                    },
                ],
            }
            with mock.patch(
                "orxaq_autonomy.dashboard._resolve_distributed_todo_file",
                return_value=todo_file,
            ), mock.patch(
                "orxaq_autonomy.dashboard._safe_lane_status_snapshot",
                return_value=lane_payload,
            ), mock.patch(
                "orxaq_autonomy.dashboard._distributed_todo_git_events",
                return_value=[],
            ), mock.patch(
                "orxaq_autonomy.dashboard._distributed_todo_routing_events",
                return_value=[],
            ):
                payload = dashboard._safe_distributed_todo_snapshot(mock.Mock())

        active_by_id = {item["id"]: item for item in payload["active_requests"]}
        self.assertEqual(active_by_id["T-governance"]["live_lane_matches"], 1)
        self.assertEqual(active_by_id["T-dashboard"]["live_lane_matches"], 2)
        self.assertNotEqual(
            active_by_id["T-governance"]["live_lane_matches"],
            active_by_id["T-dashboard"]["live_lane_matches"],
        )
        self.assertEqual(payload["summary"]["active_watch_live_covered_count"], 2)


if __name__ == "__main__":
    unittest.main()
