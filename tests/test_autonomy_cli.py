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

    def test_pr_open_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.detect_repo", return_value="acme/repo"), mock.patch(
                "orxaq_autonomy.cli.detect_head_branch",
                return_value="codex/test",
            ), mock.patch("orxaq_autonomy.cli.open_pr", return_value={"ok": True, "number": 11}):
                rc = cli.main(["--root", str(root), "pr-open", "--title", "Test PR"])
            self.assertEqual(rc, 0)

    def test_pr_wait_non_ok_returns_one(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            with mock.patch("orxaq_autonomy.cli.detect_repo", return_value="acme/repo"), mock.patch(
                "orxaq_autonomy.cli.wait_for_pr",
                return_value={"ok": False, "reason": "failed"},
            ):
                rc = cli.main(["--root", str(root), "pr-wait", "--pr", "7"])
            self.assertEqual(rc, 1)

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
            report = root / "artifacts" / "AUTONOMY_STOP_REPORT.md"
            with mock.patch("orxaq_autonomy.cli.stop_background"), mock.patch(
                "orxaq_autonomy.cli.build_stop_report",
                return_value=report,
            ) as builder:
                rc = cli.main(["--root", str(root), "stop", "--report"])
            self.assertEqual(rc, 0)
            builder.assert_called_once()


if __name__ == "__main__":
    unittest.main()
