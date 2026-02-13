import json
import os
import pathlib
import sys
import tempfile
import threading
import time
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

    def test_collect_dashboard_index_includes_staleness(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            self._write_fixture_artifacts(root)
            payload = dashboard.collect_dashboard_index(root)
            self.assertIn("staleness", payload)
            staleness = payload["staleness"]
            self.assertIn("stale", staleness)
            self.assertIn("age_sec", staleness)
            # Just-written health.json should not be stale
            self.assertFalse(staleness["stale"])

    def test_collect_dashboard_index_stale_health_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            health = root / "health.json"
            health.write_text('{"ok": true}\n', encoding="utf-8")
            # Backdate the file to 2 hours ago
            old_time = time.time() - 7200
            os.utime(health, (old_time, old_time))
            payload = dashboard.collect_dashboard_index(root)
            self.assertTrue(payload["staleness"]["stale"])
            self.assertEqual(payload["staleness"]["reason"], "age_exceeded")

    def test_collect_dashboard_index_no_health_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            payload = dashboard.collect_dashboard_index(root)
            self.assertTrue(payload["staleness"]["stale"])
            self.assertEqual(payload["staleness"]["reason"], "no_health_artifact")

    def test_collect_dashboard_index_empty_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            payload = dashboard.collect_dashboard_index(root)
            self.assertEqual(payload["health_json"], [])
            self.assertEqual(payload["evidence_dirs"], [])
            self.assertNotIn("errors", payload)

    def test_resolve_artifact_path_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            allowed = dashboard.resolve_artifact_path(root, "health.json")
            blocked = dashboard.resolve_artifact_path(root, "../secrets.txt")
            self.assertIsNotNone(allowed)
            self.assertIsNone(blocked)

    def test_resolve_artifact_path_blocks_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            self.assertIsNone(dashboard.resolve_artifact_path(root, "."))

    def test_render_dashboard_html_stale_banner_present(self):
        payload = {
            "generated_at_utc": "2026-02-13T00:00:00+00:00",
            "artifacts_root": "/tmp",
            "health_json": [],
            "health_md": [],
            "run_reports": [],
            "run_summaries": [],
            "pr_review_snapshots": [],
            "evidence_dirs": [],
            "evidence_files": [],
            "staleness": {"stale": True, "reason": "age_exceeded", "age_sec": 9999},
        }
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("Stale data", rendered)
        self.assertIn("role='alert'", rendered)
        self.assertIn("age_exceeded", rendered)

    def test_render_dashboard_html_no_stale_banner_when_fresh(self):
        payload = {
            "generated_at_utc": "2026-02-13T00:00:00+00:00",
            "artifacts_root": "/tmp",
            "health_json": [],
            "health_md": [],
            "run_reports": [],
            "run_summaries": [],
            "pr_review_snapshots": [],
            "evidence_dirs": [],
            "evidence_files": [],
            "staleness": {"stale": False, "reason": "ok", "age_sec": 10},
        }
        rendered = dashboard.render_dashboard_html(payload)
        self.assertNotIn("Stale data", rendered)

    def test_render_dashboard_html_error_banner(self):
        payload = {
            "generated_at_utc": "2026-02-13T00:00:00+00:00",
            "artifacts_root": "/tmp",
            "health_json": [],
            "health_md": [],
            "run_reports": [],
            "run_summaries": [],
            "pr_review_snapshots": [],
            "evidence_dirs": [],
            "evidence_files": [],
            "staleness": {"stale": False, "reason": "ok", "age_sec": 10},
            "errors": ["evidence_dirs: Permission denied"],
        }
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("Partial data", rendered)
        self.assertIn("Permission denied", rendered)

    def test_render_dashboard_html_uses_time_element(self):
        payload = {
            "generated_at_utc": "2026-02-13T00:00:00+00:00",
            "artifacts_root": "/tmp",
            "health_json": [],
            "health_md": [],
            "run_reports": [],
            "run_summaries": [],
            "pr_review_snapshots": [],
            "evidence_dirs": [],
            "evidence_files": [],
            "staleness": {"stale": False, "reason": "ok", "age_sec": 10},
        }
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("<time datetime=", rendered)

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
                    self.assertIn("staleness", payload)

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

    def test_api_index_returns_staleness_and_no_errors_for_valid_root(self):
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
                with urllib_request.urlopen(f"{base}/api/index", timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("staleness", payload)
                    self.assertFalse(payload["staleness"]["stale"])
                    self.assertNotIn("errors", payload)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
