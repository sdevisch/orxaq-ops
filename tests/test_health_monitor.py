import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.health_monitor import (
    CollaborationHealthMonitor,
    DegradationSignal,
    HealthGrade,
    HealthStatus,
    compute_health_score,
    create_remediations,
    dashboard_health_status,
    detect_budget_issues,
    detect_heartbeat_staleness,
    detect_idle_behavior,
    detect_lane_stoppages,
    detect_validation_bottlenecks,
    grade_from_score,
)


class TestDetectLaneStoppages(unittest.TestCase):
    def test_no_blocked_tasks(self):
        state = {"t1": {"status": "done"}, "t2": {"status": "pending"}}
        self.assertEqual(detect_lane_stoppages(state), [])

    def test_detects_blocked_tasks(self):
        state = {
            "t1": {"status": "blocked"},
            "t2": {"status": "done"},
            "t3": {"status": "blocked"},
        }
        diagnoses = detect_lane_stoppages(state)
        self.assertEqual(len(diagnoses), 1)
        self.assertEqual(diagnoses[0].signal, DegradationSignal.LANE_STOPPAGE)
        self.assertIn("t1", diagnoses[0].evidence["blocked_tasks"])
        self.assertIn("t3", diagnoses[0].evidence["blocked_tasks"])

    def test_severity_high_when_many_blocked(self):
        state = {f"t{i}": {"status": "blocked"} for i in range(5)}
        diagnoses = detect_lane_stoppages(state)
        self.assertEqual(diagnoses[0].severity, "high")

    def test_severity_medium_when_few_blocked(self):
        state = {"t1": {"status": "blocked"}}
        diagnoses = detect_lane_stoppages(state)
        self.assertEqual(diagnoses[0].severity, "medium")

    def test_skips_non_dict_entries(self):
        state = {"t1": "not-a-dict", "t2": {"status": "blocked"}}
        diagnoses = detect_lane_stoppages(state)
        self.assertEqual(len(diagnoses), 1)


class TestDetectIdleBehavior(unittest.TestCase):
    def test_no_idle_tasks(self):
        now = datetime.now(timezone.utc)
        state = {
            "t1": {"status": "in_progress", "last_update": now.isoformat()},
        }
        self.assertEqual(detect_idle_behavior(state, idle_threshold_sec=3600), [])

    def test_detects_idle_tasks(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        state = {
            "t1": {"status": "in_progress", "last_update": old},
        }
        diagnoses = detect_idle_behavior(state, idle_threshold_sec=1800)
        self.assertEqual(len(diagnoses), 1)
        self.assertEqual(diagnoses[0].signal, DegradationSignal.IDLE_BEHAVIOR)

    def test_skips_done_tasks(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        state = {"t1": {"status": "done", "last_update": old}}
        self.assertEqual(detect_idle_behavior(state, idle_threshold_sec=1800), [])

    def test_idle_when_no_last_update(self):
        state = {"t1": {"status": "in_progress"}}
        diagnoses = detect_idle_behavior(state, idle_threshold_sec=1800)
        self.assertEqual(len(diagnoses), 1)


class TestDetectValidationBottlenecks(unittest.TestCase):
    def test_no_bottlenecks(self):
        state = {"t1": {"status": "in_progress", "attempts": 1}}
        self.assertEqual(detect_validation_bottlenecks(state), [])

    def test_detects_high_attempt_tasks(self):
        state = {"t1": {"status": "in_progress", "attempts": 6}}
        diagnoses = detect_validation_bottlenecks(state, high_attempt_threshold=4)
        self.assertEqual(len(diagnoses), 1)
        self.assertEqual(diagnoses[0].signal, DegradationSignal.VALIDATION_BOTTLENECK)


class TestDetectHeartbeatStaleness(unittest.TestCase):
    def test_no_heartbeat(self):
        diagnoses = detect_heartbeat_staleness(-1)
        self.assertEqual(len(diagnoses), 1)
        self.assertEqual(diagnoses[0].signal, DegradationSignal.HEARTBEAT_STALE)

    def test_stale_heartbeat(self):
        diagnoses = detect_heartbeat_staleness(600, stale_threshold_sec=300)
        self.assertEqual(len(diagnoses), 1)

    def test_fresh_heartbeat(self):
        diagnoses = detect_heartbeat_staleness(10, stale_threshold_sec=300)
        self.assertEqual(len(diagnoses), 0)


class TestDetectBudgetIssues(unittest.TestCase):
    def test_no_budget(self):
        self.assertEqual(detect_budget_issues({}), [])

    def test_budget_exceeded(self):
        budget = {"total_cost_usd": 20.0, "limit_usd": 15.0}
        diagnoses = detect_budget_issues(budget)
        self.assertEqual(len(diagnoses), 1)
        self.assertEqual(diagnoses[0].severity, "critical")

    def test_budget_nearly_exceeded(self):
        budget = {"total_cost_usd": 14.0, "limit_usd": 15.0}
        diagnoses = detect_budget_issues(budget)
        self.assertEqual(len(diagnoses), 1)
        self.assertEqual(diagnoses[0].severity, "high")

    def test_budget_ok(self):
        budget = {"total_cost_usd": 5.0, "limit_usd": 15.0}
        self.assertEqual(detect_budget_issues(budget), [])

    def test_handles_non_dict(self):
        self.assertEqual(detect_budget_issues("not-a-dict"), [])


class TestHealthScoring(unittest.TestCase):
    def test_no_diagnoses_score_100(self):
        self.assertEqual(compute_health_score([]), 100)

    def test_critical_diagnosis_large_penalty(self):
        from orxaq_autonomy.health_monitor import Diagnosis

        diag = Diagnosis(signal="test", severity="critical", root_cause="test")
        score = compute_health_score([diag])
        self.assertLessEqual(score, 60)

    def test_grade_healthy(self):
        self.assertEqual(grade_from_score(90), HealthGrade.HEALTHY)

    def test_grade_degraded(self):
        self.assertEqual(grade_from_score(65), HealthGrade.DEGRADED)

    def test_grade_critical(self):
        self.assertEqual(grade_from_score(30), HealthGrade.CRITICAL)


class TestCreateRemediations(unittest.TestCase):
    def test_creates_remediation_for_high_severity(self):
        from orxaq_autonomy.health_monitor import Diagnosis

        diag = Diagnosis(
            signal=DegradationSignal.LANE_STOPPAGE,
            severity="high",
            root_cause="3 tasks blocked",
            suggested_remediation="Unblock tasks",
        )
        tasks = create_remediations([diag])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].target_lane, "L1")
        self.assertEqual(tasks[0].description, "Unblock tasks")

    def test_no_remediation_for_low_severity(self):
        from orxaq_autonomy.health_monitor import Diagnosis

        diag = Diagnosis(signal="test", severity="low", root_cause="minor")
        tasks = create_remediations([diag])
        self.assertEqual(len(tasks), 0)


