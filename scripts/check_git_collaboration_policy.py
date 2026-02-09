#!/usr/bin/env python3
"""Enforce collaboration-safe git policy for human + IDE + API agent workflows."""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
from pathlib import Path

DEFAULT_PROTECTED_BRANCHES = ("main", "master", "develop", "trunk")
DEFAULT_ALLOWED_BRANCH_PREFIXES = (
    "codex/",
    "claude/",
    "gemini/",
    "agent/",
    "feature/",
    "fix/",
    "bugfix/",
    "hotfix/",
    "chore/",
    "docs/",
    "refactor/",
    "release/",
)
DEFAULT_AGENT_BRANCH_PREFIXES = ("codex/", "claude/", "gemini/", "agent/")
AGENT_SESSION_ENV_KEYS = (
    "CODEX_THREAD_ID",
    "CLAUDE_SESSION_ID",
    "ORXAQ_AUTONOMY_SESSION_ID",
    "ORXAQ_AUTONOMY_RUN_ID",
    "ORXAQ_AGENT_SESSION",
    "CURSOR_AGENT",
)


def _env_true(name: str) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _run_git(repo_root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        check=check,
    )


def _current_branch(repo_root: Path) -> str:
    result = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    return result.stdout.strip()


def _tracked_paths(repo_root: Path) -> list[str]:
    result = _run_git(repo_root, ["ls-files", "-z"])
    payload = result.stdout
    return [item for item in payload.split("\x00") if item]


def _agent_session_active() -> bool:
    for key in AGENT_SESSION_ENV_KEYS:
        if str(os.environ.get(key, "")).strip():
            return True
    return False


def _check_branch_policy(
    *,
    branch: str,
    protected_branches: tuple[str, ...],
    allowed_prefixes: tuple[str, ...],
    agent_prefixes: tuple[str, ...],
    problems: list[str],
) -> None:
    if branch == "HEAD":
        problems.append("Detached HEAD is not allowed for collaborative work. Create/switch to a named branch.")
        return

    if branch in protected_branches and not _env_true("ORXAQ_ALLOW_PROTECTED_BRANCH_COMMITS"):
        problems.append(
            "Direct commits on protected branch "
            f"{branch!r} are blocked. Use a feature branch (for agents: codex/*). "
            "Set ORXAQ_ALLOW_PROTECTED_BRANCH_COMMITS=1 only for approved emergencies."
        )

    if branch not in protected_branches and not branch.startswith(allowed_prefixes):
        problems.append(
            "Branch naming policy failed. "
            f"Current branch {branch!r} must start with one of: {', '.join(allowed_prefixes)}"
        )

    if _agent_session_active() and not branch.startswith(agent_prefixes):
        problems.append(
            "Agent session detected but branch is not agent-scoped. "
            f"Current branch {branch!r} must start with one of: {', '.join(agent_prefixes)}"
        )


def _check_conflict_markers(repo_root: Path, problems: list[str]) -> None:
    # Restrict to start/end marker lines to avoid false positives from legit markdown
    # heading separators (`=======`) that are common in docs/dependencies.
    result = _run_git(repo_root, ["grep", "-nE", r"^(<<<<<<< |>>>>>>> |\|\|\|\|\|\|\| )", "--", "."], check=False)
    if result.returncode == 0:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        preview = "; ".join(lines[:5])
        problems.append(f"Merge conflict markers found in tracked files: {preview}")
    elif result.returncode not in (0, 1):
        problems.append(f"Unable to scan conflict markers: {result.stderr.strip() or result.stdout.strip()}")


