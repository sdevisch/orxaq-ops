import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import gitops


class GitOpsTests(unittest.TestCase):
    def test_open_pr_prefers_gh_when_available(self):
        cp = mock.Mock(returncode=0, stdout="https://github.com/acme/repo/pull/42\n", stderr="")
        with mock.patch("orxaq_autonomy.gitops._gh_available", return_value=True), mock.patch(
            "orxaq_autonomy.gitops._run",
            return_value=cp,
        ):
            payload = gitops.open_pr(
                repo="acme/repo",
                base="main",
                head="feature-x",
                title="Add feature",
                body="details",
                draft=False,
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "gh")
        self.assertEqual(payload["number"], 42)

    def test_open_pr_falls_back_to_api(self):
        with mock.patch("orxaq_autonomy.gitops._gh_available", return_value=False), mock.patch(
            "orxaq_autonomy.gitops._api_request",
            return_value={"number": 7, "html_url": "https://github.com/acme/repo/pull/7"},
        ):
            payload = gitops.open_pr(
                repo="acme/repo",
                base="main",
                head="feature-x",
                title="Add feature",
                body="details",
                draft=True,
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "api")
        self.assertEqual(payload["number"], 7)

    def test_wait_for_pr_closes_and_files_issue_after_repeated_failures(self):
        status = gitops.PrStatus(
            number=25,
            url="https://github.com/acme/repo/pull/25",
            state="OPEN",
            merge_state_status="BLOCKED",
            checks_passed=False,
            failed_checks=["unit-tests"],
            pending_checks=[],
        )
        with mock.patch("orxaq_autonomy.gitops.get_pr_status", side_effect=[status, status]), mock.patch(
            "orxaq_autonomy.gitops.close_pr"
        ) as close_pr, mock.patch("orxaq_autonomy.gitops.open_issue") as open_issue:
            payload = gitops.wait_for_pr(
                repo="acme/repo",
                pr_number=25,
                interval_sec=1,
                max_attempts=4,
                failure_threshold=2,
                close_on_failure=True,
                open_issue_on_failure=True,
            )
        self.assertFalse(payload["ok"])
        self.assertIn("Repeated CI failures", payload["reason"])
        close_pr.assert_called_once()
        open_issue.assert_called_once()

    def test_get_pr_status_api_fallback_parses_commit_statuses(self):
        pr_payload = {
            "number": 30,
            "html_url": "https://github.com/acme/repo/pull/30",
            "state": "open",
            "mergeable_state": "clean",
            "head": {"sha": "abc123"},
        }
        commit_payload = {
            "statuses": [
                {"context": "lint", "state": "success"},
                {"context": "tests", "state": "failure"},
            ]
        }
        with mock.patch("orxaq_autonomy.gitops._gh_available", return_value=False), mock.patch(
            "orxaq_autonomy.gitops._api_request",
            side_effect=[pr_payload, commit_payload],
        ):
            status = gitops.get_pr_status("acme/repo", 30)
        self.assertEqual(status.number, 30)
        self.assertFalse(status.checks_passed)
        self.assertEqual(status.failed_checks, ["tests"])

    def test_merge_pr_rejects_low_swarm_health(self):
        status = gitops.PrStatus(
            number=18,
            url="https://github.com/acme/repo/pull/18",
            state="OPEN",
            merge_state_status="CLEAN",
            checks_passed=True,
            failed_checks=[],
            pending_checks=[],
        )
        with mock.patch("orxaq_autonomy.gitops.get_pr_status", return_value=status):
            with self.assertRaises(gitops.GitOpsError):
                gitops.merge_pr(
                    repo="acme/repo",
                    pr_number=18,
                    method="squash",
                    delete_branch=False,
                    min_swarm_health=85.0,
                    swarm_health_score=72.0,
                    require_ci_green=True,
                )

    def test_read_swarm_health_score_from_summary(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "health.json"
            path.write_text(json.dumps({"summary": {"score": 91.5}}), encoding="utf-8")
            score = gitops.read_swarm_health_score(path)
            self.assertEqual(score, 91.5)


if __name__ == "__main__":
    unittest.main()
