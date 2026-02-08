import pathlib
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
