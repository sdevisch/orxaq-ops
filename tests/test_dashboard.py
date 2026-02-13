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


class DistributedTodoAggregationTests(unittest.TestCase):
    """Tests for distributed todo aggregation (Issue #17)."""

    def test_aggregate_single_source(self):
        sources = [{"t1": {"status": "done"}, "t2": {"status": "pending"}}]
        result = dashboard.aggregate_distributed_todos(sources)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["covered"], 1)
        self.assertEqual(result["uncovered"], 1)
        self.assertEqual(result["total"], result["covered"] + result["uncovered"])

    def test_aggregate_multiple_sources_deduplicates(self):
        sources = [
            {"t1": {"status": "pending", "last_update": "2026-01-01T00:00:00Z"}},
            {"t1": {"status": "done", "last_update": "2026-01-02T00:00:00Z"}},
        ]
        result = dashboard.aggregate_distributed_todos(sources)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["covered"], 1)
        # Newer update wins
        self.assertEqual(result["tasks"]["t1"]["status"], "done")

    def test_aggregate_multiple_sources_merges(self):
        sources = [
            {"t1": {"status": "done"}},
            {"t2": {"status": "blocked"}, "t3": {"status": "pending"}},
        ]
        result = dashboard.aggregate_distributed_todos(sources)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["covered"], 1)
        self.assertEqual(result["uncovered"], 2)

    def test_aggregate_empty_sources(self):
        result = dashboard.aggregate_distributed_todos([])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["covered"], 0)
        self.assertEqual(result["uncovered"], 0)

    def test_aggregate_skips_non_dict_entries(self):
        sources = [{"t1": {"status": "done"}, "t2": "not-a-dict"}]
        result = dashboard.aggregate_distributed_todos(sources)
        self.assertEqual(result["total"], 1)

    def test_aggregate_skips_non_dict_payloads(self):
        sources = [{"t1": {"status": "done"}}, "not-a-dict", None]
        result = dashboard.aggregate_distributed_todos(sources)
        self.assertEqual(result["total"], 1)

    def test_aggregate_total_always_consistent(self):
        """Total must always equal covered + uncovered regardless of input."""
        sources = [
            {
                "t1": {"status": "done"},
                "t2": {"status": "pending"},
                "t3": {"status": "in_progress"},
                "t4": {"status": "blocked"},
                "t5": {"status": "weird_custom_status"},
            }
        ]
        result = dashboard.aggregate_distributed_todos(sources)
        self.assertEqual(result["total"], result["covered"] + result["uncovered"])

    def test_aggregate_handles_dict_subclass(self):
        """Ensure dict subclasses are handled correctly (Issue #22)."""

        class TaskDict(dict):
            pass

        state = TaskDict({"t1": {"status": "done"}, "t2": {"status": "pending"}})
        sources = [state]
        result = dashboard.aggregate_distributed_todos(sources)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["total"], result["covered"] + result["uncovered"])


