import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_git_hygiene_health.py"

module_spec = importlib.util.spec_from_file_location("check_git_hygiene_health", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_git_hygiene_health = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_git_hygiene_health", check_git_hygiene_health)
module_spec.loader.exec_module(check_git_hygiene_health)


class GitHygieneHealthCheckTests(unittest.TestCase):
    def _init_repo(self, repo: pathlib.Path) -> None:
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True, capture_output=True)
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    def _current_branch(self, repo: pathlib.Path) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()

    def test_main_passes_with_healthy_branch_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            repo = root / "repo"
            self._init_repo(repo)
            default_branch = self._current_branch(repo)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature/a"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "checkout", default_branch], check=True, capture_output=True)
            policy_file = root / "policy.json"
            policy_file.write_text(
                json.dumps(
                    {
                        "branch_inventory": {
                            "max_local_branches": 10,
                            "max_remote_branches": 10,
                            "max_total_branches": 20,
                            "stale_days": 3650,
                            "max_stale_local_branches": 10,
                            "protected_branch_patterns": ["^master$", "^main$"],
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output_file = root / "git_hygiene_health.json"
            rc = check_git_hygiene_health.main(
                [
                    "--root",
                    str(root),
                    "--repo-root",
                    str(repo),
                    "--policy-file",
                    str(policy_file),
                    "--output",
                    str(output_file),
                    "--json",
                ]
            )
            self.assertEqual(rc, 0)
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["summary"]["violation_count"], 0)

    def test_main_fails_when_total_branch_count_exceeds_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            repo = root / "repo"
            self._init_repo(repo)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature/a"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature/b"], check=True, capture_output=True)
            policy_file = root / "policy.json"
            policy_file.write_text(
                json.dumps(
                    {
                        "branch_inventory": {
                            "max_local_branches": 2,
                            "max_remote_branches": 10,
                            "max_total_branches": 2,
                            "stale_days": 3650,
                            "max_stale_local_branches": 10,
                            "protected_branch_patterns": ["^master$", "^main$"],
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output_file = root / "git_hygiene_health.json"
            rc = check_git_hygiene_health.main(
                [
                    "--root",
                    str(root),
                    "--repo-root",
                    str(repo),
                    "--policy-file",
                    str(policy_file),
                    "--output",
                    str(output_file),
                    "--json",
                ]
            )
            self.assertEqual(rc, 1)
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertGreater(payload["summary"]["violation_count"], 0)
            self.assertTrue(
                any(item.get("type") == "total_branch_count_exceeded" for item in payload.get("violations", []))
            )

    def test_main_fails_when_legacy_suppression_caps_are_exceeded(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            repo = root / "repo"
            self._init_repo(repo)
            default_branch = self._current_branch(repo)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature/a"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature/b"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "checkout", default_branch], check=True, capture_output=True)

            baseline_file = root / "baseline.json"
            baseline_file.write_text(
                json.dumps(
                    {
                        "schema_version": "git-hygiene-baseline.v1",
                        "generated_at_utc": "2026-02-11T00:00:00Z",
                        "repo_root": str(repo),
                        "policy_file": str(root / "policy.json"),
                        "local_branch_count": 10,
                        "remote_branch_count": 0,
                        "total_branch_count": 10,
                        "stale_local_branch_count": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            policy_file = root / "policy.json"
            policy_file.write_text(
                json.dumps(
                    {
                        "branch_inventory": {
                            "max_local_branches": 1,
                            "max_remote_branches": 100,
                            "max_total_branches": 1,
                            "stale_days": 3650,
                            "max_stale_local_branches": 100,
                            "protected_branch_patterns": ["^master$", "^main$"],
                        },
                        "monitoring": {
                            "baseline_file": str(baseline_file),
                            "history_file": str(root / "history.ndjson"),
                            "use_baseline_guard": True,
                            "allow_missing_baseline": False,
                            "allow_legacy_above_threshold_when_nonincreasing": True,
                            "require_nonincreasing": False,
                            "max_delta_local_branches": 0,
                            "max_delta_remote_branches": 0,
                            "max_delta_total_branches": 0,
                            "legacy_suppression_stagnation": {
                                "enabled": True,
                                "window_runs": 2,
                                "min_reduction_local_branches": 1,
                                "min_reduction_remote_branches": 1,
                                "min_reduction_total_branches": 1,
                                "max_allowed_local_branches_while_suppressed": 2,
                                "max_allowed_remote_branches_while_suppressed": 2,
                                "max_allowed_total_branches_while_suppressed": 2,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output_file = root / "git_hygiene_health.json"
            rc = check_git_hygiene_health.main(
                [
                    "--root",
                    str(root),
                    "--repo-root",
                    str(repo),
                    "--policy-file",
                    str(policy_file),
                    "--output",
                    str(output_file),
                    "--json",
                ]
            )
            self.assertEqual(rc, 1)
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertTrue(
                any(
                    item.get("type") == "legacy_suppression_absolute_cap_exceeded"
                    for item in payload.get("violations", [])
                )
            )


if __name__ == "__main__":
    unittest.main()
