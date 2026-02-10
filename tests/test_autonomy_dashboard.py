import json
import pathlib
import sys
import tempfile
import unittest
from io import StringIO
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) in sys.path:
    sys.path.remove(str(SRC))
sys.path.insert(0, str(SRC))

from orxaq_autonomy import cli, manager


class DashboardRenderingTests(unittest.TestCase):
    def _prep_root(self, root: pathlib.Path) -> manager.ManagerConfig:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "state").mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "autonomy").mkdir(parents=True, exist_ok=True)
        (root / "config" / "tasks.json").write_text("[]\n", encoding="utf-8")
        (root / "config" / "objective.md").write_text("objective\n", encoding="utf-8")
        (root / "config" / "codex_result.schema.json").write_text("{}\n", encoding="utf-8")
        (root / "config" / "skill_protocol.json").write_text("{}\n", encoding="utf-8")
        (root / ".env.autonomy").write_text("OPENAI_API_KEY=test\nGEMINI_API_KEY=test\n", encoding="utf-8")
        return manager.ManagerConfig.from_root(root)

    def test_health_dashboard_renders_distributed_todo_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": "pending"},
                        "todo-b": {"status": "in_progress"},
                        "todo-c": {"status": "done"},
                        "todo-d": {"status": "blocked"},
                        "todo-e": {"status": "invalid_status"},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["pending"], 1)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 1)
            self.assertEqual(snapshot["state_counts"]["done"], 1)
            self.assertEqual(snapshot["state_counts"]["blocked"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 1)
            self.assertEqual(snapshot["blocked_tasks"], ["todo-d"])

    def test_health_dashboard_normalizes_status_and_non_dict_todos(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": " BLOCKED "},
                        "todo-b": {"status": " Done "},
                        "todo-c": {"status": " In_Progress "},
                        "todo-d": "pending",
                        "todo-e": None,
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["blocked"], 1)
            self.assertEqual(snapshot["state_counts"]["done"], 1)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 2)
            self.assertEqual(snapshot["blocked_tasks"], ["todo-a"])

    def test_health_dashboard_normalizes_spaced_and_hyphenated_in_progress_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": "In Progress"},
                        "todo-b": {"status": "IN-PROGRESS"},
                        "todo-c": {"status": "done"},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 2)
            self.assertEqual(snapshot["state_counts"]["done"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 0)

    def test_health_dashboard_normalizes_mixed_whitespace_and_hyphen_in_progress_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": " in-\tprogress "},
                        "todo-b": {"status": "pending"},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 1)
            self.assertEqual(snapshot["state_counts"]["pending"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 0)

    def test_health_dashboard_normalizes_crlf_and_repeated_hyphen_in_progress_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": " \r\nIN--\r\nPROGRESS\t"},
                        "todo-b": {"status": "blocked"},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 1)
            self.assertEqual(snapshot["state_counts"]["blocked"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 0)
            self.assertEqual(snapshot["blocked_tasks"], ["todo-b"])

    def test_health_dashboard_counts_missing_or_non_string_dict_status_as_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": " BLOCKED "},
                        "todo-b": {},
                        "todo-c": {"status": None},
                        "todo-d": {"status": 5},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["blocked"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 3)
            self.assertEqual(snapshot["blocked_tasks"], ["todo-a"])

    def test_health_dashboard_counts_boolean_dict_status_as_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": True},
                        "todo-b": {"status": "done"},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["done"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 1)
            self.assertEqual(snapshot["blocked_tasks"], [])

    def test_health_dashboard_collects_all_normalized_blocked_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": "blocked"},
                        "todo-b": {"status": " BLOCKED "},
                        "todo-c": {"status": "in-progress"},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["blocked"], 2)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 1)
            self.assertEqual(snapshot["blocked_tasks"], ["todo-a", "todo-b"])

    def test_health_dashboard_treats_repeated_underscore_in_progress_status_as_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": "IN__PROGRESS"},
                        "todo-b": {"status": "done"},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 0)
            self.assertEqual(snapshot["state_counts"]["done"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 1)

    def test_health_dashboard_counts_malformed_distributed_state_payload_as_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text("{invalid-json", encoding="utf-8")
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["pending"], 0)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 0)
            self.assertEqual(snapshot["state_counts"]["done"], 0)
            self.assertEqual(snapshot["state_counts"]["blocked"], 0)
            self.assertEqual(snapshot["state_counts"]["unknown"], 1)
            self.assertEqual(snapshot["blocked_tasks"], [])

    def test_health_dashboard_counts_non_dict_distributed_state_payload_as_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(json.dumps(["todo-a", "todo-b"]), encoding="utf-8")
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["pending"], 0)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 0)
            self.assertEqual(snapshot["state_counts"]["done"], 0)
            self.assertEqual(snapshot["state_counts"]["blocked"], 0)
            self.assertEqual(snapshot["state_counts"]["unknown"], 1)
            self.assertEqual(snapshot["blocked_tasks"], [])

    def test_health_dashboard_keeps_counts_zero_when_distributed_state_file_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.unlink(missing_ok=True)
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["pending"], 0)
            self.assertEqual(snapshot["state_counts"]["in_progress"], 0)
            self.assertEqual(snapshot["state_counts"]["done"], 0)
            self.assertEqual(snapshot["state_counts"]["blocked"], 0)
            self.assertEqual(snapshot["state_counts"]["unknown"], 0)
            self.assertEqual(snapshot["blocked_tasks"], [])

    def test_status_dashboard_renders_activity_log_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="worker heartbeat\ndistributed todo synced",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn("--- logs ---", rendered)
            self.assertIn("distributed todo synced", rendered)

    def test_status_dashboard_renders_activity_section_for_zero_like_non_empty_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="0",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn("--- logs ---", rendered)
            self.assertIn("\n0\n", rendered)

    def test_status_dashboard_renders_activity_section_for_newline_prefixed_non_empty_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="\n\t distributed todo synced",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn("--- logs ---", rendered)
            self.assertIn("distributed todo synced", rendered)

    def test_status_dashboard_renders_activity_section_for_crlf_prefixed_non_empty_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="\r\n distributed todo synced",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn("--- logs ---", rendered)
            self.assertIn("distributed todo synced", rendered)

    def test_status_dashboard_renders_activity_section_for_non_ok_status_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch(
                "orxaq_autonomy.cli.status_snapshot",
                return_value={"ok": False, "error": "runner unhealthy"},
            ), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="distributed todo synced",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn('"ok": false', rendered.lower())
            self.assertIn("--- logs ---", rendered)
            self.assertIn("distributed todo synced", rendered)

    def test_status_dashboard_renders_activity_section_for_non_ok_status_snapshot_with_newline_prefixed_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch(
                "orxaq_autonomy.cli.status_snapshot",
                return_value={"ok": False, "error": "runner unhealthy"},
            ), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="\n distributed todo synced",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn('"ok": false', rendered.lower())
            self.assertIn("--- logs ---", rendered)
            self.assertIn("distributed todo synced", rendered)

    def test_status_dashboard_omits_activity_section_for_empty_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn('"ok": true', rendered.lower())
            self.assertNotIn("--- logs ---", rendered)

    def test_status_dashboard_omits_activity_section_for_whitespace_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="   \n\t",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertNotIn("--- logs ---", rendered)

    def test_status_dashboard_omits_activity_section_for_crlf_whitespace_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="\r\n\r\n\t",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn('"ok": true', rendered.lower())
            self.assertNotIn("--- logs ---", rendered)

    def test_status_dashboard_omits_activity_section_for_whitespace_tail_when_snapshot_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch(
                "orxaq_autonomy.cli.status_snapshot",
                return_value={"ok": False, "error": "runner unhealthy"},
            ), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="   \n\t",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn('"ok": false', rendered.lower())
            self.assertNotIn("--- logs ---", rendered)

    def test_status_dashboard_omits_activity_section_for_crlf_whitespace_tail_when_snapshot_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch(
                "orxaq_autonomy.cli.status_snapshot",
                return_value={"ok": False, "error": "runner unhealthy"},
            ), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value="\r\n \r\n\t",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn('"ok": false', rendered.lower())
            self.assertNotIn("--- logs ---", rendered)

    def test_status_dashboard_omits_activity_section_for_non_string_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value=None,
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertNotIn("--- logs ---", rendered)

    def test_status_dashboard_omits_activity_section_for_bytes_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value=b"distributed todo synced",
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertNotIn("--- logs ---", rendered)

    def test_status_dashboard_omits_activity_section_for_non_string_tail_when_snapshot_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch(
                "orxaq_autonomy.cli.status_snapshot",
                return_value={"ok": False, "error": "runner unhealthy"},
            ), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value={"log": "distributed todo synced"},
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertIn('"ok": false', rendered.lower())
            self.assertNotIn("--- logs ---", rendered)


if __name__ == "__main__":
    unittest.main()
