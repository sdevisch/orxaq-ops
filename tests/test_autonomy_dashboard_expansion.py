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


class DashboardExpansionTests(unittest.TestCase):
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

    def test_health_dashboard_normalizes_explicit_unknown_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = self._prep_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "todo-a": {"status": " Unknown "},
                        "todo-b": {"status": "done"},
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.health_snapshot(cfg)
            self.assertEqual(snapshot["state_counts"]["done"], 1)
            self.assertEqual(snapshot["state_counts"]["unknown"], 1)
            self.assertEqual(snapshot["blocked_tasks"], [])

    def test_status_dashboard_omits_activity_section_for_integer_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prep_root(root)
            output = StringIO()
            with mock.patch("orxaq_autonomy.cli.status_snapshot", return_value={"ok": True}), mock.patch(
                "orxaq_autonomy.cli.tail_logs",
                return_value=0,
            ), mock.patch("sys.stdout", output):
                rc = cli.main(["--root", str(root), "status"])
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertNotIn("--- logs ---", rendered)


if __name__ == "__main__":
    unittest.main()
