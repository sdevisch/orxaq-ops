import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "swarm_ready_queue.py"

module_spec = importlib.util.spec_from_file_location("swarm_ready_queue", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
swarm_ready_queue = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("swarm_ready_queue", swarm_ready_queue)
module_spec.loader.exec_module(swarm_ready_queue)


class SwarmReadyQueueTests(unittest.TestCase):
    def _write_json(self, path: pathlib.Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _base_root(self, td: str) -> pathlib.Path:
        root = pathlib.Path(td) / "orxaq-ops"
        self._write_json(root / "artifacts" / "model_connectivity.json", {"endpoint_unhealthy": 0, "endpoints": []})
        self._write_json(
            root / "artifacts" / "autonomy" / "swarm_todo_health" / "current_latest.json",
            {"distributed_todo": {"stale_file_count": 0, "unassigned_active_task_total": 0}, "warnings": []},
        )
        self._write_json(root / "artifacts" / "autonomy" / "provider_costs" / "summary.json", {"ok": True})
        self._write_json(
            root / "artifacts" / "autonomy" / "t1_basic_model_policy.json",
            {"ok": True, "summary": {"violation_count": 0}, "observability": {"ok": True, "latest_metric_age_minutes": 1}},
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "privilege_policy_health.json",
            {"ok": True, "summary": {"violation_count": 0, "scanned_events": 1}},
        )
        self._write_json(
            root / "artifacts" / "autonomy" / "git_delivery_policy_health.json",
            {
                "ok": True,
                "summary": {
                    "violation_count": 0,
                    "branch": "codex/issue-123-small-fix",
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
                    "total_branch_count": 64,
                    "max_total_branches": 140,
                    "stale_local_branch_count": 5,
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
                    "open_prs_seen": 3,
                    "approved_count": 1,
                    "self_blocked_count": 0,
                    "other_blocked_count": 0,
                },
            },
        )
        return root

    def test_build_queue_adds_api_interop_task_when_policy_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "api_interop_policy_health.json",
                {
                    "ok": False,
                    "summary": {
                        "violation_count": 2,
                        "release_phase": "foundation",
                        "dependency_checks_passed": 2,
                        "activation_prereq_checks_passed": 2,
                    },
                },
            )
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        task_ids = {row.get("id") for row in queue.get("tasks", []) if isinstance(row, dict)}
        self.assertIn("T1-API-INTEROP-POLICY-ENFORCEMENT", task_ids)
        self.assertFalse(queue["summary"]["api_interop_policy_ok"])

    def test_build_queue_keeps_api_interop_green_summary_when_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        self.assertTrue(queue["summary"]["api_interop_policy_ok"])
        self.assertEqual(queue["summary"]["api_interop_policy_violation_count"], 0)
        self.assertTrue(queue["summary"]["git_delivery_policy_ok"])
        self.assertEqual(queue["summary"]["git_delivery_policy_violation_count"], 0)

    def test_build_queue_adds_git_delivery_task_when_policy_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "git_delivery_policy_health.json",
                {
                    "ok": False,
                    "summary": {
                        "violation_count": 2,
                        "branch": "codex/feature-large-fix",
                        "effective_changed_lines": 820,
                        "max_changed_lines": 400,
                        "pr_found": False,
                        "pr_approvals": 0,
                    },
                },
            )
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        task_ids = {row.get("id") for row in queue.get("tasks", []) if isinstance(row, dict)}
        self.assertIn("T1-GIT-DELIVERY-POLICY-ENFORCEMENT", task_ids)
        self.assertFalse(queue["summary"]["git_delivery_policy_ok"])
        self.assertEqual(queue["summary"]["git_delivery_policy_violation_count"], 2)

    def test_build_queue_adds_git_hygiene_task_when_policy_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "git_hygiene_health.json",
                {
                    "ok": False,
                    "summary": {
                        "violation_count": 2,
                        "total_branch_count": 172,
                        "max_total_branches": 140,
                        "stale_local_branch_count": 55,
                    },
                },
            )
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        task_ids = {row.get("id") for row in queue.get("tasks", []) if isinstance(row, dict)}
        self.assertIn("T1-GIT-HYGIENE-ENFORCEMENT", task_ids)
        self.assertFalse(queue["summary"]["git_hygiene_ok"])
        self.assertEqual(queue["summary"]["git_hygiene_violation_count"], 2)

    def test_build_queue_adds_backlog_control_task_when_unhealthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "deterministic_backlog_health.json",
                {
                    "ok": False,
                    "summary": {
                        "ready_after": 4,
                        "ready_min": 10,
                        "ready_target": 16,
                        "ready_max": 24,
                        "action_count": 0,
                    },
                    "backlog_updated": False,
                },
            )
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        task_ids = {row.get("id") for row in queue.get("tasks", []) if isinstance(row, dict)}
        self.assertIn("T1-BACKLOG-CONTROL-HEALTH", task_ids)
        self.assertFalse(queue["summary"]["deterministic_backlog_ok"])

    def test_build_queue_adds_git_hygiene_backlog_task_when_actionable_candidates_exist(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "git_hygiene_remediation.json",
                {
                    "ok": True,
                    "summary": {
                        "error_count": 0,
                        "remote_stale_prefix_count": 12,
                        "local_stale_prefix_count": 8,
                        "remote_candidate_count": 2,
                        "local_candidate_count": 3,
                        "remote_blocked_open_pr_count": 4,
                        "remote_blocked_unmerged_count": 6,
                        "local_blocked_unmerged_count": 5,
                        "local_blocked_worktree_count": 1,
                        "worktree_prune_removed_count": 0,
                        "worktree_remove_attempted_count": 1,
                        "worktree_removed_count": 1,
                        "worktree_remove_failed_count": 0,
                        "remote_deleted_count": 1,
                        "local_deleted_count": 1,
                    },
                },
            )
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        task_ids = {row.get("id") for row in queue.get("tasks", []) if isinstance(row, dict)}
        self.assertIn("T1-GIT-HYGIENE-REMEDIATION-BACKLOG", task_ids)
        self.assertIn("T1-GIT-HYGIENE-WORKTREE-RECONCILE", task_ids)
        self.assertIn("T1-GIT-HYGIENE-BRANCH-GOVERNANCE", task_ids)
        self.assertEqual(queue["summary"]["git_hygiene_remediation_remote_candidates"], 2)
        self.assertEqual(queue["summary"]["git_hygiene_remediation_local_candidates"], 3)
        self.assertEqual(queue["summary"]["git_hygiene_remediation_worktree_removed_count"], 1)

    def test_build_queue_adds_branch_governance_task_for_non_actionable_stale_branches(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "git_hygiene_remediation.json",
                {
                    "ok": True,
                    "summary": {
                        "error_count": 0,
                        "remote_stale_prefix_count": 40,
                        "local_stale_prefix_count": 32,
                        "remote_candidate_count": 0,
                        "local_candidate_count": 0,
                        "remote_blocked_open_pr_count": 10,
                        "remote_blocked_unmerged_count": 21,
                        "local_blocked_unmerged_count": 18,
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
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        task_ids = {row.get("id") for row in queue.get("tasks", []) if isinstance(row, dict)}
        self.assertIn("T1-GIT-HYGIENE-BRANCH-GOVERNANCE", task_ids)
        self.assertNotIn("T1-GIT-HYGIENE-REMEDIATION-BACKLOG", task_ids)
        self.assertEqual(queue["summary"]["git_hygiene_remediation_remote_blocked_open_pr_count"], 10)

    def test_build_queue_adds_pr_approval_remediation_task_when_other_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "pr_approval_remediation.json",
                {
                    "ok": False,
                    "summary": {
                        "open_prs_seen": 5,
                        "approved_count": 1,
                        "self_blocked_count": 1,
                        "other_blocked_count": 2,
                    },
                },
            )
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        task_ids = {row.get("id") for row in queue.get("tasks", []) if isinstance(row, dict)}
        self.assertIn("T1-PR-APPROVAL-REMEDIATION", task_ids)
        self.assertEqual(queue["summary"]["pr_approval_other_blocked_count"], 2)

    def test_build_queue_adds_reviewer_capacity_task_when_self_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            self._write_json(
                root / "artifacts" / "autonomy" / "pr_approval_remediation.json",
                {
                    "ok": True,
                    "summary": {
                        "open_prs_seen": 4,
                        "approved_count": 0,
                        "self_blocked_count": 3,
                        "other_blocked_count": 0,
                    },
                },
            )
            queue = swarm_ready_queue.build_queue(root, max_items=50)

        task_ids = {row.get("id") for row in queue.get("tasks", []) if isinstance(row, dict)}
        self.assertIn("T1-PR-REVIEWER-CAPACITY", task_ids)
        self.assertEqual(queue["summary"]["pr_approval_self_blocked_count"], 3)

    def test_build_queue_can_refresh_todo_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_root(td)
            # Provide a minimal refresh script that writes current_latest.json with no unassigned tasks.
            checker = root / "scripts" / "swarm_todo_health_current.py"
            checker.parent.mkdir(parents=True, exist_ok=True)
            checker.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import argparse, json",
                        "",
                        "def main():",
                        "  p = argparse.ArgumentParser()",
                        "  p.add_argument('--output-file', required=True)",
                        "  p.add_argument('--json', action='store_true')",
                        "  p.add_argument('--root', default='.')",
                        "  args = p.parse_args()",
                        "  payload = {\"distributed_todo\": {\"stale_file_count\": 0, \"unassigned_active_task_total\": 0}, \"warnings\": []}",
                        "  with open(args.output_file, 'w', encoding='utf-8') as f: f.write(json.dumps(payload) + '\\n')",
                        "  if args.json: print(json.dumps(payload))",
                        "  return 0",
                        "",
                        "if __name__ == '__main__':",
                        "  raise SystemExit(main())",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            queue = swarm_ready_queue.build_queue(root, max_items=50, refresh_todo_summary=True)

        self.assertTrue(queue["summary"]["todo_health_refresh_attempted"])
        self.assertTrue(queue["summary"]["todo_health_refresh_ok"])
        self.assertEqual(queue["summary"]["unassigned_active_task_total"], 0)


if __name__ == "__main__":
    unittest.main()
