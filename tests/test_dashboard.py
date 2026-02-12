import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import error as urllib_error
from urllib import request as urllib_request


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import dashboard


class DashboardTests(unittest.TestCase):
    def _write_fixture_artifacts(self, root: pathlib.Path) -> None:
        (root / "health.json").write_text('{"ok": true}\n', encoding="utf-8")
        (root / "health.md").write_text("# Health\n", encoding="utf-8")
        (root / "W12_A_run.json").write_text('{"block":"W12-A"}\n', encoding="utf-8")
        (root / "W12_A_summary.md").write_text("# W12-A\n", encoding="utf-8")
        evidence = root / "rpa_evidence" / "run-1" / "task-1"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "dom_snapshot.html").write_text("<html></html>\n", encoding="utf-8")

    def test_collect_dashboard_index_contains_expected_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            self._write_fixture_artifacts(root)
            payload = dashboard.collect_dashboard_index(root)
            self.assertIn("health.json", payload["health_json"])
            self.assertIn("health.md", payload["health_md"])
            self.assertIn("W12_A_run.json", payload["run_reports"])
            self.assertIn("W12_A_summary.md", payload["run_summaries"])
            self.assertTrue(any(row.startswith("rpa_evidence/run-1/task-1") for row in payload["evidence_dirs"]))

    def test_resolve_artifact_path_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            allowed = dashboard.resolve_artifact_path(root, "health.json")
            blocked = dashboard.resolve_artifact_path(root, "../secrets.txt")
            self.assertIsNotNone(allowed)
            self.assertIsNone(blocked)

    def test_dashboard_routes_and_traversal_protection(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            self._write_fixture_artifacts(root)

            handler = dashboard.make_dashboard_handler(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/", timeout=5) as resp:
                    body = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertIn("Orxaq Autonomy Dashboard", body)

                with urllib_request.urlopen(f"{base}/api/index", timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, 200)
                    self.assertIn("health_json", payload)

                with urllib_request.urlopen(f"{base}/api/health", timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, 200)
                    self.assertTrue(payload.get("ok"))

                with urllib_request.urlopen(f"{base}/file/health.json", timeout=5) as resp:
                    body = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertIn("{&quot;ok&quot;: true}", body)

                with self.assertRaises(urllib_error.HTTPError) as ctx:
                    urllib_request.urlopen(f"{base}/file/%2e%2e/secrets.txt", timeout=5)
                self.assertEqual(ctx.exception.code, 403)
                ctx.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