class TestCollaborationHealthMonitor(unittest.TestCase):
    def test_check_returns_healthy_status(self):
        monitor = CollaborationHealthMonitor()
        status = monitor.check(state={"t1": {"status": "done"}}, heartbeat_age_sec=10)
        self.assertEqual(status.grade, HealthGrade.HEALTHY)
        self.assertEqual(status.score, 100)

    def test_check_detects_multiple_issues(self):
        monitor = CollaborationHealthMonitor()
        state = {
            "t1": {"status": "blocked"},
            "t2": {"status": "blocked"},
            "t3": {"status": "blocked"},
            "t4": {"status": "in_progress", "attempts": 8},
        }
        status = monitor.check(state=state, heartbeat_age_sec=999)
        self.assertIn(status.grade, (HealthGrade.DEGRADED, HealthGrade.CRITICAL))
        self.assertLess(status.score, 80)
        self.assertGreater(len(status.diagnoses), 0)
        self.assertGreater(len(status.remediations), 0)

    def test_check_writes_output_file(self):
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td) / "health_output"
            monitor = CollaborationHealthMonitor(output_dir=out)
            monitor.check(state={"t1": {"status": "done"}}, heartbeat_age_sec=10)
            health_file = out / "collaboration_health.json"
            self.assertTrue(health_file.exists())
            data = json.loads(health_file.read_text(encoding="utf-8"))
            self.assertIn("grade", data)
            self.assertIn("score", data)

    def test_last_status_tracks_latest(self):
        monitor = CollaborationHealthMonitor()
        self.assertIsNone(monitor.last_status)
        monitor.check(state={}, heartbeat_age_sec=10)
        self.assertIsNotNone(monitor.last_status)


class TestDashboardHealthStatus(unittest.TestCase):
    def test_returns_valid_payload(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = pathlib.Path(td) / "state.json"
            state_file.write_text(
                json.dumps({"t1": {"status": "done"}, "t2": {"status": "pending"}}),
                encoding="utf-8",
            )
            result = dashboard_health_status(state_file=state_file, heartbeat_age_sec=10)
            self.assertIn("grade", result)
            self.assertIn("score", result)
            self.assertEqual(result["grade"], HealthGrade.HEALTHY)

    def test_handles_missing_state_file(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = pathlib.Path(td) / "missing.json"
            result = dashboard_health_status(state_file=state_file, heartbeat_age_sec=10)
            self.assertIn("grade", result)

    def test_handles_malformed_state_file(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = pathlib.Path(td) / "state.json"
            state_file.write_text("not json!", encoding="utf-8")
            result = dashboard_health_status(state_file=state_file, heartbeat_age_sec=10)
            self.assertIn("grade", result)


if __name__ == "__main__":
    unittest.main()
