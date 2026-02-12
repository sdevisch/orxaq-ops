#!/usr/bin/env python3
"""Deterministically remediate stale merged git branches."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = Path("artifacts/autonomy/git_hygiene_remediation.json")
DEFAULT_PREFIXES = ("codex/", "claude/", "gemini/", "agent/", "autonomy/")
DEFAULT_PROTECTED = ("main", "master", "develop", "dev", "staging", "production")
RECOVERY_BRANCH_PATTERN = re.compile(r"(?:^|[-_/])recovery(?:$|[-_/0-9])", re.IGNORECASE)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_text(value: Any) -> str:
    return str(value).strip()


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=repo)


def _gh(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run(["gh", *args], cwd=cwd)


def _parse_repo_slug(remote_url: str) -> str:
    text = remote_url.strip()
    patterns = (
        r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"^git@[^:]+:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            owner = _as_text(match.group("owner"))
            repo = _as_text(match.group("repo"))
            if owner and repo:
                return f"{owner}/{repo}"
    return ""


def _load_pr_heads(repo: Path, repo_slug: str, *, state: str) -> tuple[set[str], str]:
    if not repo_slug:
        return (set(), "missing_repo_slug")
    state_norm = _as_text(state).lower() or "open"
    proc = _gh(
        [
            "pr",
            "list",
            "--repo",
            repo_slug,
            "--state",
            state_norm,
            "--limit",
            "500",
            "--json",
            "headRefName",
        ],
        cwd=repo,
    )
    if proc.returncode != 0:
        reason = _as_text(proc.stderr or proc.stdout or f"gh_pr_list_failed:{state_norm}")
        return (set(), reason)
    try:
        payload = json.loads(proc.stdout or "[]")
    except Exception:  # noqa: BLE001
        return (set(), f"gh_pr_list_invalid_json:{state_norm}")
    heads: set[str] = set()
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            head = _as_text(row.get("headRefName", ""))
            if head:
                heads.add(head)
    return (heads, "")


def _current_branch(repo: Path) -> str:
    proc = _git(repo, ["branch", "--show-current"])
    if proc.returncode != 0:
        return ""
    return _as_text(proc.stdout)


def _fetch_prune(repo: Path, remote: str) -> tuple[bool, str]:
    proc = _git(repo, ["fetch", remote, "--prune"])
    if proc.returncode == 0:
        return (True, "")
    return (False, _as_text(proc.stderr or proc.stdout or "fetch_prune_failed"))


def _worktree_prune(repo: Path) -> tuple[bool, int, str]:
    proc = _git(repo, ["worktree", "prune", "--verbose"])
    if proc.returncode != 0:
        return (False, 0, _as_text(proc.stderr or proc.stdout or "worktree_prune_failed"))
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    removed_count = sum(1 for line in lines if line.lower().startswith("removing "))
    return (True, removed_count, "")


def _parse_ref_rows(proc: subprocess.CompletedProcess[str]) -> list[tuple[str, int]]:
    if proc.returncode != 0:
        return []
    rows: list[tuple[str, int]] = []
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        name = _as_text(parts[0])
        try:
            ts = int(parts[1])
        except ValueError:
            ts = 0
        if name:
            rows.append((name, ts))
    return rows


def _is_prefix_match(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name.startswith(prefix) for prefix in prefixes)


def _age_days(now_ts: int, ref_ts: int) -> int:
    if ref_ts <= 0:
        return 10_000
    return max(0, int((now_ts - ref_ts) / 86400))


def _active_worktree_branch_paths(repo: Path) -> dict[str, set[Path]]:
    proc = _git(repo, ["worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        return {}
    branch_paths: dict[str, set[Path]] = {}
    current_path: Path | None = None
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            current_path = None
            continue
        if line.startswith("worktree "):
            raw_path = _as_text(line[len("worktree ") :])
            current_path = Path(raw_path).expanduser().resolve() if raw_path else None
            continue
        if not line.startswith("branch ") or current_path is None:
            continue
        value = _as_text(line[len("branch ") :])
        if value.startswith("refs/heads/"):
            value = value[len("refs/heads/") :]
        if value:
            branch_paths.setdefault(value, set()).add(current_path)
    return branch_paths


def _active_worktree_branches(repo: Path) -> set[str]:
    return set(_active_worktree_branch_paths(repo).keys())


def _is_recovery_branch(branch: str) -> bool:
    token = _as_text(branch)
    if not token:
        return False
    return bool(RECOVERY_BRANCH_PATTERN.search(token))


def _merged_into_base(repo: Path, branch_ref: str, base_ref: str) -> bool:
    proc = _git(repo, ["merge-base", "--is-ancestor", branch_ref, base_ref])
    return proc.returncode == 0


def _ref_commit_sha(repo: Path, ref: str) -> str:
    proc = _git(repo, ["rev-parse", "--verify", ref])
    if proc.returncode != 0:
        return ""
    return _as_text(proc.stdout)


def _archive_tag_name(*, branch: str, commit_sha: str, namespace: str) -> str:
    branch_token = re.sub(r"[^a-zA-Z0-9._-]+", "-", branch).strip("-")
    if not branch_token:
        branch_token = "branch"
    short_sha = _as_text(commit_sha)[:12]
    if not short_sha:
        short_sha = "unknown"
    ns = _as_text(namespace).strip("/")
    if not ns:
        ns = "archive/branch-debt"
    return f"{ns}/{branch_token}-{short_sha}"


def _ensure_archive_tag(
    repo: Path,
    *,
    tag_name: str,
    target_ref: str,
    remote: str,
    push_remote: bool,
) -> tuple[bool, str]:
    exists = _git(repo, ["rev-parse", "--verify", f"refs/tags/{tag_name}"])
    if exists.returncode != 0:
        create = _git(repo, ["tag", tag_name, target_ref])
        if create.returncode != 0:
            return (False, _as_text(create.stderr or create.stdout or "archive_tag_create_failed"))
    if push_remote:
        push = _git(repo, ["push", remote, f"refs/tags/{tag_name}:refs/tags/{tag_name}"])
        if push.returncode != 0:
            return (False, _as_text(push.stderr or push.stdout or "archive_tag_push_failed"))
    return (True, "")


def _delete_remote_branch(repo: Path, remote: str, branch: str) -> tuple[bool, str]:
    proc = _git(repo, ["push", remote, "--delete", branch])
    if proc.returncode == 0:
        return (True, "")
    return (False, _as_text(proc.stderr or proc.stdout or "remote_delete_failed"))


def _delete_remote_branches_batched(
    repo: Path,
    *,
    remote: str,
    branches: list[str],
    chunk_size: int = 40,
) -> tuple[set[str], dict[str, str]]:
    deleted: set[str] = set()
    failures: dict[str, str] = {}
    if not branches:
        return deleted, failures
    step = max(1, int(chunk_size))
    for start in range(0, len(branches), step):
        chunk = [item for item in branches[start : start + step] if _as_text(item)]
        if not chunk:
            continue
        proc = _git(repo, ["push", remote, "--delete", *chunk])
        if proc.returncode == 0:
            deleted.update(chunk)
            continue
        for branch in chunk:
            ok, reason = _delete_remote_branch(repo, remote, branch)
            if ok:
                deleted.add(branch)
            else:
                failures[branch] = reason
    return deleted, failures


def _delete_local_branch(repo: Path, branch: str) -> tuple[bool, str]:
    proc = _git(repo, ["branch", "-d", branch])
    if proc.returncode == 0:
        return (True, "")
    return (False, _as_text(proc.stderr or proc.stdout or "local_delete_failed"))


def _force_delete_local_branch(repo: Path, branch: str) -> tuple[bool, str]:
    proc = _git(repo, ["branch", "-D", branch])
    if proc.returncode == 0:
        return (True, "")
    return (False, _as_text(proc.stderr or proc.stdout or "local_force_delete_failed"))


def _extract_worktree_path(reason: str) -> Path | None:
    match = re.search(r"used by worktree at '([^']+)'", reason)
    if not match:
        return None
    raw = _as_text(match.group(1))
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _remove_worktree_if_clean(repo: Path, worktree_path: Path) -> tuple[bool, str]:
    repo_resolved = repo.resolve()
    if worktree_path == repo_resolved:
        return (False, "worktree_is_repo_root")

    def _remove_registered() -> tuple[bool, str]:
        proc = _git(repo, ["worktree", "remove", "--force", str(worktree_path)])
        if proc.returncode != 0:
            return (False, _as_text(proc.stderr or proc.stdout or "worktree_remove_failed"))
        return (True, "")

    if not worktree_path.exists() or not worktree_path.is_dir():
        return _remove_registered()

    status = _git(worktree_path, ["status", "--porcelain"])
    if status.returncode != 0:
        status_reason = _as_text(status.stderr or status.stdout or "worktree_status_failed")
        if "must be run in a work tree" in status_reason.lower():
            return _remove_registered()
        return (False, status_reason)
    if _as_text(status.stdout):
        return (False, "worktree_dirty")
    return _remove_registered()


def _inspect_worktree_dirty(worktree_path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "worktree_path": str(worktree_path),
        "exists": worktree_path.exists() and worktree_path.is_dir(),
        "status_ok": False,
        "status_error": "",
        "dirty_entry_count": 0,
        "dirty_sample": [],
    }
    if not info["exists"]:
        info["status_error"] = "worktree_missing"
        return info
    proc = _git(worktree_path, ["status", "--porcelain"])
    if proc.returncode != 0:
        info["status_error"] = _as_text(proc.stderr or proc.stdout or "worktree_status_failed")
        return info
    entries = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    info["status_ok"] = True
    info["dirty_entry_count"] = len(entries)
    info["dirty_sample"] = entries[:12]
    return info


def _retire_stale_recovery_worktrees(
    repo: Path,
    *,
    branch: str,
    age_days: int,
    min_age_days: int,
    worktree_paths_by_branch: dict[str, set[Path]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "eligible": False,
        "attempted_paths_count": 0,
        "removed_paths": [],
        "failed": [],
    }
    if age_days < max(1, int(min_age_days)):
        return result
    if not _is_recovery_branch(branch):
        return result
    paths = sorted(worktree_paths_by_branch.get(branch, set()), key=lambda item: str(item))
    if not paths:
        return result
    result["eligible"] = True
    result["attempted_paths_count"] = len(paths)
    removed_paths: list[Path] = []
    failed: list[dict[str, str]] = []
    for worktree_path in paths:
        removed, reason = _remove_worktree_if_clean(repo, worktree_path)
        if removed:
            removed_paths.append(worktree_path)
            continue
        failed.append({"path": str(worktree_path), "reason": _as_text(reason) or "worktree_remove_failed"})
    if removed_paths:
        remaining_paths = {path for path in worktree_paths_by_branch.get(branch, set()) if path not in set(removed_paths)}
        if remaining_paths:
            worktree_paths_by_branch[branch] = remaining_paths
        else:
            worktree_paths_by_branch.pop(branch, None)
    result["removed_paths"] = [str(path) for path in removed_paths]
    result["failed"] = failed
    return result


def _as_repo_path(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    return candidate


def remediate_repo(
    repo: Path,
    *,
    remote: str,
    base_ref: str,
    stale_days: int,
    prefixes: tuple[str, ...],
    protected_branches: set[str],
    max_remote_deletes: int,
    max_local_deletes: int,
    over_cap_total_branches: int,
    ignore_stale_age_when_over_cap: bool,
    remove_worktree_locks: bool,
    allow_closed_pr_delete: bool,
    archive_unmerged_branches: bool,
    archive_unmerged_min_age_days: int,
    archive_tag_namespace: str,
    archive_push_remote_tags: bool,
    retire_stale_recovery_worktrees: bool,
    recovery_worktree_min_age_days: int,
    apply: bool,
) -> dict[str, Any]:
    now_ts = int(datetime.now(UTC).timestamp())
    report: dict[str, Any] = {
        "repo_root": str(repo),
        "ok": False,
        "errors": [],
        "skipped": [],
        "fetch_prune_ok": False,
        "worktree_prune_ok": False,
        "worktree_prune_removed_count": 0,
        "worktree_remove_attempted_count": 0,
        "worktree_removed_count": 0,
        "worktree_remove_failed_count": 0,
        "worktree_removed_paths": [],
        "dirty_worktree_blockers": [],
        "repo_slug": "",
        "open_pr_head_count": 0,
        "closed_pr_head_count": 0,
        "remote_branch_total": 0,
        "remote_stale_prefix_count": 0,
        "remote_candidate_count": 0,
        "remote_blocked_open_pr_count": 0,
        "remote_blocked_unmerged_count": 0,
        "remote_deleted_count": 0,
        "remote_deleted": [],
        "local_branch_total": 0,
        "local_stale_prefix_count": 0,
        "local_candidate_count": 0,
        "local_blocked_unmerged_count": 0,
        "local_blocked_worktree_count": 0,
        "local_blocked_worktree_dirty_count": 0,
        "local_force_deleted_count": 0,
        "local_deleted_count": 0,
        "local_deleted": [],
        "over_cap_total_branches": max(1, int(over_cap_total_branches)),
        "ignore_stale_age_when_over_cap": bool(ignore_stale_age_when_over_cap),
        "over_cap_mode_active": False,
        "active_worktree_branch_count": 0,
        "archive_unmerged_branches": bool(archive_unmerged_branches),
        "archive_unmerged_min_age_days": max(1, int(archive_unmerged_min_age_days)),
        "archive_tag_namespace": _as_text(archive_tag_namespace).strip("/") or "archive/branch-debt",
        "archive_push_remote_tags": bool(archive_push_remote_tags),
        "archive_tagged_count": 0,
        "archive_tag_push_failed_count": 0,
        "remote_archived_unmerged_deleted_count": 0,
        "local_archived_unmerged_deleted_count": 0,
        "remote_blocked_active_worktree_count": 0,
        "local_blocked_active_worktree_count": 0,
        "retire_stale_recovery_worktrees": bool(retire_stale_recovery_worktrees),
        "recovery_worktree_min_age_days": max(1, int(recovery_worktree_min_age_days)),
        "recovery_worktree_retire_attempted_count": 0,
        "recovery_worktree_retired_count": 0,
        "recovery_worktree_retire_failed_count": 0,
        "recovery_worktree_retired_paths": [],
    }

    if not repo.exists() or not repo.is_dir():
        report["errors"].append("missing_repo")
        return report
    inside = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or _as_text(inside.stdout).lower() != "true":
        report["errors"].append("not_git_repo")
        return report

    fetch_ok, fetch_reason = _fetch_prune(repo, remote)
    report["fetch_prune_ok"] = fetch_ok
    if not fetch_ok and fetch_reason:
        report["errors"].append(f"fetch_prune_failed:{fetch_reason}")

    worktree_prune_ok, worktree_prune_removed_count, worktree_prune_reason = _worktree_prune(repo)
    report["worktree_prune_ok"] = worktree_prune_ok
    report["worktree_prune_removed_count"] = worktree_prune_removed_count
    if not worktree_prune_ok and worktree_prune_reason:
        report["errors"].append(f"worktree_prune_failed:{worktree_prune_reason}")

    remote_url = _git(repo, ["remote", "get-url", remote])
    if remote_url.returncode == 0:
        report["repo_slug"] = _parse_repo_slug(remote_url.stdout)
    else:
        report["errors"].append("remote_get_url_failed")

    open_heads, open_heads_reason = _load_pr_heads(repo, str(report["repo_slug"]), state="open")
    report["open_pr_head_count"] = len(open_heads)
    if open_heads_reason:
        report["errors"].append(f"open_pr_heads_unavailable:{open_heads_reason}")
    closed_heads: set[str] = set()
    if allow_closed_pr_delete:
        closed_heads, closed_heads_reason = _load_pr_heads(repo, str(report["repo_slug"]), state="closed")
        report["closed_pr_head_count"] = len(closed_heads)
        if closed_heads_reason:
            report["errors"].append(f"closed_pr_heads_unavailable:{closed_heads_reason}")

    remote_rows = _parse_ref_rows(
        _git(repo, ["for-each-ref", "--format=%(refname:short)|%(committerdate:unix)", f"refs/remotes/{remote}"])
    )
    local_rows = _parse_ref_rows(
        _git(repo, ["for-each-ref", "--format=%(refname:short)|%(committerdate:unix)", "refs/heads"])
    )
    report["remote_branch_total"] = len(remote_rows)
    report["local_branch_total"] = len(local_rows)
    total_branch_count = report["remote_branch_total"] + report["local_branch_total"]
    over_cap_mode_active = bool(ignore_stale_age_when_over_cap) and total_branch_count > max(1, int(over_cap_total_branches))
    report["over_cap_mode_active"] = over_cap_mode_active
    active_worktree_paths_by_branch = _active_worktree_branch_paths(repo)
    active_worktree_branches = set(active_worktree_paths_by_branch.keys())
    report["active_worktree_branch_count"] = len(active_worktree_branches)
    archive_tag_namespace_resolved = _as_text(report.get("archive_tag_namespace", "")).strip("/") or "archive/branch-debt"
    archive_unmerged_min_age_days_resolved = max(1, int(report.get("archive_unmerged_min_age_days", 1) or 1))

    current = _current_branch(repo)

    remote_deleted: list[dict[str, Any]] = []
    local_deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    archive_tagged_count = 0
    archive_tag_push_failed_count = 0
    remote_archived_unmerged_deleted_count = 0
    local_archived_unmerged_deleted_count = 0
    remote_stale_prefix_count = 0
    remote_candidates = 0
    remote_blocked_open_pr_count = 0
    remote_blocked_unmerged_count = 0
    remote_blocked_active_worktree_count = 0
    local_stale_prefix_count = 0
    local_candidates = 0
    local_blocked_unmerged_count = 0
    local_blocked_active_worktree_count = 0
    local_blocked_worktree_count = 0
    local_blocked_worktree_dirty_count = 0
    local_force_deleted_count = 0
    worktree_remove_attempted_count = 0
    worktree_removed_count = 0
    worktree_remove_failed_count = 0
    worktree_removed_paths: list[str] = []
    dirty_worktree_blockers: list[dict[str, Any]] = []
    recovery_worktree_retire_attempted_count = 0
    recovery_worktree_retired_count = 0
    recovery_worktree_retire_failed_count = 0
    recovery_worktree_retired_paths: list[str] = []
    recovery_retire_attempted_branches: set[str] = set()
    pending_remote_deletes: list[dict[str, Any]] = []

    for remote_ref, ts in remote_rows:
        if remote_ref in {f"{remote}/HEAD", remote}:
            continue
        if not remote_ref.startswith(f"{remote}/"):
            continue
        branch = remote_ref[len(f"{remote}/") :]
        if branch in protected_branches:
            continue
        if not _is_prefix_match(branch, prefixes):
            continue
        age_days = _age_days(now_ts, ts)
        if age_days < stale_days and not over_cap_mode_active:
            continue
        remote_stale_prefix_count += 1
        if branch in active_worktree_branches:
            if (
                apply
                and remove_worktree_locks
                and retire_stale_recovery_worktrees
                and branch not in recovery_retire_attempted_branches
            ):
                recovery_retire_attempted_branches.add(branch)
                retire_result = _retire_stale_recovery_worktrees(
                    repo,
                    branch=branch,
                    age_days=age_days,
                    min_age_days=max(1, int(recovery_worktree_min_age_days)),
                    worktree_paths_by_branch=active_worktree_paths_by_branch,
                )
                if retire_result.get("eligible"):
                    recovery_worktree_retire_attempted_count += int(retire_result.get("attempted_paths_count", 0) or 0)
                    retired_paths = [str(item) for item in retire_result.get("removed_paths", []) if _as_text(item)]
                    recovery_worktree_retired_count += len(retired_paths)
                    recovery_worktree_retired_paths.extend(retired_paths)
                    failed_rows = [row for row in retire_result.get("failed", []) if isinstance(row, dict)]
                    recovery_worktree_retire_failed_count += len(failed_rows)
                    for failed in failed_rows:
                        skipped.append(
                            {
                                "scope": "remote",
                                "branch": branch,
                                "reason": f"stale_recovery_worktree_retire_failed:{_as_text(failed.get('reason', 'worktree_remove_failed'))}",
                                "worktree_path": _as_text(failed.get("path", "")),
                            }
                        )
                    active_worktree_branches = set(active_worktree_paths_by_branch.keys())
            if branch in active_worktree_branches:
                remote_blocked_active_worktree_count += 1
                skipped.append({"scope": "remote", "branch": branch, "reason": "active_worktree_branch"})
                continue
        if branch in open_heads:
            remote_blocked_open_pr_count += 1
            skipped.append({"scope": "remote", "branch": branch, "reason": "open_pr_head"})
            continue
        merged = _merged_into_base(repo, f"refs/remotes/{remote}/{branch}", base_ref)
        closed_pr_head = branch in closed_heads if allow_closed_pr_delete else False
        archive_unmerged = (
            bool(archive_unmerged_branches)
            and over_cap_mode_active
            and age_days >= archive_unmerged_min_age_days_resolved
            and not merged
            and not closed_pr_head
        )
        if not merged and not closed_pr_head and not archive_unmerged:
            remote_blocked_unmerged_count += 1
            if bool(archive_unmerged_branches) and over_cap_mode_active and age_days < archive_unmerged_min_age_days_resolved:
                skipped.append(
                    {
                        "scope": "remote",
                        "branch": branch,
                        "reason": f"archive_unmerged_min_age_not_met:{age_days}<{archive_unmerged_min_age_days_resolved}",
                    }
                )
            else:
                skipped.append({"scope": "remote", "branch": branch, "reason": "not_merged_and_no_closed_pr"})
            continue
        remote_candidates += 1
        if len(pending_remote_deletes) >= max_remote_deletes:
            skipped.append({"scope": "remote", "branch": branch, "reason": "max_remote_deletes_reached"})
            continue
        if not apply:
            skipped.append({"scope": "remote", "branch": branch, "reason": "dry_run"})
            continue
        archive_tag = ""
        commit_sha = ""
        if archive_unmerged:
            commit_sha = _ref_commit_sha(repo, f"refs/remotes/{remote}/{branch}")
            if not commit_sha:
                skipped.append({"scope": "remote", "branch": branch, "reason": "archive_ref_sha_unavailable"})
                continue
            archive_tag = _archive_tag_name(
                branch=branch,
                commit_sha=commit_sha,
                namespace=archive_tag_namespace_resolved,
            )
            tag_ok, tag_reason = _ensure_archive_tag(
                repo,
                tag_name=archive_tag,
                target_ref=f"refs/remotes/{remote}/{branch}",
                remote=remote,
                push_remote=bool(archive_push_remote_tags),
            )
            if not tag_ok:
                archive_tag_push_failed_count += 1
                skipped.append({"scope": "remote", "branch": branch, "reason": f"archive_tag_failed:{tag_reason}"})
                continue
            archive_tagged_count += 1
        pending_remote_deletes.append(
            {
                "branch": branch,
                "age_days": age_days,
                "basis": "merged_into_base" if merged else ("closed_pr_head" if closed_pr_head else "archived_unmerged_no_pr"),
                "archive_unmerged": archive_unmerged,
                "archive_tag": archive_tag,
                "commit_sha": commit_sha,
            }
        )

    if apply and pending_remote_deletes:
        delete_order = [str(item.get("branch", "")).strip() for item in pending_remote_deletes if str(item.get("branch", "")).strip()]
        deleted_branches, delete_failures = _delete_remote_branches_batched(
            repo,
            remote=remote,
            branches=delete_order,
        )
        for item in pending_remote_deletes:
            branch_name = str(item.get("branch", "")).strip()
            if not branch_name:
                continue
            if branch_name in deleted_branches:
                entry = {
                    "branch": branch_name,
                    "age_days": int(item.get("age_days", 0) or 0),
                    "basis": str(item.get("basis", "merged_into_base")).strip() or "merged_into_base",
                }
                if bool(item.get("archive_unmerged", False)):
                    entry["archive_tag"] = str(item.get("archive_tag", "")).strip()
                    entry["commit_sha"] = str(item.get("commit_sha", "")).strip()
                    remote_archived_unmerged_deleted_count += 1
                remote_deleted.append(entry)
                continue
            reason = _as_text(delete_failures.get(branch_name, "remote_delete_failed"))
            skipped.append({"scope": "remote", "branch": branch_name, "reason": f"delete_failed:{reason}"})

    for local_ref, ts in local_rows:
        branch = local_ref
        if branch in protected_branches:
            continue
        if branch == current:
            continue
        if not _is_prefix_match(branch, prefixes):
            continue
        age_days = _age_days(now_ts, ts)
        if age_days < stale_days and not over_cap_mode_active:
            continue
        local_stale_prefix_count += 1
        if branch in active_worktree_branches:
            if (
                apply
                and remove_worktree_locks
                and retire_stale_recovery_worktrees
                and branch not in recovery_retire_attempted_branches
            ):
                recovery_retire_attempted_branches.add(branch)
                retire_result = _retire_stale_recovery_worktrees(
                    repo,
                    branch=branch,
                    age_days=age_days,
                    min_age_days=max(1, int(recovery_worktree_min_age_days)),
                    worktree_paths_by_branch=active_worktree_paths_by_branch,
                )
                if retire_result.get("eligible"):
                    recovery_worktree_retire_attempted_count += int(retire_result.get("attempted_paths_count", 0) or 0)
                    retired_paths = [str(item) for item in retire_result.get("removed_paths", []) if _as_text(item)]
                    recovery_worktree_retired_count += len(retired_paths)
                    recovery_worktree_retired_paths.extend(retired_paths)
                    failed_rows = [row for row in retire_result.get("failed", []) if isinstance(row, dict)]
                    recovery_worktree_retire_failed_count += len(failed_rows)
                    for failed in failed_rows:
                        skipped.append(
                            {
                                "scope": "local",
                                "branch": branch,
                                "reason": f"stale_recovery_worktree_retire_failed:{_as_text(failed.get('reason', 'worktree_remove_failed'))}",
                                "worktree_path": _as_text(failed.get("path", "")),
                            }
                        )
                    active_worktree_branches = set(active_worktree_paths_by_branch.keys())
            if branch in active_worktree_branches:
                local_blocked_active_worktree_count += 1
                skipped.append({"scope": "local", "branch": branch, "reason": "active_worktree_branch"})
                continue
        merged = _merged_into_base(repo, f"refs/heads/{branch}", base_ref)
        closed_pr_head = branch in closed_heads if allow_closed_pr_delete else False
        archive_unmerged = (
            bool(archive_unmerged_branches)
            and over_cap_mode_active
            and age_days >= archive_unmerged_min_age_days_resolved
            and not merged
            and not closed_pr_head
        )
        if not merged and not closed_pr_head and not archive_unmerged:
            local_blocked_unmerged_count += 1
            if bool(archive_unmerged_branches) and over_cap_mode_active and age_days < archive_unmerged_min_age_days_resolved:
                skipped.append(
                    {
                        "scope": "local",
                        "branch": branch,
                        "reason": f"archive_unmerged_min_age_not_met:{age_days}<{archive_unmerged_min_age_days_resolved}",
                    }
                )
            else:
                skipped.append({"scope": "local", "branch": branch, "reason": "not_merged_and_no_closed_pr"})
            continue
        local_candidates += 1
        if len(local_deleted) >= max_local_deletes:
            skipped.append({"scope": "local", "branch": branch, "reason": "max_local_deletes_reached"})
            continue
        if not apply:
            skipped.append({"scope": "local", "branch": branch, "reason": "dry_run"})
            continue
        archive_tag = ""
        commit_sha = ""
        if archive_unmerged:
            commit_sha = _ref_commit_sha(repo, f"refs/heads/{branch}")
            if not commit_sha:
                skipped.append({"scope": "local", "branch": branch, "reason": "archive_ref_sha_unavailable"})
                continue
            archive_tag = _archive_tag_name(
                branch=branch,
                commit_sha=commit_sha,
                namespace=archive_tag_namespace_resolved,
            )
            tag_ok, tag_reason = _ensure_archive_tag(
                repo,
                tag_name=archive_tag,
                target_ref=f"refs/heads/{branch}",
                remote=remote,
                push_remote=bool(archive_push_remote_tags),
            )
            if not tag_ok:
                archive_tag_push_failed_count += 1
                skipped.append({"scope": "local", "branch": branch, "reason": f"archive_tag_failed:{tag_reason}"})
                continue
            archive_tagged_count += 1
        deleted, reason = _delete_local_branch(repo, branch)
        if deleted:
            basis = "merged_into_base" if merged else ("closed_pr_head" if closed_pr_head else "archived_unmerged_no_pr")
            local_deleted.append(
                {
                    "branch": branch,
                    "age_days": age_days,
                    "basis": basis,
                    **({"archive_tag": archive_tag, "commit_sha": commit_sha} if archive_unmerged else {}),
                }
            )
            if archive_unmerged:
                local_archived_unmerged_deleted_count += 1
        else:
            if (
                apply
                and not merged
                and (closed_pr_head or archive_unmerged)
                and "is not fully merged" in reason
            ):
                force_deleted, force_reason = _force_delete_local_branch(repo, branch)
                if force_deleted:
                    local_force_deleted_count += 1
                    basis = "closed_pr_head_force_delete" if closed_pr_head else "archived_unmerged_no_pr_force_delete"
                    local_deleted.append(
                        {
                            "branch": branch,
                            "age_days": age_days,
                            "basis": basis,
                            **({"archive_tag": archive_tag, "commit_sha": commit_sha} if archive_unmerged else {}),
                        }
                    )
                    if archive_unmerged:
                        local_archived_unmerged_deleted_count += 1
                    continue
                reason = f"{reason};force_delete_failed:{force_reason}"
            worktree_path = _extract_worktree_path(reason)
            if apply and remove_worktree_locks and worktree_path is not None:
                worktree_remove_attempted_count += 1
                removed, remove_reason = _remove_worktree_if_clean(repo, worktree_path)
                if removed:
                    worktree_removed_count += 1
                    worktree_removed_paths.append(str(worktree_path))
                    deleted_retry, retry_reason = _delete_local_branch(repo, branch)
                    if deleted_retry:
                        basis = "merged_into_base" if merged else ("closed_pr_head" if closed_pr_head else "archived_unmerged_no_pr")
                        local_deleted.append(
                            {
                                "branch": branch,
                                "age_days": age_days,
                                "basis": basis,
                                "worktree_reconciled": True,
                                **({"archive_tag": archive_tag, "commit_sha": commit_sha} if archive_unmerged else {}),
                            }
                        )
                        if archive_unmerged:
                            local_archived_unmerged_deleted_count += 1
                        continue
                    reason = f"{reason};retry_failed:{retry_reason}"
                else:
                    worktree_remove_failed_count += 1
                    reason = f"{reason};worktree_remove_failed:{remove_reason}"
            if "used by worktree at" in reason:
                local_blocked_worktree_count += 1
            if "worktree_remove_failed:worktree_dirty" in reason:
                local_blocked_worktree_dirty_count += 1
                wt = _extract_worktree_path(reason)
                blocker: dict[str, Any] = {
                    "branch": branch,
                    "repo_root": str(repo),
                    "reason": "worktree_dirty",
                    "worktree_path": str(wt) if wt is not None else "",
                }
                if wt is not None:
                    blocker.update(_inspect_worktree_dirty(wt))
                dirty_worktree_blockers.append(blocker)
            skipped.append({"scope": "local", "branch": branch, "reason": f"delete_failed:{reason}"})

    report["remote_stale_prefix_count"] = remote_stale_prefix_count
    report["remote_candidate_count"] = remote_candidates
    report["remote_blocked_open_pr_count"] = remote_blocked_open_pr_count
    report["remote_blocked_unmerged_count"] = remote_blocked_unmerged_count
    report["remote_blocked_active_worktree_count"] = remote_blocked_active_worktree_count
    report["remote_deleted_count"] = len(remote_deleted)
    report["remote_deleted"] = remote_deleted
    report["remote_archived_unmerged_deleted_count"] = remote_archived_unmerged_deleted_count
    report["local_stale_prefix_count"] = local_stale_prefix_count
    report["local_candidate_count"] = local_candidates
    report["local_blocked_unmerged_count"] = local_blocked_unmerged_count
    report["local_blocked_active_worktree_count"] = local_blocked_active_worktree_count
    report["local_blocked_worktree_count"] = local_blocked_worktree_count
    report["local_blocked_worktree_dirty_count"] = local_blocked_worktree_dirty_count
    report["local_force_deleted_count"] = local_force_deleted_count
    report["local_deleted_count"] = len(local_deleted)
    report["local_deleted"] = local_deleted
    report["local_archived_unmerged_deleted_count"] = local_archived_unmerged_deleted_count
    report["archive_tagged_count"] = archive_tagged_count
    report["archive_tag_push_failed_count"] = archive_tag_push_failed_count
    report["recovery_worktree_retire_attempted_count"] = recovery_worktree_retire_attempted_count
    report["recovery_worktree_retired_count"] = recovery_worktree_retired_count
    report["recovery_worktree_retire_failed_count"] = recovery_worktree_retire_failed_count
    report["recovery_worktree_retired_paths"] = recovery_worktree_retired_paths
    report["worktree_remove_attempted_count"] = worktree_remove_attempted_count
    report["worktree_removed_count"] = worktree_removed_count
    report["worktree_remove_failed_count"] = worktree_remove_failed_count
    report["worktree_removed_paths"] = worktree_removed_paths
    report["dirty_worktree_blockers"] = dirty_worktree_blockers
    report["skipped"] = skipped
    report["ok"] = len(report["errors"]) == 0
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remediate stale merged git branches for hygiene.")
    parser.add_argument("--root", default=".", help="Workspace root used to resolve relative repo paths.")
    parser.add_argument("--repo", action="append", default=[], help="Target repo path (repeatable).")
    parser.add_argument("--remote", default="origin", help="Git remote name.")
    parser.add_argument("--base-ref", default="origin/main", help="Base branch ref used for merge checks.")
    parser.add_argument("--stale-days", type=int, default=7, help="Minimum branch age in days for remediation.")
    parser.add_argument("--branch-prefix", action="append", default=[], help="Eligible branch prefix (repeatable).")
    parser.add_argument("--protected-branch", action="append", default=[], help="Protected branch name (repeatable).")
    parser.add_argument("--max-remote-deletes", type=int, default=60, help="Maximum remote deletions per repo.")
    parser.add_argument("--max-local-deletes", type=int, default=60, help="Maximum local deletions per repo.")
    parser.add_argument(
        "--over-cap-total-branches",
        type=int,
        default=500,
        help="Activate over-cap remediation mode when local+remote branches exceed this value.",
    )
    parser.add_argument(
        "--ignore-stale-age-when-over-cap",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When in over-cap mode, allow merged/closed-PR branch cleanup regardless of stale-days age.",
    )
    parser.add_argument(
        "--remove-worktree-locks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When local deletion is blocked by a clean attached worktree, remove that worktree and retry deletion.",
    )
    parser.add_argument(
        "--allow-closed-pr-delete",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow deletion when branch is tied to a closed PR head (open PR heads remain protected).",
    )
    parser.add_argument(
        "--archive-unmerged-branches",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="When over-cap, archive unmerged/no-PR branches to tags before deletion.",
    )
    parser.add_argument(
        "--archive-unmerged-min-age-days",
        type=int,
        default=2,
        help="Minimum age in days before archive-delete for unmerged/no-PR branches when enabled.",
    )
    parser.add_argument(
        "--archive-tag-namespace",
        default="archive/branch-debt",
        help="Tag namespace prefix used for archive-delete backups.",
    )
    parser.add_argument(
        "--archive-push-remote-tags",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Push archive tags to remote before deleting archive-delete branches.",
    )
    parser.add_argument(
        "--retire-stale-recovery-worktrees",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Attempt clean removal of stale *recovery* worktrees before classifying branch as active-worktree-blocked.",
    )
    parser.add_argument(
        "--recovery-worktree-min-age-days",
        type=int,
        default=2,
        help="Minimum branch age in days before stale recovery worktree retirement is attempted.",
    )
    parser.add_argument("--apply", action=argparse.BooleanOptionalAction, default=False, help="Apply deletions.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output JSON report path.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    output = _as_repo_path(root, args.output)

    repo_inputs = [str(item) for item in (args.repo or []) if _as_text(item)]
    if not repo_inputs:
        repo_inputs = [str(root), str((root.parent / "orxaq").resolve())]
    repo_paths = [_as_repo_path(root, item) for item in repo_inputs]

    prefixes = tuple(_as_text(item) for item in (args.branch_prefix or []) if _as_text(item))
    if not prefixes:
        prefixes = DEFAULT_PREFIXES
    protected = set(DEFAULT_PROTECTED)
    protected.update({_as_text(item) for item in (args.protected_branch or []) if _as_text(item)})

    repo_reports: list[dict[str, Any]] = []
    for repo in repo_paths:
        repo_reports.append(
            remediate_repo(
                repo,
                remote=_as_text(args.remote) or "origin",
                base_ref=_as_text(args.base_ref) or "origin/main",
                stale_days=max(1, int(args.stale_days)),
                prefixes=prefixes,
                protected_branches=protected,
                max_remote_deletes=max(0, int(args.max_remote_deletes)),
                max_local_deletes=max(0, int(args.max_local_deletes)),
                over_cap_total_branches=max(1, int(args.over_cap_total_branches)),
                ignore_stale_age_when_over_cap=bool(args.ignore_stale_age_when_over_cap),
                remove_worktree_locks=bool(args.remove_worktree_locks),
                allow_closed_pr_delete=bool(args.allow_closed_pr_delete),
                archive_unmerged_branches=bool(args.archive_unmerged_branches),
                archive_unmerged_min_age_days=max(1, int(args.archive_unmerged_min_age_days)),
                archive_tag_namespace=_as_text(args.archive_tag_namespace) or "archive/branch-debt",
                archive_push_remote_tags=bool(args.archive_push_remote_tags),
                retire_stale_recovery_worktrees=bool(args.retire_stale_recovery_worktrees),
                recovery_worktree_min_age_days=max(1, int(args.recovery_worktree_min_age_days)),
                apply=bool(args.apply),
            )
        )

    summary = {
        "repo_count": len(repo_reports),
        "worktree_prune_removed_count": sum(int(row.get("worktree_prune_removed_count", 0) or 0) for row in repo_reports),
        "worktree_remove_attempted_count": sum(int(row.get("worktree_remove_attempted_count", 0) or 0) for row in repo_reports),
        "worktree_removed_count": sum(int(row.get("worktree_removed_count", 0) or 0) for row in repo_reports),
        "worktree_remove_failed_count": sum(int(row.get("worktree_remove_failed_count", 0) or 0) for row in repo_reports),
        "remote_stale_prefix_count": sum(int(row.get("remote_stale_prefix_count", 0) or 0) for row in repo_reports),
        "local_stale_prefix_count": sum(int(row.get("local_stale_prefix_count", 0) or 0) for row in repo_reports),
        "remote_deleted_count": sum(int(row.get("remote_deleted_count", 0) or 0) for row in repo_reports),
        "local_deleted_count": sum(int(row.get("local_deleted_count", 0) or 0) for row in repo_reports),
        "remote_archived_unmerged_deleted_count": sum(
            int(row.get("remote_archived_unmerged_deleted_count", 0) or 0) for row in repo_reports
        ),
        "local_archived_unmerged_deleted_count": sum(
            int(row.get("local_archived_unmerged_deleted_count", 0) or 0) for row in repo_reports
        ),
        "archive_tagged_count": sum(int(row.get("archive_tagged_count", 0) or 0) for row in repo_reports),
        "archive_tag_push_failed_count": sum(int(row.get("archive_tag_push_failed_count", 0) or 0) for row in repo_reports),
        "recovery_worktree_retire_attempted_count": sum(
            int(row.get("recovery_worktree_retire_attempted_count", 0) or 0) for row in repo_reports
        ),
        "recovery_worktree_retired_count": sum(
            int(row.get("recovery_worktree_retired_count", 0) or 0) for row in repo_reports
        ),
        "recovery_worktree_retire_failed_count": sum(
            int(row.get("recovery_worktree_retire_failed_count", 0) or 0) for row in repo_reports
        ),
        "remote_candidate_count": sum(int(row.get("remote_candidate_count", 0) or 0) for row in repo_reports),
        "local_candidate_count": sum(int(row.get("local_candidate_count", 0) or 0) for row in repo_reports),
        "remote_blocked_open_pr_count": sum(int(row.get("remote_blocked_open_pr_count", 0) or 0) for row in repo_reports),
        "remote_blocked_unmerged_count": sum(int(row.get("remote_blocked_unmerged_count", 0) or 0) for row in repo_reports),
        "remote_blocked_active_worktree_count": sum(
            int(row.get("remote_blocked_active_worktree_count", 0) or 0) for row in repo_reports
        ),
        "local_blocked_unmerged_count": sum(int(row.get("local_blocked_unmerged_count", 0) or 0) for row in repo_reports),
        "local_blocked_active_worktree_count": sum(
            int(row.get("local_blocked_active_worktree_count", 0) or 0) for row in repo_reports
        ),
        "local_blocked_worktree_count": sum(int(row.get("local_blocked_worktree_count", 0) or 0) for row in repo_reports),
        "local_blocked_worktree_dirty_count": sum(
            int(row.get("local_blocked_worktree_dirty_count", 0) or 0) for row in repo_reports
        ),
        "local_force_deleted_count": sum(int(row.get("local_force_deleted_count", 0) or 0) for row in repo_reports),
        "dirty_worktree_blocker_count": sum(
            len(row.get("dirty_worktree_blockers", []))
            for row in repo_reports
            if isinstance(row, dict) and isinstance(row.get("dirty_worktree_blockers"), list)
        ),
        "open_pr_head_count": sum(int(row.get("open_pr_head_count", 0) or 0) for row in repo_reports),
        "error_count": sum(len(row.get("errors", [])) for row in repo_reports if isinstance(row, dict)),
    }
    dirty_worktree_blockers: list[dict[str, Any]] = []
    for repo_report in repo_reports:
        if not isinstance(repo_report, dict):
            continue
        rows = repo_report.get("dirty_worktree_blockers", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            dirty_worktree_blockers.append(row)
    report = {
        "schema_version": "git-hygiene-remediation.v1",
        "generated_at_utc": _utc_now_iso(),
        "root_dir": str(root),
        "remote": _as_text(args.remote) or "origin",
        "base_ref": _as_text(args.base_ref) or "origin/main",
        "stale_days": max(1, int(args.stale_days)),
        "branch_prefixes": list(prefixes),
        "remove_worktree_locks": bool(args.remove_worktree_locks),
        "allow_closed_pr_delete": bool(args.allow_closed_pr_delete),
        "over_cap_total_branches": max(1, int(args.over_cap_total_branches)),
        "ignore_stale_age_when_over_cap": bool(args.ignore_stale_age_when_over_cap),
        "archive_unmerged_branches": bool(args.archive_unmerged_branches),
        "archive_unmerged_min_age_days": max(1, int(args.archive_unmerged_min_age_days)),
        "archive_tag_namespace": _as_text(args.archive_tag_namespace).strip("/") or "archive/branch-debt",
        "archive_push_remote_tags": bool(args.archive_push_remote_tags),
        "retire_stale_recovery_worktrees": bool(args.retire_stale_recovery_worktrees),
        "recovery_worktree_min_age_days": max(1, int(args.recovery_worktree_min_age_days)),
        "apply": bool(args.apply),
        "repos": repo_reports,
        "dirty_worktree_blockers": dirty_worktree_blockers[:100],
        "summary": summary,
        "ok": summary["error_count"] == 0,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            "git_hygiene_remediation "
            f"ok={report['ok']} repos={summary['repo_count']} "
            f"remote_deleted={summary['remote_deleted_count']} local_deleted={summary['local_deleted_count']} "
            f"output={output}"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
