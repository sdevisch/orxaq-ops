import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "swarm_cycle_report.py"

module_spec = importlib.util.spec_from_file_location("swarm_cycle_report", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
swarm_cycle_report = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("swarm_cycle_report", swarm_cycle_report)
module_spec.loader.exec_module(swarm_cycle_report)


class _Response:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class SwarmCycleReportTests(unittest.TestCase):
    def _write_json(self, path: pathlib.Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _base_root(self, td: str) -> pathlib.Path:
        root = pathlib.Path(td) / "orxaq-ops"
        product_root = pathlib.Path(td) / "orxaq"

        # health snapshots
        strict_health = {
            "pass_gate": True,
            "score": 90,
            "threshold": 85,
            "checks": {
                "quality_gates": [
                    {"name": "unit_tests", "ok": True},
                    {"name": "lint", "ok": True},
                    {"name": "typecheck", "ok": True},
                    {"name": "security_scan", "ok": True},
                    {"name": "security_audit", "ok": True},
                ]
            },
        }
        operational_health = {"pass_gate": True, "score": 88}
        self._write_json(root / "artifacts" / "autonomy" / "health_snapshot" / "strict.json", strict_health)
        self._write_json(root / "artifacts" / "autonomy" / "health_snapshot" / "operational.json", operational_health)

        # connectivity
        self._write_json(
            root / "artifacts" / "model_connectivity.json",
            {
                "endpoint_unhealthy": 0,
                "endpoint_required_total": 1,
                "optional_endpoint_unhealthy": 0,
            },
        )

        # health policies
        self._write_json(
            root / "artifacts" / "autonomy" / "swarm_todo_health" / "current_latest.json",
            {
                "warnings": [],
                "distributed_todo": {
                    "stale_file_count": 0,
                    "unassigned_active_task_total": 0,
                },
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "t1_basic_model_policy.json",
            {
                "ok": True,
                "summary": {"violation_count": 0, "basic_tasks": 0, "scanned_metrics": 1},
                "observability": {"ok": True, "latest_metric_age_minutes": 1, "parse_skip_ratio": 0.0},
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "privilege_policy_health.json",
            {
                "ok": True,
                "summary": {
                    "violation_count": 0,
                    "latest_event_age_minutes": 1,
                    "breakglass_events": 0,
                },
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "git_delivery_policy_health.json",
            {
                "ok": True,
                "summary": {
                    "violation_count": 0,
                    "branch": "codex/issue-123-small-fix",
                    "ticket_branch_match": True,
                    "effective_changed_lines": 180,
                    "max_changed_lines": 400,
                    "pr_found": True,
                    "pr_approvals": 1,
                },
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "git_hygiene_health.json",
            {
                "ok": True,
                "summary": {
                    "violation_count": 0,
                    "local_branch_count": 24,
                    "remote_branch_count": 60,
                    "total_branch_count": 84,
                    "stale_local_branch_count": 8,
                    "max_total_branches": 140,
                },
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "git_hygiene_remediation.json",
            {
                "ok": True,
                "summary": {
                    "error_count": 0,
                    "remote_stale_prefix_count": 0,
                    "local_stale_prefix_count": 0,
                    "remote_candidate_count": 0,
                    "local_candidate_count": 0,
                    "remote_blocked_open_pr_count": 0,
                    "remote_blocked_unmerged_count": 0,
                    "local_blocked_unmerged_count": 0,
                    "local_blocked_worktree_count": 0,
                    "worktree_prune_removed_count": 0,
                    "worktree_remove_attempted_count": 0,
                    "worktree_removed_count": 0,
                    "worktree_remove_failed_count": 0,
                    "remote_deleted_count": 0,
                    "local_deleted_count": 0,
                },
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "backend_upgrade_policy_health.json",
            {
                "ok": True,
                "summary": {
                    "violation_count": 0,
                    "release_phase": "foundation",
                    "dependency_checks_passed": 3,
                    "activation_task_checks_passed": 3,
                },
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "api_interop_policy_health.json",
            {
                "ok": True,
                "summary": {
                    "violation_count": 0,
                    "release_phase": "foundation",
                    "dependency_checks_passed": 6,
                    "activation_prereq_checks_passed": 4,
                },
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "deterministic_backlog_health.json",
            {
                "ok": True,
                "summary": {
                    "ready_after": 16,
                    "ready_min": 10,
                    "ready_target": 16,
                    "ready_max": 24,
                    "done_after": 5,
                    "action_count": 3,
                },
                "backlog_updated": True,
            },
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "pr_approval_remediation.json",
            {
                "ok": True,
                "summary": {
                    "open_prs_seen": 6,
                    "approved_count": 2,
                    "self_blocked_count": 1,
                    "other_blocked_count": 0,
                },
            },
        )
        self._write_json(root / "artifacts" / "autonomy" / "ready_queue_week.json", {"summary": {"task_count": 5}})
        self._write_json(root / "artifacts" / "autonomy" / "provider_costs" / "summary.json", {"ok": True})
        self._write_json(
            root / "artifacts" / "autonomy" / "pr_tier_policy_health.json",
            {
                "ok": True,
                "summary": {
                    "violation_count": 0,
                    "reviewed_prs": 14,
                    "ratio_base_prs": 14,
                    "t1_count": 11,
                    "escalated_count": 3,
                    "unlabeled_count": 0,
                    "conflict_count": 0,
                    "t1_ratio": 0.7857,
                    "escalated_ratio": 0.2143,
                    "unlabeled_ratio": 0.0,
                },
                "policy": {
                    "min_t1_ratio": 0.7,
                },
            },
        )
        self._write_json(root / "config" / "routellm_policy.local-workhorse.json", {"routing": {"local_first": True, "local_saturation_policy": "saturate_before_hosted"}})
        self._write_json(
            root / "config" / "lanes.json",
            {
                "lanes": [
                    {"id": "lane-codex", "owner": "codex", "enabled": True},
                ]
            },
        )
        self._write_json(root / "artifacts" / "autonomy" / "heartbeat.json", {"phase": "completed"})

        (root / "artifacts" / "autonomy" / "conversations.ndjson").parent.mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "autonomy" / "conversations.ndjson").write_text('{"evt":"ok"}\n', encoding="utf-8")
        (root / "artifacts" / "autonomy" / "event_mesh").mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson").write_text('{"evt":"ok"}\n', encoding="utf-8")

        # Dashboard metadata can be stale; PID check will still pass from current process id.
        self._write_json(root / "artifacts" / "autonomy" / "dashboard.json", {"running": False, "host": "127.0.0.1", "port": 8765})

        pid_text = f"{os.getpid()}\n"
        for rel in [
            "artifacts/autonomy/supervisor.pid",
            "artifacts/autonomy/runner.pid",
            "artifacts/autonomy/dashboard.pid",
            "artifacts/autonomy/swarm_todo_health/current_health.pid",
        ]:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pid_text, encoding="utf-8")

        # Ensure product root exists for fallback path assumptions.
        (product_root / "artifacts").mkdir(parents=True, exist_ok=True)
        return root

    def test_dashboard_http_live_accepts_healthy_response(self):
        with mock.patch("swarm_cycle_report.urlopen", return_value=_Response(200)):
            ok, base = swarm_cycle_report._dashboard_http_live({"url": "http://127.0.0.1:8876"})
        self.assertTrue(ok)
        self.assertTrue(base.startswith("http://127.0.0.1"))

    def test_build_report_includes_backend_upgrade_policy_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("backend_upgrade_policy_ready", criteria)
        self.assertTrue(criteria["backend_upgrade_policy_ready"].get("ok"))
        self.assertIn("api_interop_policy_ready", criteria)
        self.assertTrue(criteria["api_interop_policy_ready"].get("ok"))
        self.assertIn("ticket_branch_pr_workflow", criteria)
        self.assertTrue(criteria["ticket_branch_pr_workflow"].get("ok"))
        self.assertIn("git_hygiene_instrumented", criteria)
        self.assertTrue(criteria["git_hygiene_instrumented"].get("ok"))
        self.assertIn("git_hygiene_remediation", criteria)
        self.assertTrue(criteria["git_hygiene_remediation"].get("ok"))
        self.assertIn("swarm_lanes_configured", criteria)
        self.assertTrue(criteria["swarm_lanes_configured"].get("ok"))
        self.assertIn("t1_pr_ratio_policy", criteria)
        self.assertTrue(criteria["t1_pr_ratio_policy"].get("ok"))
        self.assertIn("deterministic_backlog_control", criteria)
        self.assertTrue(criteria["deterministic_backlog_control"].get("ok"))
        self.assertIn("pr_approval_remediation", criteria)
        self.assertTrue(criteria["pr_approval_remediation"].get("ok"))
        self.assertEqual(report["summary"]["criteria_failed"], 0)
        self.assertTrue(report["summary"]["backend_upgrade_policy_ok"])
        self.assertTrue(report["summary"]["api_interop_policy_ok"])
        self.assertTrue(report["summary"]["git_delivery_policy_ok"])
        self.assertTrue(report["summary"]["pr_tier_policy_ok"])
        self.assertGreater(report["summary"]["lanes_total_count"], 0)
        self.assertTrue(report["summary"]["git_hygiene_remediation_ok"])
        self.assertEqual(report["summary"]["git_hygiene_remediation_remote_blocked_open_pr_count"], 0)
        self.assertEqual(report["summary"]["git_hygiene_remediation_worktree_removed_count"], 0)

    def test_build_report_fails_when_backend_upgrade_policy_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "backend_upgrade_policy_health.json",
                {
                    "ok": False,
                    "summary": {
                        "violation_count": 2,
                        "release_phase": "foundation",
                        "dependency_checks_passed": 1,
                        "activation_task_checks_passed": 1,
                    },
                },
            )
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("backend_upgrade_policy_ready", criteria)
        self.assertFalse(criteria["backend_upgrade_policy_ready"].get("ok"))
        self.assertGreater(report["summary"]["criteria_failed"], 0)

    def test_build_report_fails_when_api_interop_policy_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "api_interop_policy_health.json",
                {
                    "ok": False,
                    "summary": {
                        "violation_count": 3,
                        "release_phase": "foundation",
                        "dependency_checks_passed": 2,
                        "activation_prereq_checks_passed": 2,
                    },
                },
            )
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("api_interop_policy_ready", criteria)
        self.assertFalse(criteria["api_interop_policy_ready"].get("ok"))
        self.assertGreater(report["summary"]["criteria_failed"], 0)

    def test_build_report_fails_when_git_delivery_policy_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "git_delivery_policy_health.json",
                {
                    "ok": False,
                    "summary": {
                        "violation_count": 2,
                        "branch": "codex/feature-large-fix",
                        "ticket_branch_match": False,
                        "effective_changed_lines": 880,
                        "max_changed_lines": 400,
                        "pr_found": False,
                        "pr_approvals": 0,
                    },
                },
            )
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("ticket_branch_pr_workflow", criteria)
        self.assertFalse(criteria["ticket_branch_pr_workflow"].get("ok"))
        self.assertFalse(report["summary"]["git_delivery_policy_ok"])
        self.assertEqual(report["summary"]["git_delivery_policy_violation_count"], 2)

    def test_build_report_fails_when_git_hygiene_policy_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "git_hygiene_health.json",
                {
                    "ok": False,
                    "summary": {
                        "violation_count": 2,
                        "local_branch_count": 92,
                        "remote_branch_count": 80,
                        "total_branch_count": 172,
                        "stale_local_branch_count": 55,
                        "max_total_branches": 140,
                    },
                },
            )
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("git_hygiene_instrumented", criteria)
        self.assertFalse(criteria["git_hygiene_instrumented"].get("ok"))
        self.assertFalse(report["summary"]["git_hygiene_ok"])
        self.assertEqual(report["summary"]["git_hygiene_violation_count"], 2)

    def test_build_report_fails_when_git_hygiene_remediation_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "git_hygiene_remediation.json",
                {
                    "ok": False,
                    "summary": {
                        "error_count": 1,
                        "remote_candidate_count": 42,
                        "local_candidate_count": 8,
                        "remote_deleted_count": 0,
                        "local_deleted_count": 0,
                    },
                },
            )
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("git_hygiene_remediation", criteria)
        self.assertFalse(criteria["git_hygiene_remediation"].get("ok"))
        self.assertFalse(report["summary"]["git_hygiene_remediation_ok"])
        self.assertEqual(report["summary"]["git_hygiene_remediation_error_count"], 1)

    def test_build_report_fails_when_deterministic_backlog_control_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "deterministic_backlog_health.json",
                {
                    "ok": False,
                    "summary": {
                        "ready_after": 3,
                        "ready_min": 10,
                        "ready_target": 16,
                        "ready_max": 24,
                        "done_after": 2,
                        "action_count": 0,
                    },
                    "backlog_updated": False,
                },
            )
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("deterministic_backlog_control", criteria)
        self.assertFalse(criteria["deterministic_backlog_control"].get("ok"))

    def test_build_report_fails_when_pr_approval_remediation_has_other_blockers(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "pr_approval_remediation.json",
                {
                    "ok": False,
                    "summary": {
                        "open_prs_seen": 8,
                        "approved_count": 1,
                        "self_blocked_count": 2,
                        "other_blocked_count": 3,
                    },
                },
            )
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("pr_approval_remediation", criteria)
        self.assertFalse(criteria["pr_approval_remediation"].get("ok"))
        self.assertEqual(report["summary"]["pr_remediation_other_blocked_count"], 3)
        self.assertGreater(report["summary"]["criteria_failed"], 0)

    def test_build_report_fails_when_pr_tier_policy_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "pr_tier_policy_health.json",
                {
                    "ok": False,
                    "summary": {
                        "violation_count": 2,
                        "reviewed_prs": 10,
                        "ratio_base_prs": 10,
                        "t1_count": 3,
                        "escalated_count": 1,
                        "unlabeled_count": 6,
                        "conflict_count": 0,
                        "t1_ratio": 0.3,
                        "escalated_ratio": 0.1,
                        "unlabeled_ratio": 0.6,
                    },
                    "policy": {
                        "min_t1_ratio": 0.7,
                    },
                },
            )
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("t1_pr_ratio_policy", criteria)
        self.assertFalse(criteria["t1_pr_ratio_policy"].get("ok"))
        self.assertFalse(report["summary"]["pr_tier_policy_ok"])
        self.assertEqual(report["summary"]["pr_tier_policy_violation_count"], 2)

    def test_build_report_fails_when_lanes_unconfigured(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(root / "config" / "lanes.json", {"lanes": []})
            report = swarm_cycle_report.build_report(root)

        criteria = {row.get("id"): row for row in report.get("criteria", []) if isinstance(row, dict)}
        self.assertIn("swarm_lanes_configured", criteria)
        self.assertFalse(criteria["swarm_lanes_configured"].get("ok"))
        self.assertEqual(report["summary"]["lanes_total_count"], 0)


if __name__ == "__main__":
    unittest.main()
