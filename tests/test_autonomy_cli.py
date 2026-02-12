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

    def test_stop_command_writes_payload(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.autonomy_stop",
                return_value={"ok": True, "report_path": "artifacts/autonomy/AUTONOMY_STOP_REPORT.md"},
            ) as stop:
                rc = cli.main(
                    [
                        "--root",
                        str(root),
                        "stop",
                        "--reason",
                        "manual intervention",
                        "--file-issue",
                        "--issue-repo",
                        "Orxaq/orxaq-ops",
                        "--issue-label",
                        "autonomy",
                    ]
                )
            self.assertEqual(rc, 0)
            stop.assert_called_once()

    def test_router_check_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.run_router_check",
                return_value={"summary": {"overall_ok": True}, "providers": []},
            ) as check:
                rc = cli.main(
                    [
                        "--root",
                        str(root),
                        "router-check",
                        "--config",
                        "./config/router.example.yaml",
                        "--output",
                        "./artifacts/router_check.json",
                        "--lane",
                        "L0",
                        "--strict",
                    ]
                )
            self.assertEqual(rc, 0)
            check.assert_called_once()

    def test_profile_apply_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.apply_router_profile",
                return_value={"ok": True, "profile": "local"},
            ) as apply_profile:
                rc = cli.main(
                    [
                        "--root",
                        str(root),
                        "router-profile-apply",
                        "local",
                        "--config",
                        "./config/router.example.yaml",
                        "--profiles-dir",
                        "./router_profiles",
                        "--output",
                        "./config/router.active.yaml",
                    ]
                )
            self.assertEqual(rc, 0)
            apply_profile.assert_called_once()

    def test_rpa_schedule_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.run_rpa_schedule_from_config",
                return_value={"ok": True, "jobs_total": 0},
            ) as schedule:
                rc = cli.main(
                    [
                        "--root",
                        str(root),
                        "rpa-schedule",
                        "--config",
                        "./config/rpa_schedule.example.json",
                        "--output",
                        "./artifacts/autonomy/rpa_scheduler_report.json",
                        "--strict",
                    ]
                )
            self.assertEqual(rc, 0)
            schedule.assert_called_once()

    def test_dashboard_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.run_dashboard_server") as serve:
                rc = cli.main(
                    [
                        "--root",
                        str(root),
                        "dashboard",
                        "--artifacts-dir",
                        "./artifacts",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8989",
                    ]
                )
            self.assertEqual(rc, 0)
            serve.assert_called_once()

    def test_dashboard_ensure_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.dashboard_ensure", return_value={"ok": True}) as ensure:
                rc = cli.main(
                    [
                        "--root",
                        str(root),
                        "dashboard-ensure",
                        "--artifacts-dir",
                        "./artifacts",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                        "--refresh-sec",
                        "5",
                        "--port-scan",
                        "0",
                        "--no-browser",
                    ]
                )
            self.assertEqual(rc, 0)
            ensure.assert_called_once()

    def test_providers_check_strict_failure_returns_one(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.run_providers_check",
                return_value={"summary": {"all_required_up": False}},
            ):
                rc = cli.main(["--root", str(root), "providers-check", "--strict"])
            self.assertEqual(rc, 1)

    def test_task_queue_validate_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            rc = cli.main(["--root", str(root), "task-queue-validate"])
            self.assertEqual(rc, 0)

    def test_profile_apply(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            (root / "profiles").mkdir(parents=True, exist_ok=True)
            (root / "profiles" / "local.yaml").write_text('{"name":"local"}\n', encoding="utf-8")
            rc = cli.main(["--root", str(root), "profile-apply", "local"])
            self.assertEqual(rc, 0)
            self.assertTrue((root / "config" / "providers.active.yaml").exists())

    def test_stop_with_report_calls_stop_report_builder(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch(
                "orxaq_autonomy.cli.autonomy_stop",
                return_value={"ok": True, "report_path": "artifacts/autonomy/AUTONOMY_STOP_REPORT.md"},
            ) as stop:
                rc = cli.main(["--root", str(root), "stop", "--report"])
            self.assertEqual(rc, 0)
            stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
