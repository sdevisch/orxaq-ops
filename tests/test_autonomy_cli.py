import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import cli


class CliTests(unittest.TestCase):
    def _prep_root(self, root: pathlib.Path) -> None:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "state").mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "autonomy").mkdir(parents=True, exist_ok=True)
        (root / "config" / "tasks.json").write_text("[]\n", encoding="utf-8")
        (root / "config" / "objective.md").write_text("objective\n", encoding="utf-8")
        (root / "config" / "codex_result.schema.json").write_text("{}\n", encoding="utf-8")
        (root / "config" / "skill_protocol.json").write_text("{}\n", encoding="utf-8")
        (root / ".env.autonomy").write_text("OPENAI_API_KEY=test\nGEMINI_API_KEY=test\n", encoding="utf-8")

    def test_init_skill_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            rc = cli.main(["--root", str(root), "init-skill-protocol", "--output", "config/new_skill.json"])
            self.assertEqual(rc, 0)
            self.assertTrue((root / "config" / "new_skill.json").exists())

    def test_status_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs", return_value=""
            ):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)

    def test_health_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.health_snapshot", return_value={"healthy": True}):
                rc = cli.main(["--root", str(root), "health"])
            self.assertEqual(rc, 0)

    def test_monitor_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.monitor_snapshot",
                return_value={"status": {}, "progress": {}, "repos": {}, "latest_log_line": ""},
            ), mock.patch("orxaq_autonomy.cli.render_monitor_text", return_value="monitor"):
                rc = cli.main(["--root", str(root), "monitor"])
            self.assertEqual(rc, 0)

    def test_metrics_command_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
                "response_metrics": {
                    "responses_total": 3,
                    "quality_score_avg": 0.9,
                    "cost_usd_total": 1.23,
                    "tokens_total": 4200,
                    "token_rate_per_minute": 840.0,
                    "exciting_stat": {"label": "Token Flow", "value": "4,200 tokens"},
                }
            }
            with mock.patch("orxaq_autonomy.cli.monitor_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "metrics", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["responses_total"], 3)
            self.assertEqual(data["tokens_total"], 4200)
            self.assertEqual(data["exciting_stat"]["label"], "Token Flow")

    def test_metrics_command_prints_recommendations(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
                "response_metrics": {
                    "responses_total": 1,
                    "quality_score_avg": 0.1,
                    "first_time_pass_rate": 0.0,
                    "acceptance_pass_rate": 0.0,
                    "latency_sec_avg": 300.0,
                    "prompt_difficulty_score_avg": 80.0,
                    "cost_usd_total": 2.0,
                    "cost_usd_avg": 2.0,
                    "exact_cost_coverage": 0.0,
                    "optimization_recommendations": ["Tighten prompts"],
                }
            }
            with mock.patch("orxaq_autonomy.cli.monitor_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "metrics"])
            self.assertEqual(rc, 0)
            output = buffer.getvalue()
            self.assertIn("responses_total", output)
            self.assertIn("recommendations:", output)
            self.assertIn("Tighten prompts", output)

    def test_bootstrap_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.bootstrap_background",
                return_value={"ok": True, "startup_packet": "packet.md"},
            ) as bootstrap:
                rc = cli.main(["--root", str(root), "bootstrap"])
            self.assertEqual(rc, 0)
            kwargs = bootstrap.call_args.kwargs
            self.assertTrue(kwargs["allow_dirty"])
            self.assertTrue(kwargs["install_keepalive_job"])
            self.assertEqual(kwargs["ide"], "vscode")

    def test_bootstrap_command_returns_nonzero_on_preflight_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.bootstrap_background",
                return_value={"ok": False, "reason": "preflight_failed"},
            ):
                rc = cli.main(["--root", str(root), "bootstrap", "--require-clean", "--skip-keepalive", "--ide", "none"])
            self.assertEqual(rc, 1)

    def test_cli_returns_structured_error_on_runtime_exception(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.start_background", side_effect=RuntimeError("codex missing")):
                rc = cli.main(["--root", str(root), "start"])
            self.assertEqual(rc, 1)

    def test_dashboard_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.start_dashboard", return_value=0) as start:
                rc = cli.main(
                    [
                        "--root",
                        str(root),
                        "dashboard",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8789",
                        "--refresh-sec",
                        "2",
                        "--no-browser",
                    ]
                )
            self.assertEqual(rc, 0)
            kwargs = start.call_args.kwargs
            self.assertEqual(kwargs["host"], "127.0.0.1")
            self.assertEqual(kwargs["port"], 8789)
            self.assertEqual(kwargs["refresh_sec"], 2)
            self.assertFalse(kwargs["open_browser"])

    def test_dashboard_start_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.start_dashboard_background",
                return_value={"running": True, "url": "http://127.0.0.1:8765/"},
            ) as start:
                rc = cli.main(["--root", str(root), "dashboard-start", "--no-browser"])
            self.assertEqual(rc, 0)
            kwargs = start.call_args.kwargs
            self.assertFalse(kwargs["open_browser"])

    def test_dashboard_status_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.dashboard_status_snapshot", return_value={"running": True}):
                rc = cli.main(["--root", str(root), "dashboard-status"])
            self.assertEqual(rc, 0)

    def test_conversations_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value={"total_events": 0, "events": [], "owner_counts": {}},
            ) as snap:
                rc = cli.main(["--root", str(root), "conversations", "--lines", "50"])
            self.assertEqual(rc, 0)
            kwargs = snap.call_args.kwargs
            self.assertEqual(kwargs["lines"], 50)
            self.assertTrue(kwargs["include_lanes"])

    def test_conversation_inspect_alias_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value={
                    "total_events": 1,
                    "events": [{"timestamp": "2026-01-01T00:00:00+00:00", "owner": "codex", "lane_id": "lane-a"}],
                    "owner_counts": {"codex": 1},
                },
            ) as snap:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(
                        [
                            "--root",
                            str(root),
                            "conversation-inspect",
                            "--lines",
                            "80",
                            "--lane",
                            "lane-a",
                            "--tail",
                            "1",
                        ]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["total_events"], 1)
            self.assertEqual(payload["filters"]["lane"], "lane-a")
            self.assertEqual(payload["filters"]["tail"], 1)
            kwargs = snap.call_args.kwargs
            self.assertEqual(kwargs["lines"], 80)
            self.assertTrue(kwargs["include_lanes"])

    def test_conversations_command_degrades_when_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                side_effect=RuntimeError("conversation source unavailable"),
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "conversations", "--owner", "codex"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["filters"]["owner"], "codex")
            self.assertEqual(payload["total_events"], 0)
            self.assertEqual(len(payload["sources"]), 1)
            self.assertEqual(payload["sources"][0]["kind"], "primary")
            self.assertIn("conversation source unavailable", payload["errors"][0])

    def test_conversations_command_applies_filters(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
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
            with mock.patch("orxaq_autonomy.cli.conversations_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(
                        [
                            "--root",
                            str(root),
                            "conversations",
                            "--owner",
                            "codex",
                            "--lane",
                            "lane-a",
                            "--event-type",
                            "status",
                            "--contains",
                            "alpha",
                            "--tail",
                            "1",
                        ]
                    )
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["total_events"], 1)
            self.assertEqual(data["unfiltered_total_events"], 2)
            self.assertEqual(data["events"][0]["owner"], "codex")
            self.assertEqual(data["filters"]["lane"], "lane-a")

    def test_conversations_command_suppresses_unrelated_lane_source_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
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
            with mock.patch("orxaq_autonomy.cli.conversations_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "conversations", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial"])
            self.assertEqual(data["total_events"], 1)
            self.assertEqual(data["errors"], [])
            self.assertEqual(data["suppressed_source_error_count"], 1)
            self.assertEqual(len(data["sources"]), 2)

    def test_conversations_command_suppresses_path_prefixed_lane_source_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
                "total_events": 2,
                "events": [
                    {"owner": "codex", "lane_id": "lane-a", "event_type": "status", "content": "alpha"},
                    {"owner": "gemini", "lane_id": "lane-b", "event_type": "status", "content": "beta"},
                ],
                "owner_counts": {"codex": 1, "gemini": 1},
                "sources": [
                    {
                        "lane_id": "",
                        "kind": "primary",
                        "path": "/tmp/main/conversations.ndjson",
                        "resolved_path": "/tmp/main/conversations.ndjson",
                        "ok": True,
                        "error": "",
                        "event_count": 2,
                    },
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
                "partial": True,
                "ok": False,
                "errors": ["/tmp/lanes/lane-b/conversations.ndjson: lane-b stream unavailable"],
            }
            with mock.patch("orxaq_autonomy.cli.conversations_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "conversations", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial"])
            self.assertEqual(data["total_events"], 1)
            self.assertEqual(data["errors"], [])
            self.assertIn(
                "/tmp/lanes/lane-b/conversations.ndjson: lane-b stream unavailable",
                data["suppressed_source_errors"],
            )
            self.assertEqual(data["suppressed_source_error_count"], 2)
            self.assertEqual(len(data["sources"]), 2)

    def test_conversations_command_suppresses_primary_failure_when_lane_source_is_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
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
            with mock.patch("orxaq_autonomy.cli.conversations_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "conversations", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial"])
            self.assertEqual(data["errors"], [])
            self.assertEqual(data["total_events"], 1)
            self.assertEqual(data["suppressed_source_count"], 1)
            self.assertEqual(len(data["sources"]), 1)
            self.assertEqual(data["sources"][0]["lane_id"], "lane-a")
            self.assertIn("primary stream unavailable", " ".join(data["suppressed_source_errors"]))

    def test_conversations_command_lane_filter_matches_sources_case_insensitively(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
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
            with mock.patch("orxaq_autonomy.cli.conversations_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "conversations", "--lane", "LANE-A"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial"])
            self.assertEqual(data["errors"], [])
            self.assertEqual(data["total_events"], 1)
            self.assertEqual(data["suppressed_source_count"], 1)
            self.assertEqual(len(data["sources"]), 1)
            self.assertEqual(data["sources"][0]["lane_id"], "lane-a")
            self.assertEqual(data["filters"]["lane"], "LANE-A")

    def test_conversations_command_suppresses_generic_primary_error_when_lane_source_is_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
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
            with mock.patch("orxaq_autonomy.cli.conversations_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "conversations", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial"])
            self.assertEqual(data["errors"], [])
            self.assertEqual(data["suppressed_source_count"], 1)
            self.assertEqual(data["suppressed_source_error_count"], 2)
            self.assertIn("conversation source unavailable", data["suppressed_source_errors"])

    def test_conversations_command_normalizes_scalar_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            payload = {
                "total_events": 1,
                "events": [
                    {"owner": "codex", "lane_id": "lane-a", "event_type": "status", "content": "alpha"},
                ],
                "owner_counts": {"codex": 1},
                "sources": [
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
                "errors": "lane-a source lagging",
            }
            with mock.patch("orxaq_autonomy.cli.conversations_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "conversations", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertFalse(data["ok"])
            self.assertTrue(data["partial"])
            self.assertEqual(data["errors"], ["lane-a source lagging"])
            self.assertEqual(data["suppressed_source_error_count"], 0)

    def test_lane_inspect_command_returns_lane_and_filtered_conversations(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "errors": [],
                "health_counts": {"ok": 1},
                "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
            }
            conv_payload = {
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
                "sources": [
                    {
                        "lane_id": "lane-a",
                        "ok": True,
                        "missing": False,
                        "recoverable_missing": False,
                        "fallback_used": False,
                        "error": "",
                        "event_count": 1,
                    }
                ],
                "partial": False,
                "ok": True,
                "errors": [],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conv_payload,
            ) as conversations:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a", "--contains", "alp"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["requested_lane"], "lane-a")
            self.assertEqual(payload["lane"]["owner"], "codex")
            self.assertEqual(payload["conversations"]["total_events"], 1)
            self.assertEqual(payload["conversations"]["events"][0]["lane_id"], "lane-a")
            self.assertEqual(payload["conversations"]["filters"]["lane"], "lane-a")
            self.assertEqual(payload["conversation_source_health"]["lane"], "lane-a")
            self.assertEqual(payload["conversation_source_health"]["state"], "ok")
            self.assertEqual(payload["conversation_source_health"]["reported_sources"], 1)
            self.assertTrue(payload["ok"])
            self.assertTrue(conversations.call_args.kwargs["include_lanes"])

    def test_lane_inspect_matches_requested_lane_case_insensitively(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "health_counts": {"ok": 1},
                "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
            }
            conv_payload = {
                "total_events": 1,
                "events": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "owner": "codex",
                        "lane_id": "lane-a",
                        "event_type": "status",
                        "content": "alpha",
                    },
                ],
                "owner_counts": {"codex": 1},
                "sources": [{"lane_id": "lane-a", "ok": True, "error": "", "event_count": 1}],
                "partial": False,
                "ok": True,
                "errors": [],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conv_payload,
            ) as conversations:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "LANE-A"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["requested_lane"], "lane-a")
            self.assertEqual(payload["input_lane"], "LANE-A")
            self.assertEqual(payload["lane"]["id"], "lane-a")
            self.assertEqual(payload["conversations"]["filters"]["lane"], "lane-a")
            self.assertEqual(payload["conversation_source_health"]["lane"], "lane-a")

    def test_lane_inspect_suppresses_unrelated_lane_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "ok": False,
                "partial": True,
                "errors": ["lane-b: heartbeat stale"],
                "health_counts": {"ok": 1, "stale": 1},
                "owner_counts": {
                    "codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0},
                    "gemini": {"total": 1, "running": 1, "healthy": 0, "degraded": 1},
                },
                "lanes": [
                    {"id": "lane-a", "owner": "codex", "running": True, "health": "ok"},
                    {"id": "lane-b", "owner": "gemini", "running": True, "health": "stale"},
                ],
            }
            conv_payload = {
                "total_events": 1,
                "events": [{"owner": "codex", "lane_id": "lane-a", "event_type": "status", "content": "ok"}],
                "owner_counts": {"codex": 1},
                "ok": True,
                "partial": False,
                "errors": [],
                "sources": [],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conv_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["partial"])
            self.assertEqual(payload["lane_errors"], [])
            self.assertEqual(payload["suppressed_lane_errors"], ["lane-b: heartbeat stale"])
            self.assertEqual(payload["lane"]["id"], "lane-a")

    def test_lane_inspect_existing_lane_includes_conversation_rollup_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "health_counts": {"ok": 1},
                "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
            }
            conv_payload = {
                "total_events": 4,
                "events": [
                    {
                        "timestamp": "abc-invalid-a",
                        "owner": "claude",
                        "lane_id": "lane-a",
                        "event_type": "status",
                        "content": "invalid-first",
                    },
                    {
                        "timestamp": "2026-01-01T01:30:00+01:00",
                        "owner": "codex",
                        "lane_id": "lane-a",
                        "event_type": "status",
                        "content": "earlier",
                    },
                    {
                        "timestamp": "2026-01-01T00:45:00+00:00",
                        "owner": "gemini",
                        "lane_id": "lane-a",
                        "event_type": "status",
                        "content": "later",
                    },
                    {
                        "timestamp": "definitely-invalid-z",
                        "owner": "codex",
                        "lane_id": "lane-a",
                        "event_type": "status",
                        "content": "invalid-last",
                    },
                ],
                "owner_counts": {"codex": 2, "gemini": 1, "claude": 1},
                "ok": True,
                "partial": False,
                "errors": [],
                "sources": [
                    {
                        "lane_id": "lane-a",
                        "ok": True,
                        "missing": True,
                        "recoverable_missing": True,
                        "fallback_used": True,
                        "error": "",
                        "event_count": 3,
                    }
                ],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conv_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            lane = payload["lane"]
            self.assertEqual(lane["conversation_source_state"], "ok")
            self.assertEqual(lane["conversation_event_count"], 4)
            self.assertEqual(lane["conversation_source_count"], 1)
            self.assertEqual(lane["conversation_source_missing_count"], 1)
            self.assertEqual(lane["conversation_source_recoverable_missing_count"], 1)
            self.assertEqual(lane["conversation_source_fallback_count"], 1)
            self.assertEqual(lane["latest_conversation_event"]["content"], "later")

    def test_lane_inspect_existing_lane_infers_owner_from_conversation_rollup(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "health_counts": {"ok": 1},
                "owner_counts": {"unknown": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                "lanes": [{"id": "lane-a", "owner": "unknown", "running": True, "health": "ok"}],
            }
            conv_payload = {
                "total_events": 0,
                "events": [],
                "owner_counts": {},
                "ok": True,
                "partial": False,
                "errors": [],
                "sources": [
                    {"lane_id": "lane-a", "owner": "gemini", "ok": True, "error": "", "event_count": 0},
                ],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conv_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["lane"]["owner"], "gemini")
            self.assertEqual(payload["lane"]["conversation_source_state"], "ok")

    def test_lane_inspect_suppresses_unrelated_conversation_source_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "health_counts": {"ok": 1},
                "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
            }
            conv_payload = {
                "total_events": 2,
                "events": [
                    {"owner": "codex", "lane_id": "lane-a", "event_type": "status", "content": "ok"},
                    {"owner": "gemini", "lane_id": "lane-b", "event_type": "status", "content": "noisy"},
                ],
                "owner_counts": {"codex": 1, "gemini": 1},
                "ok": False,
                "partial": True,
                "errors": ["lane-b stream unavailable"],
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
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conv_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["partial"])
            self.assertTrue(payload["conversations"]["ok"])
            self.assertFalse(payload["conversations"]["partial"])
            self.assertEqual(payload["conversations"]["errors"], [])
            self.assertEqual(payload["conversations"]["total_events"], 1)
            self.assertEqual(payload["conversations"]["suppressed_source_error_count"], 1)
            self.assertEqual(payload["conversation_source_health"]["state"], "ok")

    def test_lane_inspect_reports_degraded_conversation_source_health(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "errors": [],
                "health_counts": {"ok": 1},
                "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
            }
            conv_payload = {
                "total_events": 0,
                "events": [],
                "owner_counts": {},
                "ok": False,
                "partial": True,
                "errors": ["lane file locked"],
                "sources": [
                    {
                        "lane_id": "lane-a",
                        "ok": False,
                        "missing": True,
                        "recoverable_missing": False,
                        "fallback_used": True,
                        "error": "lane file locked (fallback lane events used)",
                        "event_count": 0,
                    }
                ],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conv_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            health = payload["conversation_source_health"]
            self.assertEqual(health["lane"], "lane-a")
            self.assertEqual(health["state"], "degraded")
            self.assertEqual(health["reported_sources"], 1)
            self.assertEqual(health["fallback_count"], 1)
            self.assertEqual(health["missing_count"], 1)
            self.assertEqual(health["error_count"], 1)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])

    def test_lane_inspect_reports_recoverable_missing_lane_conversation_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "errors": [],
                "health_counts": {"ok": 1},
                "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
            }
            conv_payload = {
                "total_events": 1,
                "events": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "owner": "codex",
                        "lane_id": "lane-a",
                        "event_type": "status",
                        "content": "recovered via lane events",
                    }
                ],
                "owner_counts": {"codex": 1},
                "ok": True,
                "partial": False,
                "errors": [],
                "sources": [
                    {
                        "lane_id": "lane-a",
                        "ok": True,
                        "missing": True,
                        "recoverable_missing": True,
                        "fallback_used": True,
                        "error": "",
                        "event_count": 1,
                    }
                ],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conv_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            health = payload["conversation_source_health"]
            self.assertEqual(health["lane"], "lane-a")
            self.assertEqual(health["state"], "ok")
            self.assertEqual(health["reported_sources"], 1)
            self.assertEqual(health["fallback_count"], 1)
            self.assertEqual(health["missing_count"], 1)
            self.assertEqual(health["recoverable_missing_count"], 1)
            self.assertEqual(health["error_count"], 0)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["partial"])

    def test_lane_inspect_command_degrades_when_conversation_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "errors": [],
                "health_counts": {"ok": 1},
                "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                side_effect=RuntimeError("lane stream unavailable"),
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["conversations"]["partial"])
            self.assertEqual(payload["conversations"]["total_events"], 0)
            self.assertEqual(payload["lane"]["owner"], "codex")
            self.assertIn("lane stream unavailable", payload["conversations"]["errors"][0])
            self.assertEqual(payload["conversation_source_health"]["state"], "unreported")
            self.assertTrue(payload["conversation_source_health"]["global_partial"])

    def test_lane_inspect_command_returns_error_for_unknown_lane(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={"lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}]},
            ):
                rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "missing"])
            self.assertEqual(rc, 1)

    def test_lane_inspect_recovers_missing_lane_with_conversation_signal(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "errors": [],
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
                "running_count": 1,
                "total_count": 1,
            }
            conversation_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "events": [
                    {
                        "timestamp": "2026-01-01T00:00:01+00:00",
                        "owner": "gemini",
                        "lane_id": "missing",
                        "event_type": "status",
                        "content": "still active",
                    }
                ],
                "sources": [
                    {"lane_id": "missing", "ok": True, "error": "", "event_count": 1},
                ],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "missing"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["lane"]["id"], "missing")
            self.assertEqual(payload["lane"]["owner"], "gemini")
            self.assertEqual(payload["lane"]["health"], "unknown")
            self.assertTrue(payload["lane"]["conversation_lane_fallback"])
            self.assertEqual(payload["lane"]["conversation_source_state"], "ok")
            self.assertEqual(payload["conversation_source_health"]["state"], "ok")

    def test_lane_inspect_recovers_owner_from_source_metadata_without_events(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "errors": [],
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
                "running_count": 1,
                "total_count": 1,
            }
            conversation_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "events": [],
                "sources": [
                    {
                        "lane_id": "missing",
                        "owner": "gemini",
                        "ok": True,
                        "error": "",
                        "event_count": 0,
                    },
                ],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "missing"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["lane"]["id"], "missing")
            self.assertEqual(payload["lane"]["owner"], "gemini")
            self.assertTrue(payload["lane"]["conversation_lane_fallback"])
            self.assertEqual(payload["lane"]["conversation_source_state"], "ok")
            self.assertEqual(payload["conversation_source_health"]["state"], "ok")

    def test_lane_inspect_command_degrades_when_lane_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                side_effect=RuntimeError("lane source unavailable"),
            ), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value={"total_events": 0, "events": [], "owner_counts": {}, "ok": True, "partial": False},
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["lane"]["id"], "lane-a")
            self.assertEqual(payload["lane"]["owner"], "unknown")
            self.assertEqual(payload["lane"]["health"], "error")
            self.assertTrue(payload["lane_errors"])
            self.assertIn("lane source unavailable", payload["lane_errors"][0])

    def test_lane_inspect_uses_lane_plan_fallback_owner_when_lane_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "codex",
                                "impl_repo": str(root),
                                "test_repo": str(root),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                side_effect=RuntimeError("lane source unavailable"),
            ), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value={"total_events": 0, "events": [], "owner_counts": {}, "ok": True, "partial": False},
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-inspect", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["lane"]["id"], "lane-a")
            self.assertEqual(payload["lane"]["owner"], "codex")
            self.assertEqual(payload["lane"]["health"], "unknown")
            self.assertTrue(payload["lane_errors"])
            self.assertIn("lane source unavailable", payload["lane_errors"][0])

    def test_lanes_start_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.start_lanes_background",
                return_value={"started_count": 1, "started": [{"id": "codex-governance"}], "ok": True},
            ) as start:
                rc = cli.main(["--root", str(root), "lanes-start", "--lane", "codex-governance"])
            self.assertEqual(rc, 0)
            self.assertEqual(start.call_args.kwargs["lane_id"], "codex-governance")

    def test_lane_start_alias_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.start_lanes_background",
                return_value={"started_count": 1, "started": [{"id": "codex-governance"}], "ok": True},
            ) as start:
                rc = cli.main(["--root", str(root), "lane-start", "--lane", "codex-governance"])
            self.assertEqual(rc, 0)
            self.assertEqual(start.call_args.kwargs["lane_id"], "codex-governance")

    def test_lanes_stop_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.stop_lanes_background",
                return_value={"stopped_count": 2, "stopped": [{"id": "lane-a"}, {"id": "lane-b"}]},
            ) as stop:
                rc = cli.main(["--root", str(root), "lanes-stop"])
            self.assertEqual(rc, 0)
            self.assertIsNone(stop.call_args.kwargs["lane_id"])

    def test_lanes_stop_command_with_lane_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.stop_lanes_background",
                return_value={"stopped_count": 1, "stopped": [{"id": "codex-governance"}]},
            ) as stop:
                rc = cli.main(["--root", str(root), "lanes-stop", "--lane", "codex-governance"])
            self.assertEqual(rc, 0)
            self.assertEqual(stop.call_args.kwargs["lane_id"], "codex-governance")

    def test_lane_stop_alias_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.stop_lanes_background",
                return_value={"stopped_count": 1, "stopped": [{"id": "codex-governance"}]},
            ) as stop:
                rc = cli.main(["--root", str(root), "lane-stop", "--lane", "codex-governance"])
            self.assertEqual(rc, 0)
            self.assertEqual(stop.call_args.kwargs["lane_id"], "codex-governance")

    def test_lanes_stop_command_returns_nonzero_when_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.stop_lanes_background",
                return_value={"ok": False, "stopped_count": 0, "failed_count": 1},
            ):
                rc = cli.main(["--root", str(root), "lanes-stop"])
            self.assertEqual(rc, 1)

    def test_lanes_stop_command_infers_success_when_ok_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.stop_lanes_background",
                return_value={"stopped_count": 1, "failed_count": 0},
            ):
                rc = cli.main(["--root", str(root), "lanes-stop"])
            self.assertEqual(rc, 0)

    def test_lanes_status_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "running_count": 0,
                    "total_count": 1,
                    "lanes": [{"id": "lane-a", "owner": "codex", "running": False, "pid": None}],
                },
            ):
                rc = cli.main(["--root", str(root), "lanes-status"])
            self.assertEqual(rc, 0)

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
        filtered = cli._filter_lane_status_payload(
            payload,
            requested_lane="",
            lanes_file=pathlib.Path("/tmp/lanes.json"),
        )
        self.assertEqual(filtered["total_count"], 2)
        self.assertEqual(filtered["lanes"][0]["id"], "lane-a")
        self.assertEqual(filtered["lanes"][0]["owner"], "unknown")
        self.assertEqual(filtered["lanes"][0]["health"], "unknown")
        self.assertEqual(filtered["lanes"][0]["heartbeat_age_sec"], -1)
        self.assertEqual(filtered["lanes"][1]["id"], "unknown")
        self.assertEqual(filtered["lanes"][1]["owner"], "gemini")

    def test_lanes_status_command_handles_missing_lane_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "running_count": 0,
                    "total_count": 3,
                    "lanes": [{"id": "lane-a"}, {"owner": "gemini"}, "bad-entry"],
                },
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status"])
            self.assertEqual(rc, 0)
            output = buffer.getvalue()
            self.assertIn("- lane-a [unknown] stopped pid=None health=unknown heartbeat_age=-1s", output)
            self.assertIn("- unknown [gemini] stopped pid=None health=unknown heartbeat_age=-1s", output)

    def test_lanes_status_command_with_conversations_embeds_rollup(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "running_count": 1,
                "total_count": 1,
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 123, "health": "ok"}],
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
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ) as conversations:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--with-conversations"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            lane = data["lanes"][0]
            self.assertEqual(lane["conversation_source_state"], "ok")
            self.assertEqual(lane["conversation_event_count"], 1)
            self.assertEqual(lane["latest_conversation_event"]["event_type"], "status")
            self.assertIn("conversation_by_lane", data)
            self.assertTrue(data["conversation_ok"])
            self.assertFalse(data["conversation_partial"])
            kwargs = conversations.call_args.kwargs
            self.assertEqual(kwargs["lines"], 200)
            self.assertTrue(kwargs["include_lanes"])

    def test_lanes_status_with_conversations_uses_event_sequence_for_invalid_timestamps(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "running_count": 1,
                "total_count": 1,
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 123, "health": "ok"}],
            }
            conversation_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
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
                "sources": [{"lane_id": "lane-a", "ok": True, "error": "", "event_count": 2}],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--with-conversations"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            lane = data["lanes"][0]
            self.assertEqual(lane["latest_conversation_event"]["content"], "newer-invalid")

    def test_lanes_status_text_with_conversations_formats_latest_timestamp_in_local_tz(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "running_count": 1,
                "total_count": 1,
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 123, "health": "ok"}],
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
                "sources": [{"lane_id": "lane-a", "ok": True, "error": "", "event_count": 1}],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--with-conversations"])
            self.assertEqual(rc, 0)
            output = buffer.getvalue()
            self.assertIn("latest_ts=", output)
            self.assertNotIn("2026-01-01T00:00:01+00:00", output)
            self.assertIn("conversation_recovery: recovered_lanes=0", output)

    def test_lanes_status_text_with_conversations_reports_recovered_lane_count(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "ok": False,
                "partial": True,
                "errors": ["lane status source unavailable"],
                "running_count": 1,
                "total_count": 1,
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 123, "health": "ok"}],
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
                        "content": "lane-a ready",
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
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--with-conversations"])
            self.assertEqual(rc, 0)
            output = buffer.getvalue()
            self.assertIn("conversation_recovery: recovered_lanes=1", output)

    def test_lanes_status_command_with_lane_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "running_count": 1,
                    "total_count": 2,
                    "lanes": [
                        {"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"},
                        {"id": "lane-b", "owner": "gemini", "running": False, "pid": None, "health": "stopped"},
                    ],
                },
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["requested_lane"], "lane-a")
            self.assertEqual(data["total_count"], 1)
            self.assertEqual(data["lanes"][0]["id"], "lane-a")
            self.assertEqual(data["health_counts"], {"ok": 1})
            self.assertEqual(data["owner_counts"]["codex"]["running"], 1)

    def test_lanes_status_command_with_lane_filter_matches_case_insensitively(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "running_count": 1,
                    "total_count": 2,
                    "lanes": [
                        {"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"},
                        {"id": "lane-b", "owner": "gemini", "running": False, "pid": None, "health": "stopped"},
                    ],
                },
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--lane", "LANE-A"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["requested_lane"], "lane-a")
            self.assertEqual(data["total_count"], 1)
            self.assertEqual(data["lanes"][0]["id"], "lane-a")

    def test_lanes_status_with_lane_filter_suppresses_unrelated_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "ok": False,
                    "partial": True,
                    "errors": ["lane-b: heartbeat stale"],
                    "lanes": [
                        {"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"},
                        {"id": "lane-b", "owner": "gemini", "running": True, "pid": 200, "health": "stale"},
                    ],
                },
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertTrue(data["ok"])
            self.assertFalse(data["partial"])
            self.assertEqual(data["errors"], [])
            self.assertEqual(data["suppressed_errors"], ["lane-b: heartbeat stale"])
            self.assertEqual(data["lanes"][0]["id"], "lane-a")

    def test_lanes_status_with_lane_filter_keeps_global_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "ok": False,
                    "partial": True,
                    "errors": ["lane status source unavailable"],
                    "lanes": [
                        {"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"},
                    ],
                },
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertFalse(data["ok"])
            self.assertTrue(data["partial"])
            self.assertEqual(data["suppressed_errors"], [])
            self.assertIn("lane status source unavailable", data["errors"][0])

    def test_lanes_status_with_lane_filter_normalizes_scalar_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "ok": False,
                    "partial": True,
                    "errors": "lane status source unavailable",
                    "lanes": [
                        {"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"},
                    ],
                },
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertFalse(data["ok"])
            self.assertTrue(data["partial"])
            self.assertEqual(data["suppressed_errors"], [])
            self.assertEqual(data["errors"], ["lane status source unavailable"])

    def test_lanes_status_with_lane_filter_keeps_colon_global_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "ok": False,
                    "partial": True,
                    "errors": ["lane status source: timeout", "lane-b: heartbeat stale"],
                    "lanes": [
                        {"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"},
                        {"id": "lane-b", "owner": "gemini", "running": True, "pid": 200, "health": "stale"},
                    ],
                },
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertFalse(data["ok"])
            self.assertTrue(data["partial"])
            self.assertEqual(data["errors"], ["lane status source: timeout"])
            self.assertEqual(data["suppressed_errors"], ["lane-b: heartbeat stale"])

    def test_lane_status_alias_command_with_lane_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "running_count": 1,
                    "total_count": 2,
                    "lanes": [
                        {"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"},
                        {"id": "lane-b", "owner": "gemini", "running": False, "pid": None, "health": "stopped"},
                    ],
                },
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lane-status", "--json", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["requested_lane"], "lane-a")
            self.assertEqual(data["total_count"], 1)
            self.assertEqual(data["lanes"][0]["id"], "lane-a")
            self.assertEqual(data["health_counts"], {"ok": 1})
            self.assertEqual(data["owner_counts"]["codex"]["running"], 1)

    def test_lanes_status_command_returns_error_for_unknown_lane_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                return_value={
                    "lanes_file": "config/lanes.json",
                    "running_count": 1,
                    "total_count": 1,
                    "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 100}],
                },
            ):
                rc = cli.main(["--root", str(root), "lanes-status", "--lane", "missing"])
            self.assertEqual(rc, 1)

    def test_lanes_status_recovers_unknown_lane_with_conversation_rollup(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "errors": [],
                "running_count": 1,
                "total_count": 1,
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"}],
            }
            conversation_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "events": [
                    {
                        "timestamp": "2026-01-01T00:00:01+00:00",
                        "owner": "gemini",
                        "lane_id": "missing",
                        "event_type": "status",
                        "content": "recover from conversation",
                    }
                ],
                "sources": [{"lane_id": "missing", "ok": True, "error": "", "event_count": 1}],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(
                        [
                            "--root",
                            str(root),
                            "lanes-status",
                            "--json",
                            "--lane",
                            "missing",
                            "--with-conversations",
                        ]
                    )
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertFalse(data["ok"])
            self.assertTrue(data["partial"])
            self.assertEqual(data["recovered_lane_count"], 1)
            self.assertEqual(data["recovered_lanes"], ["missing"])
            self.assertEqual(data["lanes"][0]["id"], "missing")
            self.assertEqual(data["lanes"][0]["owner"], "gemini")
            self.assertEqual(
                data["errors"],
                ["Lane status missing for 'missing'; using conversation-derived fallback."],
            )

    def test_lanes_status_recovers_owner_from_source_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "errors": [],
                "running_count": 1,
                "total_count": 1,
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"}],
            }
            conversation_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "events": [],
                "sources": [{"lane_id": "missing", "owner": "gemini", "ok": True, "error": "", "event_count": 0}],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(
                        [
                            "--root",
                            str(root),
                            "lanes-status",
                            "--json",
                            "--lane",
                            "missing",
                            "--with-conversations",
                        ]
                    )
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["recovered_lane_count"], 1)
            self.assertEqual(data["lanes"][0]["id"], "missing")
            self.assertEqual(data["lanes"][0]["owner"], "gemini")

    def test_lanes_status_recovery_clears_requested_lane_unavailable_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "ok": False,
                "partial": True,
                "errors": [
                    "lane status source unavailable",
                    "Requested lane 'missing' is unavailable because lane status sources failed.",
                ],
                "running_count": 1,
                "total_count": 1,
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"}],
            }
            conversation_payload = {
                "ok": True,
                "partial": False,
                "errors": [],
                "events": [
                    {
                        "timestamp": "2026-01-01T00:00:01+00:00",
                        "owner": "gemini",
                        "lane_id": "missing",
                        "event_type": "status",
                        "content": "recover from conversation",
                    }
                ],
                "sources": [{"lane_id": "missing", "ok": True, "error": "", "event_count": 1}],
            }
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(
                        [
                            "--root",
                            str(root),
                            "lanes-status",
                            "--json",
                            "--lane",
                            "missing",
                            "--with-conversations",
                        ]
                    )
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["recovered_lane_count"], 1)
            self.assertNotIn(
                "Requested lane 'missing' is unavailable because lane status sources failed.",
                data["errors"],
            )
            self.assertIn("lane status source unavailable", data["errors"])
            self.assertIn(
                "Lane status missing for 'missing'; using conversation-derived fallback.",
                data["errors"],
            )

    def test_lanes_status_recovers_partial_missing_lane_with_conversation_rollup(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            lane_payload = {
                "lanes_file": "config/lanes.json",
                "ok": False,
                "partial": True,
                "errors": ["lane status source unavailable"],
                "running_count": 1,
                "total_count": 1,
                "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "pid": 100, "health": "ok"}],
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
            with mock.patch("orxaq_autonomy.cli.lane_status_snapshot", return_value=lane_payload), mock.patch(
                "orxaq_autonomy.cli.conversations_snapshot",
                return_value=conversation_payload,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(
                        [
                            "--root",
                            str(root),
                            "lanes-status",
                            "--json",
                            "--with-conversations",
                        ]
                    )
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["recovered_lane_count"], 1)
            self.assertEqual(data["recovered_lanes"], ["lane-b"])
            self.assertEqual(data["total_count"], 2)
            lane_b = next(item for item in data["lanes"] if item["id"] == "lane-b")
            self.assertEqual(lane_b["owner"], "gemini")
            self.assertTrue(lane_b["conversation_lane_fallback"])
            self.assertIn(
                "Lane status missing for 'lane-b'; using conversation-derived fallback.",
                data["errors"],
            )

    def test_lanes_status_command_degrades_when_lane_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.lane_status_snapshot",
                side_effect=RuntimeError("lane status read failed"),
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "lanes-status", "--json", "--lane", "lane-a"])
            self.assertEqual(rc, 0)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["requested_lane"], "lane-a")
            self.assertEqual(payload["total_count"], 0)
            self.assertTrue(payload["errors"])
            self.assertIn("lane status read failed", payload["errors"][0])

    def test_lanes_ensure_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.ensure_lanes_background",
                return_value={
                    "ensured_count": 1,
                    "started_count": 1,
                    "restarted_count": 0,
                    "failed_count": 0,
                    "ok": True,
                },
            ) as ensure:
                rc = cli.main(["--root", str(root), "lanes-ensure"])
            self.assertEqual(rc, 0)
            self.assertIsNone(ensure.call_args.kwargs["lane_id"])

    def test_lanes_ensure_command_with_lane_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.ensure_lanes_background",
                return_value={
                    "requested_lane": "codex-governance",
                    "ensured_count": 1,
                    "started_count": 0,
                    "restarted_count": 0,
                    "failed_count": 0,
                    "ok": True,
                },
            ) as ensure:
                rc = cli.main(["--root", str(root), "lanes-ensure", "--lane", "codex-governance"])
            self.assertEqual(rc, 0)
            self.assertEqual(ensure.call_args.kwargs["lane_id"], "codex-governance")

    def test_lane_ensure_alias_command_with_lane_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.ensure_lanes_background",
                return_value={
                    "requested_lane": "codex-governance",
                    "ensured_count": 1,
                    "started_count": 0,
                    "restarted_count": 0,
                    "failed_count": 0,
                    "ok": True,
                },
            ) as ensure:
                rc = cli.main(["--root", str(root), "lane-ensure", "--lane", "codex-governance", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(ensure.call_args.kwargs["lane_id"], "codex-governance")


if __name__ == "__main__":
    unittest.main()