class TodoActivityWidgetTests(unittest.TestCase):
    """Tests for the todo activity widget rendering (Issues #16, #17)."""

    def test_empty_state_shows_no_tasks(self):
        html = dashboard.render_todo_activity_widget({})
        self.assertIn("No tasks found", html)
        self.assertIn("aria-label='Task Activity'", html)

    def test_none_state_shows_no_tasks(self):
        html = dashboard.render_todo_activity_widget(None)
        self.assertIn("No tasks found", html)

    def test_renders_task_rows(self):
        state = {
            "t1": {"status": "done", "last_update": "2026-01-02T00:00:00Z", "last_summary": "All good"},
            "t2": {"status": "pending", "last_update": "2026-01-01T00:00:00Z"},
        }
        html = dashboard.render_todo_activity_widget(state)
        self.assertIn("t1", html)
        self.assertIn("t2", html)
        self.assertIn("Done", html)
        self.assertIn("Pending", html)
        self.assertIn("All good", html)

    def test_table_has_accessibility_roles(self):
        state = {"t1": {"status": "done"}}
        html = dashboard.render_todo_activity_widget(state)
        self.assertIn("role='table'", html)
        self.assertIn("scope='col'", html)
        self.assertIn("role='status'", html)
        self.assertIn("aria-label='Recent task activity'", html)

    def test_status_badges_have_aria_labels(self):
        state = {"t1": {"status": "blocked"}}
        html = dashboard.render_todo_activity_widget(state)
        self.assertIn("aria-label='Status: Blocked'", html)
        self.assertIn("status-blocked", html)

    def test_summary_line_shows_correct_counts(self):
        state = {
            "t1": {"status": "done"},
            "t2": {"status": "done"},
            "t3": {"status": "pending"},
        }
        html = dashboard.render_todo_activity_widget(state)
        self.assertIn("<strong>2</strong> done", html)
        self.assertIn("<strong>1</strong> remaining", html)
        self.assertIn("<strong>3</strong> total", html)

    def test_summary_total_consistent(self):
        state = {
            "t1": {"status": "done"},
            "t2": {"status": "blocked"},
            "t3": {"status": "in_progress"},
            "t4": {"status": "unknown_custom"},
        }
        html = dashboard.render_todo_activity_widget(state)
        # 1 done + 3 remaining = 4 total
        self.assertIn("<strong>1</strong> done", html)
        self.assertIn("<strong>3</strong> remaining", html)
        self.assertIn("<strong>4</strong> total", html)

    def test_max_items_limits_rows(self):
        state = {f"t{i}": {"status": "pending"} for i in range(20)}
        html = dashboard.render_todo_activity_widget(state, max_items=5)
        # Should contain at most 5 <tr> in tbody
        self.assertLessEqual(html.count("<tr>"), 6)  # 5 data rows + 1 header row

    def test_sorts_by_last_update_descending(self):
        state = {
            "old": {"status": "pending", "last_update": "2026-01-01T00:00:00Z"},
            "new": {"status": "pending", "last_update": "2026-01-03T00:00:00Z"},
            "mid": {"status": "pending", "last_update": "2026-01-02T00:00:00Z"},
        }
        html = dashboard.render_todo_activity_widget(state)
        # "new" should appear before "old" in the rendered output
        idx_new = html.index("new")
        idx_old = html.index("old")
        self.assertLess(idx_new, idx_old)

    def test_skips_non_dict_task_entries(self):
        state = {"t1": {"status": "done"}, "t2": "not-a-dict"}
        html = dashboard.render_todo_activity_widget(state)
        self.assertIn("t1", html)
        # Non-dict entry should not appear as a row
        self.assertIn("<strong>1</strong> total", html)

    def test_escapes_html_in_task_ids(self):
        state = {"<script>alert(1)</script>": {"status": "done"}}
        html = dashboard.render_todo_activity_widget(state)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class DashboardAccessibilityTests(unittest.TestCase):
    """Tests for dashboard accessibility improvements (Issue #16)."""

    def _make_payload(self, **overrides):
        base = {
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
        base.update(overrides)
        return base

    def test_html_has_skip_link(self):
        rendered = dashboard.render_dashboard_html(self._make_payload())
        self.assertIn("skip-link", rendered)
        self.assertIn("Skip to main content", rendered)

    def test_html_has_main_landmark(self):
        rendered = dashboard.render_dashboard_html(self._make_payload())
        self.assertIn("role=\"main\"", rendered)
        self.assertIn("id=\"main-content\"", rendered)

    def test_html_has_banner_landmark(self):
        rendered = dashboard.render_dashboard_html(self._make_payload())
        self.assertIn("role=\"banner\"", rendered)

    def test_sections_have_aria_labels(self):
        payload = self._make_payload(health_json=["health.json"])
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("aria-label='Health JSON'", rendered)

    def test_file_links_show_count_badge(self):
        payload = self._make_payload(
            health_json=["health.json"],
            run_reports=["r1.json", "r2.json"],
        )
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("class='badge'", rendered)
        self.assertIn("1 items", rendered)
        self.assertIn("2 items", rendered)

    def test_empty_section_no_count_badge(self):
        payload = self._make_payload(health_json=[])
        rendered = dashboard.render_dashboard_html(payload)
        # The Health JSON section should show "None found" without a badge
        self.assertIn("None found", rendered)

    def test_todo_activity_widget_in_rendered_html(self):
        payload = self._make_payload(
            task_state={
                "t1": {"status": "done", "last_update": "2026-01-01T00:00:00Z"},
                "t2": {"status": "pending", "last_update": "2026-01-01T00:00:00Z"},
            }
        )
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("Task Activity", rendered)
        self.assertIn("activity-table", rendered)

    def test_todo_activity_widget_absent_when_no_task_state(self):
        payload = self._make_payload()
        rendered = dashboard.render_dashboard_html(payload)
        # Should still have the section but say "No tasks found"
        self.assertIn("No tasks found", rendered)

    def test_uses_list_roles_for_file_links(self):
        payload = self._make_payload(health_json=["health.json"])
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("role='list'", rendered)

    def test_staleness_non_dict_handled(self):
        """Staleness as non-dict should not crash (Issue #22 edge case)."""
        payload = self._make_payload(staleness="not-a-dict")
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("Orxaq Autonomy Dashboard", rendered)
        self.assertNotIn("Stale data", rendered)

    def test_errors_non_list_handled(self):
        """Errors as non-list should not crash (Issue #22 edge case)."""
        payload = self._make_payload(errors="not-a-list")
        rendered = dashboard.render_dashboard_html(payload)
        self.assertIn("Orxaq Autonomy Dashboard", rendered)
        self.assertNotIn("Partial data", rendered)


if __name__ == "__main__":
    unittest.main()
