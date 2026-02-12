import subprocess

from pathlib import Path

from orxaq_autonomy import manager


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_ensure_git_common_core_bare_false_remediates_legacy_worktree(tmp_path: Path) -> None:
    # Reproduce the failure mode:
    # - common config has core.bare=true
    # - main worktree later adds a config.worktree override core.bare=false
    # - existing worktrees created before the override remain "bare" and fail git status/checkout
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run_git(repo, ["init"]).returncode == 0
    assert _run_git(repo, ["config", "user.email", "codex@example.com"]).returncode == 0
    assert _run_git(repo, ["config", "user.name", "Codex"]).returncode == 0

    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    assert _run_git(repo, ["add", "a.txt"]).returncode == 0
    assert _run_git(repo, ["commit", "-m", "init"]).returncode == 0

    # Enable worktree config files (config.worktree).
    assert _run_git(repo, ["config", "extensions.worktreeConfig", "true"]).returncode == 0

    # Common config declares the repo as bare.
    assert _run_git(repo, ["config", "--local", "core.bare", "true"]).returncode == 0

    # Create a worktree before the main worktree overrides core.bare.
    wt = tmp_path / "wt"
    assert _run_git(repo, ["worktree", "add", str(wt), "-b", "test", "HEAD"]).returncode == 0

    # Main worktree overrides core.bare=false, making the root repo usable while the legacy worktree remains bare.
    assert _run_git(repo, ["config", "--worktree", "core.bare", "false"]).returncode == 0

    assert _run_git(wt, ["rev-parse", "--is-inside-work-tree"]).stdout.strip() == "false"
    assert _run_git(wt, ["status", "--porcelain"]).returncode != 0

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    payload = manager._ensure_git_common_core_bare_false(repo, artifacts_dir=artifacts)
    assert payload["remediation_applied"] is True
    assert payload["fixed"] is True

    assert _run_git(wt, ["rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"
    assert _run_git(wt, ["status", "--porcelain"]).returncode == 0

    report = artifacts / "git_worktree_common_config_remediation.json"
    assert report.exists()

