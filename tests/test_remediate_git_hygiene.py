import importlib.util
import pathlib
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "remediate_git_hygiene.py"
spec = importlib.util.spec_from_file_location("remediate_git_hygiene", SCRIPT)
assert spec is not None and spec.loader is not None
remediate_git_hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remediate_git_hygiene)


class RemediateGitHygieneTests(unittest.TestCase):
    def test_archive_tag_name_sanitizes_branch_and_sha(self):
        tag = remediate_git_hygiene._archive_tag_name(
            branch="codex/issue:1 weird",
            commit_sha="abcdef1234567890",
            namespace="archive/branch-debt",
        )
        self.assertEqual(tag, "archive/branch-debt/codex-issue-1-weird-abcdef123456")

    def test_active_worktree_branches_parses_porcelain(self):
        payload = (
            "worktree /tmp/repo\n"
            "HEAD 123456\n"
            "branch refs/heads/codex/lane-a\n"
            "\n"
            "worktree /tmp/repo-recovery\n"
            "HEAD abcdef\n"
            "branch refs/heads/main\n"
        )
        with mock.patch.object(
            remediate_git_hygiene,
            "_git",
            return_value=mock.Mock(returncode=0, stdout=payload, stderr=""),
        ):
            branches = remediate_git_hygiene._active_worktree_branches(pathlib.Path("/tmp/repo"))
        self.assertEqual(branches, {"codex/lane-a", "main"})

    def test_active_worktree_branch_paths_parses_porcelain(self):
        payload = (
            "worktree /tmp/repo\n"
            "HEAD 123456\n"
            "branch refs/heads/codex/lane-a\n"
            "\n"
            "worktree /tmp/repo-recovery\n"
            "HEAD abcdef\n"
            "branch refs/heads/codex/lane-a\n"
            "\n"
            "worktree /tmp/repo-main\n"
            "HEAD ff00aa\n"
            "branch refs/heads/main\n"
        )
        with mock.patch.object(
            remediate_git_hygiene,
            "_git",
            return_value=mock.Mock(returncode=0, stdout=payload, stderr=""),
        ):
            paths = remediate_git_hygiene._active_worktree_branch_paths(pathlib.Path("/tmp/repo"))
        expected_lane_paths = {pathlib.Path("/tmp/repo").resolve(), pathlib.Path("/tmp/repo-recovery").resolve()}
        expected_main_paths = {pathlib.Path("/tmp/repo-main").resolve()}
        self.assertEqual(
            paths,
            {
                "codex/lane-a": expected_lane_paths,
                "main": expected_main_paths,
            },
        )

    def test_retire_stale_recovery_worktrees_removes_clean_and_keeps_failed(self):
        branch = "codex/issue-9-shared-recovery-20260212053733"
        worktree_paths = {
            branch: {
                pathlib.Path("/tmp/recovery-clean"),
                pathlib.Path("/tmp/recovery-dirty"),
            }
        }
        with mock.patch.object(
            remediate_git_hygiene,
            "_remove_worktree_if_clean",
            side_effect=[(True, ""), (False, "worktree_dirty")],
        ):
            result = remediate_git_hygiene._retire_stale_recovery_worktrees(
                pathlib.Path("/tmp/repo"),
                branch=branch,
                age_days=4,
                min_age_days=2,
                worktree_paths_by_branch=worktree_paths,
            )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["attempted_paths_count"], 2)
        self.assertEqual(result["removed_paths"], ["/tmp/recovery-clean"])
        self.assertEqual(result["failed"], [{"path": "/tmp/recovery-dirty", "reason": "worktree_dirty"}])
        self.assertEqual(worktree_paths, {branch: {pathlib.Path("/tmp/recovery-dirty")}})

    def test_ensure_archive_tag_creates_pushes_and_skips_create_when_present(self):
        missing_then_create = [
            mock.Mock(returncode=1, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]
        with mock.patch.object(remediate_git_hygiene, "_git", side_effect=missing_then_create) as git_mock:
            ok, reason = remediate_git_hygiene._ensure_archive_tag(
                pathlib.Path("/tmp/repo"),
                tag_name="archive/branch-debt/codex-lane-a-abcdef123456",
                target_ref="refs/heads/codex/lane-a",
                remote="origin",
                push_remote=True,
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(git_mock.call_count, 3)

        exists_then_push = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]
        with mock.patch.object(remediate_git_hygiene, "_git", side_effect=exists_then_push) as git_mock:
            ok, reason = remediate_git_hygiene._ensure_archive_tag(
                pathlib.Path("/tmp/repo"),
                tag_name="archive/branch-debt/codex-lane-a-abcdef123456",
                target_ref="refs/heads/codex/lane-a",
                remote="origin",
                push_remote=True,
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(git_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