def _check_unmerged_paths(repo_root: Path, problems: list[str]) -> None:
    result = _run_git(repo_root, ["diff", "--name-only", "--diff-filter=U"], check=False)
    if result.returncode != 0:
        problems.append(
            "Unable to scan unresolved merge conflicts: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return
    unmerged = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if unmerged:
        preview = ", ".join(unmerged[:12])
        problems.append(
            "Unresolved merge conflicts detected: "
            f"{preview}. Resolve conflicts first. Clean merges are allowed when this list is empty."
        )


def _check_forbidden_tracked_globs(
    *,
    tracked_paths: list[str],
    forbidden_globs: tuple[str, ...],
    allow_globs: tuple[str, ...],
    problems: list[str],
) -> None:
    if not forbidden_globs:
        return

    def _matches(path: str, patterns: tuple[str, ...]) -> bool:
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    blocked: list[str] = []
    for relpath in tracked_paths:
        if _matches(relpath, forbidden_globs) and not _matches(relpath, allow_globs):
            blocked.append(relpath)
    if blocked:
        preview = ", ".join(sorted(blocked)[:12])
        problems.append(
            "Forbidden tracked file patterns detected. "
            f"First matches: {preview}"
        )


def _check_upstream_behind(repo_root: Path, problems: list[str]) -> None:
    upstream = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], check=False)
    if upstream.returncode != 0:
        return
    upstream_ref = upstream.stdout.strip()
    if not upstream_ref:
        return

    counts = _run_git(repo_root, ["rev-list", "--left-right", "--count", f"{upstream_ref}...HEAD"])
    raw = counts.stdout.strip().split()
    if len(raw) != 2:
        return
    behind = int(raw[0])
    ahead = int(raw[1])

    if behind > 0 and not _env_true("ORXAQ_ALLOW_BEHIND_PUSH"):
        problems.append(
            f"Branch is behind upstream ({upstream_ref}) by {behind} commit(s) and ahead by {ahead}. "
            "Rebase/merge before push. Set ORXAQ_ALLOW_BEHIND_PUSH=1 only for controlled exceptions."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate collaboration git policy.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--protected-branch", action="append", default=[])
    parser.add_argument("--allowed-branch-prefix", action="append", default=[])
    parser.add_argument("--agent-branch-prefix", action="append", default=[])
    parser.add_argument("--forbid-glob", action="append", default=[])
    parser.add_argument("--allow-glob", action="append", default=[])
    parser.add_argument("--check-upstream-behind", action="store_true")
    parser.add_argument("--skip-conflict-marker-check", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo_root = Path(args.repo_root).resolve()

    protected_branches = tuple(args.protected_branch) or DEFAULT_PROTECTED_BRANCHES
    allowed_prefixes = tuple(args.allowed_branch_prefix) or DEFAULT_ALLOWED_BRANCH_PREFIXES
    agent_prefixes = tuple(args.agent_branch_prefix) or DEFAULT_AGENT_BRANCH_PREFIXES
    forbidden_globs = tuple(args.forbid_glob)
    allow_globs = tuple(args.allow_glob)

    problems: list[str] = []

    try:
        branch = _current_branch(repo_root)
    except Exception as err:
        print(f"Git collaboration policy failed: cannot determine current branch: {err}")
        return 1

    _check_branch_policy(
        branch=branch,
        protected_branches=protected_branches,
        allowed_prefixes=allowed_prefixes,
        agent_prefixes=agent_prefixes,
        problems=problems,
    )

    if not args.skip_conflict_marker_check:
        _check_conflict_markers(repo_root, problems)
    _check_unmerged_paths(repo_root, problems)

    try:
        tracked_paths = _tracked_paths(repo_root)
    except Exception as err:
        print(f"Git collaboration policy failed: cannot list tracked files: {err}")
        return 1

    _check_forbidden_tracked_globs(
        tracked_paths=tracked_paths,
        forbidden_globs=forbidden_globs,
        allow_globs=allow_globs,
        problems=problems,
    )

    if args.check_upstream_behind:
        _check_upstream_behind(repo_root, problems)

    if problems:
        print("Git collaboration policy failed:")
        for item in problems:
            print(f"- {item}")
        return 1

    print(f"Git collaboration policy OK: branch={branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
