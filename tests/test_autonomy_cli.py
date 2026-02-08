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
                }
            }
            with mock.patch("orxaq_autonomy.cli.monitor_snapshot", return_value=payload):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = cli.main(["--root", str(root), "metrics", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["responses_total"], 3)

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
            ):
                rc = cli.main(["--root", str(root), "lanes-ensure"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
