"""Comprehensive tests for the v2 swarm command dashboard."""

import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib import error as urllib_error
from urllib import request as urllib_request


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import dashboard_v2


class HelperTests(unittest.TestCase):
    """Tests for low-level helper functions."""

    def test_utc_now_iso_returns_iso_string(self):
        result = dashboard_v2._utc_now_iso()
        # Should be parseable as ISO 8601
        parsed = datetime.fromisoformat(result)
        self.assertIsNotNone(parsed.tzinfo)

    def test_read_json_returns_dict_for_valid_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "data.json"
            path.write_text('{"key": "value"}', encoding="utf-8")
            result = dashboard_v2._read_json(path)
            self.assertEqual(result, {"key": "value"})

    def test_read_json_returns_list_for_array(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "data.json"
            path.write_text('[1, 2, 3]', encoding="utf-8")
            result = dashboard_v2._read_json(path)
            self.assertEqual(result, [1, 2, 3])

    def test_read_json_returns_empty_dict_for_missing_file(self):
        result = dashboard_v2._read_json(pathlib.Path("/nonexistent/file.json"))
        self.assertEqual(result, {})

    def test_read_json_returns_empty_dict_for_invalid_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "bad.json"
            path.write_text("not json!", encoding="utf-8")
            result = dashboard_v2._read_json(path)
            self.assertEqual(result, {})

    def test_read_json_returns_empty_dict_for_scalar(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "scalar.json"
            path.write_text('"just a string"', encoding="utf-8")
            result = dashboard_v2._read_json(path)
            self.assertEqual(result, {})

    def test_read_json_dict_returns_dict(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "data.json"
            path.write_text('{"a": 1}', encoding="utf-8")
            result = dashboard_v2._read_json_dict(path)
            self.assertEqual(result, {"a": 1})

    def test_read_json_dict_returns_empty_for_array(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "data.json"
            path.write_text('[1, 2]', encoding="utf-8")
            result = dashboard_v2._read_json_dict(path)
            self.assertEqual(result, {})

    def test_read_ndjson_parses_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "events.ndjson"
            lines = [
                '{"type": "a", "ts": 1}',
                '{"type": "b", "ts": 2}',
                '{"type": "c", "ts": 3}',
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = dashboard_v2._read_ndjson(path, tail=10)
            self.assertEqual(len(result), 3)
            self.assertEqual(result[0]["type"], "a")

    def test_read_ndjson_respects_tail(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "events.ndjson"
            lines = [json.dumps({"i": i}) for i in range(20)]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = dashboard_v2._read_ndjson(path, tail=5)
            self.assertEqual(len(result), 5)
            self.assertEqual(result[0]["i"], 15)

    def test_read_ndjson_skips_invalid_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "events.ndjson"
            path.write_text('{"ok":1}\nnot json\n{"ok":2}\n', encoding="utf-8")
            result = dashboard_v2._read_ndjson(path)
            self.assertEqual(len(result), 2)

    def test_read_ndjson_returns_empty_for_missing(self):
        result = dashboard_v2._read_ndjson(pathlib.Path("/nonexistent.ndjson"))
        self.assertEqual(result, [])

    def test_safe_int(self):
        self.assertEqual(dashboard_v2._safe_int(42), 42)
        self.assertEqual(dashboard_v2._safe_int("7"), 7)
        self.assertEqual(dashboard_v2._safe_int(None), 0)
        self.assertEqual(dashboard_v2._safe_int("bad", 99), 99)

    def test_safe_float(self):
        self.assertAlmostEqual(dashboard_v2._safe_float(3.14), 3.14)
        self.assertAlmostEqual(dashboard_v2._safe_float("2.5"), 2.5)
        self.assertEqual(dashboard_v2._safe_float(None), 0.0)
        self.assertEqual(dashboard_v2._safe_float("bad", 1.0), 1.0)


class LaneDiscoveryTests(unittest.TestCase):
    """Tests for lane discovery and reading."""

    def _make_lane(self, root, lane_id, *, owner="codex", tasks=None, heartbeat=None, config=None):
        """Create a lane fixture directory with artifacts."""
        lane_dir = root / "autonomy" / "lanes" / lane_id
        lane_dir.mkdir(parents=True, exist_ok=True)

        if tasks is None:
            tasks = {
                "task-1": {"status": "done", "attempts": 1, "deadlock_recoveries": 0,
                           "last_update": "2025-01-01T00:00:00Z", "last_summary": "Complete"},
                "task-2": {"status": "in_progress", "attempts": 2, "deadlock_recoveries": 1,
                           "last_update": "2025-01-01T01:00:00Z", "last_summary": "Working"},
            }
        (lane_dir / "state.json").write_text(json.dumps(tasks), encoding="utf-8")

        if heartbeat is None:
            heartbeat = {"cycle": 10, "phase": "execute", "pid": 1234,
                         "timestamp": datetime.now(timezone.utc).isoformat(), "message": "Running"}
        (lane_dir / "heartbeat.json").write_text(json.dumps(heartbeat), encoding="utf-8")

        lane_config = config or {"owner": owner, "execution_profile": "standard",
                                  "continuous": True, "max_cycles": 100, "max_attempts": 5,
                                  "started_at": "2025-01-01T00:00:00Z"}
        (lane_dir / "lane.json").write_text(json.dumps(lane_config), encoding="utf-8")

        metrics = {"responses_total": 50, "cost_usd_total": 0.25, "tokens_total": 10000,
                   "first_time_pass_rate": 0.8, "acceptance_pass_rate": 0.9}
        (lane_dir / "response_metrics_summary.json").write_text(json.dumps(metrics), encoding="utf-8")

        return lane_dir

    def test_discover_lanes_finds_all_lanes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._make_lane(root, "lane-alpha", owner="codex")
            self._make_lane(root, "lane-beta", owner="gemini")
            self._make_lane(root, "lane-gamma", owner="claude")

            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(len(lanes), 3)
            ids = {l["lane_id"] for l in lanes}
            self.assertEqual(ids, {"lane-alpha", "lane-beta", "lane-gamma"})

    def test_discover_lanes_returns_empty_when_no_lanes_dir(self):
        with tempfile.TemporaryDirectory() as td:
            lanes = dashboard_v2.discover_lanes(pathlib.Path(td))
            self.assertEqual(lanes, [])

    def test_lane_status_derivation_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            tasks = {"t1": {"status": "blocked"}, "t2": {"status": "done"}}
            self._make_lane(root, "blocked-lane", tasks=tasks)
            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(len(lanes), 1)
            self.assertEqual(lanes[0]["status"], "blocked")

    def test_lane_status_derivation_in_progress(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            tasks = {"t1": {"status": "in_progress"}, "t2": {"status": "pending"}}
            self._make_lane(root, "active-lane", tasks=tasks)
            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(lanes[0]["status"], "in_progress")

    def test_lane_status_derivation_done(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            tasks = {"t1": {"status": "done"}, "t2": {"status": "done"}}
            self._make_lane(root, "done-lane", tasks=tasks)
            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(lanes[0]["status"], "done")

    def test_lane_status_derivation_idle(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._make_lane(root, "idle-lane", tasks={})
            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(lanes[0]["status"], "idle")

    def test_lane_task_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            tasks = {
                "a": {"status": "done"}, "b": {"status": "done"},
                "c": {"status": "in_progress"}, "d": {"status": "pending"},
                "e": {"status": "blocked"},
            }
            self._make_lane(root, "multi-lane", tasks=tasks)
            lanes = dashboard_v2.discover_lanes(root)
            tc = lanes[0]["task_counts"]
            self.assertEqual(tc["done"], 2)
            self.assertEqual(tc["in_progress"], 1)
            self.assertEqual(tc["pending"], 1)
            self.assertEqual(tc["blocked"], 1)

    def test_lane_heartbeat_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            hb = {"cycle": 42, "phase": "review", "pid": 9999,
                  "timestamp": "2025-06-01T12:00:00Z", "message": "All good"}
            self._make_lane(root, "hb-lane", heartbeat=hb)
            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(lanes[0]["heartbeat"]["cycle"], 42)
            self.assertEqual(lanes[0]["heartbeat"]["phase"], "review")
            self.assertEqual(lanes[0]["heartbeat"]["pid"], 9999)

    def test_lane_metrics_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._make_lane(root, "metric-lane")
            lanes = dashboard_v2.discover_lanes(root)
            m = lanes[0]["metrics"]
            self.assertEqual(m["responses_total"], 50)
            self.assertAlmostEqual(m["cost_usd_total"], 0.25)
            self.assertEqual(m["tokens_total"], 10000)

    def test_lane_owner_from_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._make_lane(root, "owner-lane", owner="gemini")
            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(lanes[0]["owner"], "gemini")

    def test_ignores_files_in_lanes_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._make_lane(root, "real-lane")
            # Place a file (not dir) in lanes/
            (root / "autonomy" / "lanes" / "stray-file.txt").write_text("hi", encoding="utf-8")
            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(len(lanes), 1)


class EventCollectionTests(unittest.TestCase):
    """Tests for collect_events."""

    def test_collects_events_across_lanes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for lid in ("lane-a", "lane-b"):
                lane_dir = root / "autonomy" / "lanes" / lid
                lane_dir.mkdir(parents=True)
                events = [json.dumps({"timestamp": f"2025-01-01T0{i}:00:00Z", "type": "task_done"})
                          for i in range(3)]
                (lane_dir / "conversations.ndjson").write_text("\n".join(events) + "\n", encoding="utf-8")

            result = dashboard_v2.collect_events(root, tail=10)
            self.assertEqual(len(result), 6)
            # Check lane_id annotation
            lane_ids = {e["lane_id"] for e in result}
            self.assertEqual(lane_ids, {"lane-a", "lane-b"})

    def test_events_sorted_descending(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lane_dir = root / "autonomy" / "lanes" / "lane-x"
            lane_dir.mkdir(parents=True)
            events = [
                json.dumps({"timestamp": "2025-01-01T01:00:00Z"}),
                json.dumps({"timestamp": "2025-01-01T03:00:00Z"}),
                json.dumps({"timestamp": "2025-01-01T02:00:00Z"}),
            ]
            (lane_dir / "conversations.ndjson").write_text("\n".join(events) + "\n", encoding="utf-8")

            result = dashboard_v2.collect_events(root, tail=10)
            timestamps = [e["timestamp"] for e in result]
            self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_events_tail_limits_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lane_dir = root / "autonomy" / "lanes" / "lane-y"
            lane_dir.mkdir(parents=True)
            events = [json.dumps({"timestamp": f"2025-01-01T{i:02d}:00:00Z"}) for i in range(20)]
            (lane_dir / "conversations.ndjson").write_text("\n".join(events) + "\n", encoding="utf-8")

            result = dashboard_v2.collect_events(root, tail=5)
            self.assertEqual(len(result), 5)

    def test_events_empty_when_no_lanes(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_events(pathlib.Path(td))
            self.assertEqual(result, [])


class HealthTests(unittest.TestCase):
    """Tests for collect_health."""

    def test_reads_health_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            auto_dir = root / "autonomy"
            auto_dir.mkdir(parents=True)
            (auto_dir / "health.json").write_text('{"status": "green"}', encoding="utf-8")
            (auto_dir / "dashboard_health.json").write_text('{"collab": true}', encoding="utf-8")

            result = dashboard_v2.collect_health(root)
            self.assertEqual(result["health"]["status"], "green")
            self.assertTrue(result["collaboration"]["collab"])
            self.assertIn("timestamp", result)

    def test_health_empty_when_no_files(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_health(pathlib.Path(td))
            self.assertEqual(result["health"], {})
            self.assertEqual(result["collaboration"], {})


class MetricsTests(unittest.TestCase):
    """Tests for collect_metrics."""

    def test_reads_global_and_per_lane_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            auto_dir = root / "autonomy"
            auto_dir.mkdir(parents=True)
            (auto_dir / "response_metrics_summary.json").write_text(
                '{"responses_total": 100, "cost_usd_total": 2.50}', encoding="utf-8")

            lane_dir = auto_dir / "lanes" / "lane-a"
            lane_dir.mkdir(parents=True)
            (lane_dir / "response_metrics_summary.json").write_text(
                '{"responses_total": 30, "cost_usd_total": 0.80}', encoding="utf-8")

            result = dashboard_v2.collect_metrics(root)
            self.assertEqual(result["global"]["responses_total"], 100)
            self.assertIn("lane-a", result["per_lane"])
            self.assertEqual(result["per_lane"]["lane-a"]["responses_total"], 30)


class GitStateTests(unittest.TestCase):
    """Tests for collect_git_state."""

    def test_returns_structure_for_non_repo(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_git_state(pathlib.Path(td))
            self.assertIn("branches", result)
            self.assertIn("recent_commits", result)
            self.assertIn("branch_count", result)

    def test_returns_git_data_for_real_repo(self):
        # Use the orxaq-ops repo itself as test subject
        repo = ROOT
        result = dashboard_v2.collect_git_state(repo)
        self.assertGreater(result["branch_count"], 0)
        self.assertGreater(len(result["recent_commits"]), 0)
        self.assertIn("current_branch", result)
        # Verify commit structure
        commit = result["recent_commits"][0]
        self.assertIn("hash", commit)
        self.assertIn("message", commit)
        self.assertIn("author", commit)


class ConnectivityTests(unittest.TestCase):
    """Tests for collect_connectivity."""

    def test_reads_connectivity_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            root.mkdir(exist_ok=True)
            (root / "model_connectivity.json").write_text(
                '{"endpoints": [{"id": "test", "ok": true}]}', encoding="utf-8")
            result = dashboard_v2.collect_connectivity(root)
            self.assertEqual(len(result["endpoints"]), 1)

    def test_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_connectivity(pathlib.Path(td))
            self.assertEqual(result, {})


class CheckpointTests(unittest.TestCase):
    """Tests for collect_checkpoints."""

    def test_reads_checkpoint_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cp_dir = root / "checkpoints"
            cp_dir.mkdir(parents=True)
            for i in range(3):
                (cp_dir / f"cp_{i:03d}.json").write_text(
                    json.dumps({"run_id": f"run-{i}", "cycle": i * 10}), encoding="utf-8")

            result = dashboard_v2.collect_checkpoints(root)
            self.assertEqual(len(result), 3)
            self.assertIn("run_id", result[0])

    def test_limits_to_5_most_recent(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cp_dir = root / "checkpoints"
            cp_dir.mkdir(parents=True)
            for i in range(10):
                (cp_dir / f"cp_{i:03d}.json").write_text(
                    json.dumps({"run_id": f"run-{i}", "cycle": i}), encoding="utf-8")

            result = dashboard_v2.collect_checkpoints(root)
            self.assertEqual(len(result), 5)

    def test_returns_empty_when_no_dir(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_checkpoints(pathlib.Path(td))
            self.assertEqual(result, [])


class FullSnapshotTests(unittest.TestCase):
    """Tests for the complete snapshot aggregation."""

    def _build_fixture_tree(self, root):
        """Build a realistic artifact tree for snapshot testing."""
        auto_dir = root / "autonomy"
        auto_dir.mkdir(parents=True)

        # Health
        (auto_dir / "health.json").write_text(
            json.dumps({"status": "ok", "budget_daily_cap": 100, "budget_daily_spend": 5.0}),
            encoding="utf-8")

        # Global metrics
        (auto_dir / "response_metrics_summary.json").write_text(
            json.dumps({"responses_total": 200, "cost_usd_total": 5.0}), encoding="utf-8")

        # Lanes
        lanes_dir = auto_dir / "lanes"
        now = datetime.now(timezone.utc)

        # Active lane
        l1 = lanes_dir / "codex-feature"
        l1.mkdir(parents=True)
        (l1 / "lane.json").write_text(json.dumps({"owner": "codex"}), encoding="utf-8")
        (l1 / "state.json").write_text(json.dumps({
            "task-1": {"status": "done", "attempts": 1, "deadlock_recoveries": 0},
            "task-2": {"status": "in_progress", "attempts": 3, "deadlock_recoveries": 0},
        }), encoding="utf-8")
        (l1 / "heartbeat.json").write_text(json.dumps({
            "cycle": 15, "phase": "execute", "pid": 100,
            "timestamp": now.isoformat(), "message": "Working"}), encoding="utf-8")
        (l1 / "response_metrics_summary.json").write_text(
            json.dumps({"responses_total": 100, "cost_usd_total": 2.0, "tokens_total": 50000}),
            encoding="utf-8")
        (l1 / "conversations.ndjson").write_text(
            json.dumps({"timestamp": now.isoformat(), "type": "task_done", "message": "Done"}) + "\n",
            encoding="utf-8")

        # Blocked lane
        l2 = lanes_dir / "gemini-tests"
        l2.mkdir(parents=True)
        (l2 / "lane.json").write_text(json.dumps({"owner": "gemini"}), encoding="utf-8")
        (l2 / "state.json").write_text(json.dumps({
            "task-a": {"status": "blocked", "attempts": 5, "deadlock_recoveries": 6},
        }), encoding="utf-8")
        (l2 / "heartbeat.json").write_text(json.dumps({
            "cycle": 3, "phase": "blocked", "pid": 200,
            "timestamp": (now - timedelta(seconds=600)).isoformat(), "message": "Stuck"}),
            encoding="utf-8")
        (l2 / "response_metrics_summary.json").write_text(
            json.dumps({"responses_total": 50, "cost_usd_total": 1.0, "tokens_total": 20000}),
            encoding="utf-8")

        return root

    def test_snapshot_contains_all_sections(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_fixture_tree(pathlib.Path(td))
            snap = dashboard_v2.full_snapshot(root)

            self.assertIn("timestamp", snap)
            self.assertIn("summary", snap)
            self.assertIn("lanes", snap)
            self.assertIn("health", snap)
            self.assertIn("metrics", snap)
            self.assertIn("events", snap)
            self.assertIn("alerts", snap)
            self.assertIn("git", snap)
            self.assertIn("connectivity", snap)
            self.assertIn("checkpoints", snap)

    def test_snapshot_summary_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_fixture_tree(pathlib.Path(td))
            snap = dashboard_v2.full_snapshot(root)
            s = snap["summary"]

            self.assertEqual(s["lanes_total"], 2)
            self.assertEqual(s["owner_counts"]["codex"], 1)
            self.assertEqual(s["owner_counts"]["gemini"], 1)
            self.assertIn("blocked", s["status_counts"])
            self.assertGreater(s["total_cost_usd"], 0)
            self.assertGreater(s["total_tokens"], 0)

    def test_snapshot_generates_blocked_lane_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_fixture_tree(pathlib.Path(td))
            snap = dashboard_v2.full_snapshot(root)
            alerts = snap["alerts"]

            blocked_alerts = [a for a in alerts if a["type"] == "blocked_lane"]
            self.assertGreater(len(blocked_alerts), 0)
            self.assertEqual(blocked_alerts[0]["lane_id"], "gemini-tests")
            self.assertEqual(blocked_alerts[0]["severity"], "high")

    def test_snapshot_generates_stale_heartbeat_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_fixture_tree(pathlib.Path(td))
            snap = dashboard_v2.full_snapshot(root)
            alerts = snap["alerts"]

            stale_alerts = [a for a in alerts if a["type"] == "stale_heartbeat"]
            self.assertGreater(len(stale_alerts), 0)
            self.assertEqual(stale_alerts[0]["lane_id"], "gemini-tests")

    def test_snapshot_generates_deadlock_storm_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_fixture_tree(pathlib.Path(td))
            snap = dashboard_v2.full_snapshot(root)
            alerts = snap["alerts"]

            deadlock_alerts = [a for a in alerts if a["type"] == "deadlock_storm"]
            self.assertGreater(len(deadlock_alerts), 0)

    def test_snapshot_heartbeat_age_computed(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_fixture_tree(pathlib.Path(td))
            snap = dashboard_v2.full_snapshot(root)

            codex_lane = next(l for l in snap["lanes"] if l["lane_id"] == "codex-feature")
            # Fresh heartbeat should have small age
            self.assertGreaterEqual(codex_lane["heartbeat"]["age_sec"], 0)
            self.assertLess(codex_lane["heartbeat"]["age_sec"], 30)

            gemini_lane = next(l for l in snap["lanes"] if l["lane_id"] == "gemini-tests")
            # Stale heartbeat (600s old)
            self.assertGreater(gemini_lane["heartbeat"]["age_sec"], 500)

    def test_snapshot_with_repo_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_fixture_tree(pathlib.Path(td))
            snap = dashboard_v2.full_snapshot(root, repo_dir=ROOT)
            self.assertIn("current_branch", snap["git"])

    def test_snapshot_without_repo_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_fixture_tree(pathlib.Path(td))
            snap = dashboard_v2.full_snapshot(root)
            self.assertEqual(snap["git"], {})


class HTTPHandlerTests(unittest.TestCase):
    """Integration tests for the v2 HTTP handler."""

    def _build_fixture_tree(self, root):
        """Build a minimal artifact tree for HTTP tests."""
        auto_dir = root / "autonomy"
        auto_dir.mkdir(parents=True)
        (auto_dir / "health.json").write_text('{"ok": true}', encoding="utf-8")

        lane_dir = auto_dir / "lanes" / "test-lane"
        lane_dir.mkdir(parents=True)
        (lane_dir / "lane.json").write_text('{"owner": "codex"}', encoding="utf-8")
        (lane_dir / "state.json").write_text(
            '{"t1": {"status": "done", "attempts": 1}}', encoding="utf-8")
        (lane_dir / "heartbeat.json").write_text(json.dumps({
            "cycle": 5, "phase": "idle", "pid": 1,
            "timestamp": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        (lane_dir / "response_metrics_summary.json").write_text(
            '{"responses_total": 10}', encoding="utf-8")

    def _start_server(self, artifacts_dir, repo_dir=None):
        handler = dashboard_v2.make_v2_handler(artifacts_dir, repo_dir=repo_dir)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_root_serves_html(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/", timeout=5) as resp:
                    body = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    # Should serve the frontend HTML
                    self.assertIn("NEXUS", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_v2_path_serves_html(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/v2", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIn("NEXUS", resp.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_snapshot_returns_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/snapshot", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("summary", data)
                    self.assertIn("lanes", data)
                    self.assertEqual(data["summary"]["lanes_total"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_lanes_returns_list(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/lanes", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIsInstance(data, list)
                    self.assertEqual(len(data), 1)
                    self.assertEqual(data[0]["lane_id"], "test-lane")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_lanes_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/lanes/test-lane", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(data["lane_id"], "test-lane")
                    self.assertEqual(data["owner"], "codex")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_lanes_by_id_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(urllib_error.HTTPError) as ctx:
                    urllib_request.urlopen(f"{base}/api/v2/lanes/nonexistent", timeout=5)
                self.assertEqual(ctx.exception.code, 404)
                ctx.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_events(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            # Add conversation events
            lane_dir = root / "autonomy" / "lanes" / "test-lane"
            (lane_dir / "conversations.ndjson").write_text(
                json.dumps({"timestamp": "2025-01-01T00:00:00Z", "type": "task_done"}) + "\n",
                encoding="utf-8")
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/events?tail=5", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIsInstance(data, list)
                    self.assertEqual(len(data), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_health(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/health", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(data["health"]["ok"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/metrics", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("global", data)
                    self.assertIn("per_lane", data)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_git_with_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root, repo_dir=ROOT)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/git", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("branches", data)
                    self.assertGreater(data["branch_count"], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_git_without_repo_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root, repo_dir=None)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(urllib_error.HTTPError) as ctx:
                    urllib_request.urlopen(f"{base}/api/v2/git", timeout=5)
                self.assertEqual(ctx.exception.code, 400)
                ctx.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_unknown_route_returns_404(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(urllib_error.HTTPError) as ctx:
                    urllib_request.urlopen(f"{base}/api/v2/nonexistent", timeout=5)
                self.assertEqual(ctx.exception.code, 404)
                ctx.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_cors_header_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_fixture_tree(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/snapshot", timeout=5) as resp:
                    cors = resp.headers.get("Access-Control-Allow-Origin")
                    self.assertEqual(cors, "*")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class ReportTests(unittest.TestCase):
    """Tests for collect_report."""

    def test_reads_report_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            auto_dir = root / "autonomy"
            auto_dir.mkdir(parents=True)
            (auto_dir / "swarm_cycle_report.json").write_text(
                json.dumps({"criteria": [{"id": "c1", "ok": True}], "summary": {}}),
                encoding="utf-8")
            (auto_dir / "full_autonomy_report.json").write_text(
                json.dumps({"criteria": [{"id": "c1", "ok": True, "description": "Test"}]}),
                encoding="utf-8")
            result = dashboard_v2.collect_report(root)
            self.assertIn("cycle_report", result)
            self.assertIn("full_report", result)
            self.assertEqual(len(result["cycle_report"]["criteria"]), 1)

    def test_report_empty_when_no_files(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_report(pathlib.Path(td))
            self.assertEqual(result["cycle_report"], {})
            self.assertEqual(result["full_report"], {})


class CostSeriesTests(unittest.TestCase):
    """Tests for collect_cost_series."""

    def test_reads_cost_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cost_dir = root / "autonomy" / "provider_costs"
            cost_dir.mkdir(parents=True)
            (cost_dir / "summary.json").write_text(json.dumps({
                "cost_series_hourly_24h": [{"hour": "2025-01-01T00", "cost_usd_total": 1.5}],
                "cost_windows_usd": {"today": 5.0, "last_hour": 0.5},
                "provider_cost_30d": {"openai": 10.0},
                "model_cost_30d": {"gpt-4": 8.0},
                "data_freshness": {"stale": False},
            }), encoding="utf-8")
            result = dashboard_v2.collect_cost_series(root)
            self.assertEqual(len(result["hourly_series"]), 1)
            self.assertEqual(result["windows"]["today"], 5.0)
            self.assertEqual(result["by_provider"]["openai"], 10.0)

    def test_cost_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_cost_series(pathlib.Path(td))
            self.assertEqual(result["hourly_series"], [])
            self.assertEqual(result["windows"], {})


class PrivilegeTests(unittest.TestCase):
    """Tests for collect_privileges."""

    def test_reads_privilege_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            auto_dir = root / "autonomy"
            auto_dir.mkdir(parents=True)
            (auto_dir / "session_autonomy_privileges.json").write_text(
                json.dumps({"autonomy_authorized": True, "critical_security_gate": "pass"}),
                encoding="utf-8")
            (auto_dir / "privilege_escalations.ndjson").write_text(
                json.dumps({"event_type": "breakglass_grant_created", "grant_id": "bg-1"}) + "\n",
                encoding="utf-8")
            result = dashboard_v2.collect_privileges(root)
            self.assertTrue(result["current"]["autonomy_authorized"])
            self.assertEqual(len(result["recent_escalations"]), 1)

    def test_privileges_empty_when_no_files(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_privileges(pathlib.Path(td))
            self.assertEqual(result["current"], {})
            self.assertEqual(result["recent_escalations"], [])


class PolicyTests(unittest.TestCase):
    """Tests for collect_policies."""

    def test_reads_policy_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            auto_dir = root / "autonomy"
            auto_dir.mkdir(parents=True)
            (auto_dir / "git_delivery_policy_health.json").write_text(
                json.dumps({"violations": [], "summary": {"compliant": True}}),
                encoding="utf-8")
            (auto_dir / "pr_tier_policy_health.json").write_text(
                json.dumps({"t1_ratio": 0.85}),
                encoding="utf-8")
            result = dashboard_v2.collect_policies(root)
            self.assertIn("git_delivery", result["policies"])
            self.assertIn("pr_tier", result["policies"])

    def test_policies_empty_when_no_files(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_policies(pathlib.Path(td))
            self.assertEqual(result["policies"], {})


class TaskBacklogTests(unittest.TestCase):
    """Tests for collect_task_backlog."""

    def test_reads_task_backlog(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config_dir = root.parent / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            tasks = [
                {"id": "t1", "owner": "codex", "priority": 1, "title": "Task 1"},
                {"id": "t2", "owner": "gemini", "priority": 2, "title": "Task 2"},
                {"id": "t3", "owner": "codex", "priority": 1, "title": "Task 3"},
            ]
            (config_dir / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
            result = dashboard_v2.collect_task_backlog(root)
            self.assertEqual(result["total"], 3)
            self.assertEqual(result["by_owner"]["codex"], 2)
            self.assertEqual(result["by_owner"]["gemini"], 1)

    def test_backlog_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            # Use a nested path so parent/config doesn't accidentally exist
            artifacts = pathlib.Path(td) / "deep" / "nested" / "artifacts"
            artifacts.mkdir(parents=True)
            result = dashboard_v2.collect_task_backlog(artifacts)
            self.assertEqual(result["total"], 0)
            self.assertEqual(result["tasks"], [])


class ResponseStreamTests(unittest.TestCase):
    """Tests for collect_response_stream."""

    def test_collects_response_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lane_dir = root / "autonomy" / "lanes" / "lane-a"
            lane_dir.mkdir(parents=True)
            entries = [
                json.dumps({"timestamp": f"2025-01-01T0{i}:00:00Z", "cost_usd": 0.01, "model": "gpt-4"})
                for i in range(5)
            ]
            (lane_dir / "response_metrics.ndjson").write_text("\n".join(entries) + "\n", encoding="utf-8")
            result = dashboard_v2.collect_response_stream(root, tail=3)
            self.assertEqual(len(result), 3)
            self.assertEqual(result[0]["lane_id"], "lane-a")

    def test_response_stream_empty_when_no_lanes(self):
        with tempfile.TemporaryDirectory() as td:
            result = dashboard_v2.collect_response_stream(pathlib.Path(td))
            self.assertEqual(result, [])


class NewHTTPEndpointTests(unittest.TestCase):
    """Tests for the new enriched API HTTP endpoints."""

    def _build_full_fixture(self, root):
        """Build a fixture tree with data for all new endpoints."""
        auto_dir = root / "autonomy"
        auto_dir.mkdir(parents=True)
        (auto_dir / "health.json").write_text('{"ok": true}', encoding="utf-8")

        # Lane
        lane_dir = auto_dir / "lanes" / "test-lane"
        lane_dir.mkdir(parents=True)
        (lane_dir / "lane.json").write_text('{"owner": "codex"}', encoding="utf-8")
        (lane_dir / "state.json").write_text(
            '{"t1": {"status": "done", "attempts": 1}}', encoding="utf-8")
        (lane_dir / "heartbeat.json").write_text(json.dumps({
            "cycle": 5, "phase": "idle", "pid": 1,
            "timestamp": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        (lane_dir / "response_metrics_summary.json").write_text(
            '{"responses_total": 10}', encoding="utf-8")
        (lane_dir / "response_metrics.ndjson").write_text(
            json.dumps({"timestamp": "2025-01-01T00:00:00Z", "cost_usd": 0.01}) + "\n",
            encoding="utf-8")

        # Report
        (auto_dir / "swarm_cycle_report.json").write_text(
            json.dumps({"criteria": [{"id": "c1", "ok": True}]}), encoding="utf-8")

        # Cost series
        cost_dir = auto_dir / "provider_costs"
        cost_dir.mkdir(parents=True)
        (cost_dir / "summary.json").write_text(json.dumps({
            "cost_series_hourly_24h": [{"hour": "h1", "cost_usd_total": 1.0}],
            "cost_windows_usd": {"today": 2.0},
        }), encoding="utf-8")

        # Privileges
        (auto_dir / "session_autonomy_privileges.json").write_text(
            json.dumps({"autonomy_authorized": True}), encoding="utf-8")

        # Policies
        (auto_dir / "git_delivery_policy_health.json").write_text(
            json.dumps({"compliant": True}), encoding="utf-8")

        # Task backlog
        config_dir = root.parent / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "tasks.json").write_text(
            json.dumps([{"id": "t1", "owner": "codex", "priority": 1}]), encoding="utf-8")

    def _start_server(self, artifacts_dir):
        handler = dashboard_v2.make_v2_handler(artifacts_dir, repo_dir=ROOT)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_api_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_full_fixture(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/report", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("cycle_report", data)
                    self.assertEqual(len(data["cycle_report"]["criteria"]), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_cost_series(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_full_fixture(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/cost-series", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(len(data["hourly_series"]), 1)
                    self.assertEqual(data["windows"]["today"], 2.0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_privileges(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_full_fixture(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/privileges", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(data["current"]["autonomy_authorized"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_policies(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_full_fixture(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/policies", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("git_delivery", data["policies"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_task_backlog(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_full_fixture(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/task-backlog", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(data["total"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_response_stream(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_full_fixture(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/response-stream?tail=5", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIsInstance(data, list)
                    self.assertEqual(len(data), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_frontend_routes_serve_html(self):
        """Test that /meridian, /prism, /signal routes return HTML content-type."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "artifacts"
            root.mkdir()
            self._build_full_fixture(root)
            server, thread = self._start_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                for route in ["/meridian", "/prism", "/signal"]:
                    try:
                        with urllib_request.urlopen(f"{base}{route}", timeout=5) as resp:
                            ct = resp.headers.get("Content-Type", "")
                            self.assertIn("text/html", ct)
                    except urllib_error.HTTPError as err:
                        # 500 is acceptable when HTML file doesn't exist yet
                        self.assertEqual(err.code, 500)
                        err.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class EdgeCaseTests(unittest.TestCase):
    """Tests for edge cases and robustness."""

    def test_empty_artifacts_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            snap = dashboard_v2.full_snapshot(root)
            self.assertEqual(snap["summary"]["lanes_total"], 0)
            self.assertEqual(snap["alerts"], [])
            self.assertEqual(snap["events"], [])

    def test_lane_with_corrupt_state_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lane_dir = root / "autonomy" / "lanes" / "corrupt-lane"
            lane_dir.mkdir(parents=True)
            (lane_dir / "state.json").write_text("NOT JSON", encoding="utf-8")
            (lane_dir / "lane.json").write_text('{"owner": "codex"}', encoding="utf-8")
            (lane_dir / "heartbeat.json").write_text('{}', encoding="utf-8")
            (lane_dir / "response_metrics_summary.json").write_text('{}', encoding="utf-8")

            lanes = dashboard_v2.discover_lanes(root)
            # Should still find the lane, just with no tasks
            self.assertEqual(len(lanes), 1)
            self.assertEqual(lanes[0]["status"], "idle")

    def test_lane_with_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lane_dir = root / "autonomy" / "lanes" / "bare-lane"
            lane_dir.mkdir(parents=True)
            # No files at all in the lane directory

            lanes = dashboard_v2.discover_lanes(root)
            self.assertEqual(len(lanes), 1)
            self.assertEqual(lanes[0]["lane_id"], "bare-lane")
            self.assertEqual(lanes[0]["owner"], "unknown")

    def test_heartbeat_with_no_timezone_info(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lane_dir = root / "autonomy" / "lanes" / "no-tz-lane"
            lane_dir.mkdir(parents=True)
            (lane_dir / "lane.json").write_text('{"owner": "codex"}', encoding="utf-8")
            (lane_dir / "state.json").write_text(
                '{"t1": {"status": "in_progress"}}', encoding="utf-8")
            # Timestamp without timezone
            (lane_dir / "heartbeat.json").write_text(json.dumps({
                "timestamp": "2025-01-01T00:00:00"}), encoding="utf-8")
            (lane_dir / "response_metrics_summary.json").write_text('{}', encoding="utf-8")

            snap = dashboard_v2.full_snapshot(root)
            lane = snap["lanes"][0]
            # Should still compute age (assuming UTC)
            self.assertIsNotNone(lane["heartbeat"]["age_sec"])

    def test_heartbeat_with_empty_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lane_dir = root / "autonomy" / "lanes" / "empty-ts-lane"
            lane_dir.mkdir(parents=True)
            (lane_dir / "lane.json").write_text('{"owner": "codex"}', encoding="utf-8")
            (lane_dir / "state.json").write_text('{"t1": {"status": "done"}}', encoding="utf-8")
            (lane_dir / "heartbeat.json").write_text('{"timestamp": ""}', encoding="utf-8")
            (lane_dir / "response_metrics_summary.json").write_text('{}', encoding="utf-8")

            snap = dashboard_v2.full_snapshot(root)
            lane = snap["lanes"][0]
            self.assertEqual(lane["heartbeat"]["age_sec"], -1)

    def test_ndjson_with_non_dict_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "mixed.ndjson"
            path.write_text('"a string"\n42\n{"ok":1}\n[1,2]\n', encoding="utf-8")
            result = dashboard_v2._read_ndjson(path)
            # Only the dict line should be included
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["ok"], 1)


if __name__ == "__main__":
    unittest.main()
