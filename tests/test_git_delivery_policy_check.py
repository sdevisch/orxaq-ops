import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_git_delivery_policy.py"

module_spec = importlib.util.spec_from_file_location("check_git_delivery_policy", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_git_delivery_policy = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_git_delivery_policy", check_git_delivery_policy)
module_spec.loader.exec_module(check_git_delivery_policy)


class GitDeliveryPolicyCheckTests(unittest.TestCase):
    def _base_policy(self) -> dict:
        return {
            "schema_version": "git-delivery-policy.v1",
            "branch": {
                "require_ticket_branch": True,
                "ticket_branch_regex": r"^(codex|claude|gemini|agent)/issue-[0-9]+-[a-z0-9._-]+$",
            },
            "change_block": {
                "enforce_max_changed_lines": True,
                "max_changed_lines": 400,
                "allow_env_override": "ORXAQ_ALLOW_LARGE_CHANGE_BLOCK",
            },
            "pull_request": {
                "require_pr": True,
                "require_open": True,
                "allow_draft": False,
                "require_review": True,
                "required_approvals": 1,
                "require_review_decision_approved": True,
                "allow_self_authored_approval_waiver": False,
                "allow_env_override": "ORXAQ_ALLOW_PR_WORKFLOW_BYPASS",
            },
            "monitoring": {"base_ref": "origin/main", "include_working_tree_changes": True},
        }

    def _git_facts(self, **overrides: object) -> dict:
        payload = {
            "branch": "codex/issue-123-small-fix",
            "base_ref": "origin/main",
            "merge_base": "abc123",
            "committed_changed_lines": 150,
            "committed_changed_files": 3,
            "staged_changed_lines": 0,
            "staged_changed_files": 0,
            "working_tree_changed_lines": 0,
            "working_tree_changed_files": 0,
            "effective_changed_lines": 150,
            "effective_changed_files": 3,
            "largest_file_changed_lines": 90,
        }
        payload.update(overrides)
        return payload

    def _pr_facts(self, **overrides: object) -> dict:
        payload = {
            "available": True,
            "found": True,
            "error": "",
            "number": 42,
            "state": "OPEN",
            "is_draft": False,
            "review_decision": "APPROVED",
            "approvals": 1,
            "reviews_total": 2,
            "url": "https://example.invalid/pr/42",
            "author_login": "someone-else",
            "actor_login": "automation-user",
        }
        payload.update(overrides)
        return payload

    def test_evaluate_passes_with_ticket_branch_small_block_and_approved_pr(self):
        report = check_git_delivery_policy.evaluate(
            policy=self._base_policy(),
            git_facts=self._git_facts(),
            pr_facts=self._pr_facts(),
            change_block_override=False,
            pr_override=False,
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 0)

    def test_evaluate_fails_when_branch_not_ticket_linked(self):
        report = check_git_delivery_policy.evaluate(
            policy=self._base_policy(),
            git_facts=self._git_facts(branch="codex/feature-small-fix"),
            pr_facts=self._pr_facts(),
            change_block_override=False,
            pr_override=False,
        )
        self.assertFalse(report["ok"])
        violation_types = {item.get("type") for item in report.get("violations", []) if isinstance(item, dict)}
        self.assertIn("ticket_branch_required", violation_types)

    def test_evaluate_fails_when_change_block_exceeds_limit_without_override(self):
        report = check_git_delivery_policy.evaluate(
            policy=self._base_policy(),
            git_facts=self._git_facts(effective_changed_lines=501),
            pr_facts=self._pr_facts(),
            change_block_override=False,
            pr_override=False,
        )
        self.assertFalse(report["ok"])
        violation_types = {item.get("type") for item in report.get("violations", []) if isinstance(item, dict)}
        self.assertIn("change_block_too_large", violation_types)

    def test_evaluate_uses_policy_effective_changed_lines_when_present(self):
        report = check_git_delivery_policy.evaluate(
            policy=self._base_policy(),
            git_facts=self._git_facts(effective_changed_lines=900, effective_changed_lines_policy=120),
            pr_facts=self._pr_facts(),
            change_block_override=False,
            pr_override=False,
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["effective_changed_lines"], 120)
        self.assertEqual(report["summary"]["effective_changed_lines_raw"], 900)

    def test_resolve_policy_changed_lines_with_baseline(self):
        resolved = check_git_delivery_policy.resolve_policy_changed_lines(
            {"effective_changed_lines": 900},
            use_baseline_delta=True,
            baseline={"effective_changed_lines": 760},
        )
        self.assertEqual(resolved["effective_changed_lines_policy"], 140)
        self.assertTrue(resolved["baseline_used"])

    def test_evaluate_allows_large_change_block_with_override(self):
        report = check_git_delivery_policy.evaluate(
            policy=self._base_policy(),
            git_facts=self._git_facts(effective_changed_lines=777),
            pr_facts=self._pr_facts(),
            change_block_override=True,
            pr_override=False,
        )
        self.assertTrue(report["ok"])
        self.assertTrue(report["summary"]["change_block_override_used"])

    def test_evaluate_fails_when_pr_not_approved(self):
        report = check_git_delivery_policy.evaluate(
            policy=self._base_policy(),
            git_facts=self._git_facts(),
            pr_facts=self._pr_facts(review_decision="REVIEW_REQUIRED", approvals=0),
            change_block_override=False,
            pr_override=False,
        )
        self.assertFalse(report["ok"])
        violation_types = {item.get("type") for item in report.get("violations", []) if isinstance(item, dict)}
        self.assertIn("pr_approval_threshold_not_met", violation_types)
        self.assertIn("pr_review_decision_not_approved", violation_types)

    def test_evaluate_waives_self_authored_approval_when_configured(self):
        policy = self._base_policy()
        policy["pull_request"]["allow_self_authored_approval_waiver"] = True
        report = check_git_delivery_policy.evaluate(
            policy=policy,
            git_facts=self._git_facts(),
            pr_facts=self._pr_facts(
                review_decision="REVIEW_REQUIRED",
                approvals=0,
                reviews_total=0,
                author_login="automation-user",
                actor_login="automation-user",
            ),
            change_block_override=False,
            pr_override=False,
        )
        self.assertTrue(report["ok"])
        summary = report["summary"]
        self.assertTrue(summary["pr_self_approval_waiver_active"])
        self.assertEqual(summary["violation_count"], 0)
        self.assertGreaterEqual(summary["warning_count"], 1)

    def test_main_writes_report_and_honors_strict(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            policy_file = td_path / "policy.json"
            report_file = td_path / "report.json"
            policy_file.write_text(json.dumps(self._base_policy()) + "\n", encoding="utf-8")

            with mock.patch(
                "check_git_delivery_policy.load_git_facts",
                return_value=self._git_facts(branch="codex/feature-small-fix"),
            ), mock.patch(
                "check_git_delivery_policy.load_pr_facts",
                return_value=self._pr_facts(),
            ):
                rc = check_git_delivery_policy.main(
                    [
                        "--root",
                        td,
                        "--repo-root",
                        td,
                        "--policy-file",
                        str(policy_file),
                        "--output",
                        str(report_file),
                        "--strict",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 1)
            report = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertEqual(report["summary"]["violation_count"], 1)


if __name__ == "__main__":
    unittest.main()
