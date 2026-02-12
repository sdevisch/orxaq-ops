import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_pr_t1_ratio.py"

module_spec = importlib.util.spec_from_file_location("check_pr_t1_ratio", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_pr_t1_ratio = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_pr_t1_ratio", check_pr_t1_ratio)
module_spec.loader.exec_module(check_pr_t1_ratio)


class PrTierPolicyCheckTests(unittest.TestCase):
    def _base_policy(self) -> dict:
        return {
            "enabled": True,
            "labels": {
                "t1": ["tier:t1"],
                "escalated": ["tier:escalated"],
            },
            "require_tier_label": True,
            "max_unlabeled_ratio": 0.1,
            "min_t1_ratio": 0.7,
            "max_escalated_ratio": 0.3,
            "min_sample_prs": 2,
            "enforce_min_sample": False,
            "max_violations": 0,
        }

    def test_evaluate_passes_for_t1_majority(self):
        payloads = [
            {
                "repo": "Orxaq/orxaq-ops",
                "state": "open",
                "fetch_error": "",
                "pull_requests": [
                    {"number": 1, "title": "A", "labels": [{"name": "tier:t1"}]},
                    {"number": 2, "title": "B", "labels": [{"name": "tier:t1"}]},
                    {"number": 3, "title": "C", "labels": [{"name": "tier:t1"}]},
                    {"number": 4, "title": "D", "labels": [{"name": "tier:escalated"}]},
                ],
            }
        ]
        report = check_pr_t1_ratio.evaluate_policy(policy=self._base_policy(), repo_payloads=payloads)
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 0)
        self.assertEqual(report["summary"]["t1_count"], 3)
        self.assertAlmostEqual(report["summary"]["t1_ratio"], 3.0 / 4.0, places=4)

    def test_evaluate_fails_for_unlabeled_mix(self):
        payloads = [
            {
                "repo": "Orxaq/orxaq-ops",
                "state": "open",
                "fetch_error": "",
                "pull_requests": [
                    {"number": 1, "title": "A", "labels": []},
                    {"number": 2, "title": "B", "labels": []},
                    {"number": 3, "title": "C", "labels": [{"name": "tier:escalated"}]},
                ],
            }
        ]
        report = check_pr_t1_ratio.evaluate_policy(policy=self._base_policy(), repo_payloads=payloads)
        self.assertFalse(report["ok"])
        types = {row.get("type") for row in report.get("violations", []) if isinstance(row, dict)}
        self.assertIn("missing_tier_labels", types)
        self.assertIn("t1_ratio_below_threshold", types)

    def test_main_strict_passes_with_mocked_repo_data(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            policy_file = td_path / "policy.json"
            out_file = td_path / "report.json"
            policy = self._base_policy()
            policy.update(
                {
                    "repos": ["Orxaq/orxaq-ops"],
                    "states": ["open"],
                    "lookback_days": 30,
                    "max_prs_per_repo": 20,
                    "include_drafts": False,
                }
            )
            policy_file.write_text(json.dumps(policy) + "\n", encoding="utf-8")

            now = check_pr_t1_ratio._utc_now_iso().replace("Z", "+00:00")
            pr_rows = [
                {"number": 1, "title": "A", "labels": [{"name": "tier:t1"}], "createdAt": now, "isDraft": False},
                {"number": 2, "title": "B", "labels": [{"name": "tier:t1"}], "createdAt": now, "isDraft": False},
                {"number": 3, "title": "C", "labels": [{"name": "tier:t1"}], "createdAt": now, "isDraft": False},
                {"number": 4, "title": "D", "labels": [{"name": "tier:escalated"}], "createdAt": now, "isDraft": False},
            ]
            with mock.patch.object(check_pr_t1_ratio, "_collect_repo_prs", return_value=(pr_rows, "")):
                rc = check_pr_t1_ratio.main(
                    [
                        "--root",
                        td,
                        "--policy-file",
                        str(policy_file),
                        "--output",
                        str(out_file),
                        "--strict",
                        "--json",
                    ]
                )

            self.assertEqual(rc, 0)
            report = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["summary"]["violation_count"], 0)

    def test_main_strict_fails_when_policy_broken(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            policy_file = td_path / "policy.json"
            out_file = td_path / "report.json"
            policy = self._base_policy()
            policy.update(
                {
                    "repos": ["Orxaq/orxaq-ops"],
                    "states": ["open"],
                    "lookback_days": 30,
                    "max_prs_per_repo": 20,
                    "include_drafts": False,
                }
            )
            policy_file.write_text(json.dumps(policy) + "\n", encoding="utf-8")

            now = check_pr_t1_ratio._utc_now_iso().replace("Z", "+00:00")
            pr_rows = [
                {"number": 1, "title": "A", "labels": [], "createdAt": now, "isDraft": False},
                {"number": 2, "title": "B", "labels": [], "createdAt": now, "isDraft": False},
            ]
            with mock.patch.object(check_pr_t1_ratio, "_collect_repo_prs", return_value=(pr_rows, "")):
                rc = check_pr_t1_ratio.main(
                    [
                        "--root",
                        td,
                        "--policy-file",
                        str(policy_file),
                        "--output",
                        str(out_file),
                        "--strict",
                        "--json",
                    ]
                )

            self.assertEqual(rc, 1)
            report = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertGreater(report["summary"]["violation_count"], 0)


if __name__ == "__main__":
    unittest.main()
