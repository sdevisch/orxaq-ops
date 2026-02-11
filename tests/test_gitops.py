import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.gitops import pr_merge


class GitOpsTests(unittest.TestCase):
    def test_pr_merge_blocks_low_health(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            health = root / "health.json"
            health.write_text(json.dumps({"score": 70}), encoding="utf-8")
            payload = pr_merge(root=root, pr="1", health_report_path=health, min_score=85)
            self.assertFalse(payload["ok"])

    def test_pr_merge_calls_gh_when_score_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            health = root / "health.json"
            health.write_text(json.dumps({"score": 95}), encoding="utf-8")
            cp = mock.Mock(returncode=0, stdout="ok", stderr="")
            with mock.patch("orxaq_autonomy.gitops.subprocess.run", return_value=cp) as run:
                payload = pr_merge(root=root, pr="1", health_report_path=health, min_score=85)
            self.assertTrue(payload["ok"])
            run.assert_called()


if __name__ == "__main__":
    unittest.main()
