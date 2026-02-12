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


def _merged_into_base(repo: Path, branch_ref: str, base_ref: str) -> bool:
    proc = _git(repo, ["merge-base", "--is-ancestor", branch_ref, base_ref])
    return proc.returncode == 0


def _delete_remote_branch(repo: Path, remote: str, branch: str) -> tuple[bool, str]:
    proc = _git(repo, ["push", remote, "--delete", branch])
    if proc.returncode == 0:
        return (True, "")
    return (False, _as_text(proc.stderr or proc.stdout or "remote_delete_failed"))


def _delete_local_branch(repo: Path, branch: str) -> tuple[bool, str]:
    proc = _git(repo, ["branch", "-d", branch])
    if proc.returncode == 0:
        return (True, "")
    return (False, _as_text(proc.stderr or proc.stdout or "local_delete_failed"))


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
    remove_worktree_locks: bool,
    allow_closed_pr_delete: bool,
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
        "local_deleted_count": 0,
        "local_deleted": [],
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

    current = _current_branch(repo)

    remote_deleted: list[dict[str, Any]] = []
    local_deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    remote_stale_prefix_count = 0
    remote_candidates = 0
    remote_blocked_open_pr_count = 0
    remote_blocked_unmerged_count = 0
    local_stale_prefix_count = 0
    local_candidates = 0
    local_blocked_unmerged_count = 0
    local_blocked_worktree_count = 0
    worktree_remove_attempted_count = 0
    worktree_removed_count = 0
    worktree_remove_failed_count = 0
    worktree_removed_paths: list[str] = []

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
        if age_days < stale_days:
            continue
        remote_stale_prefix_count += 1
        if branch in open_heads:
            remote_blocked_open_pr_count += 1
            skipped.append({"scope": "remote", "branch": branch, "reason": "open_pr_head"})
            continue
        merged = _merged_into_base(repo, f"refs/remotes/{remote}/{branch}", base_ref)
        closed_pr_head = branch in closed_heads if allow_closed_pr_delete else False
        if not merged and not closed_pr_head:
            remote_blocked_unmerged_count += 1
            skipped.append({"scope": "remote", "branch": branch, "reason": "not_merged_and_no_closed_pr"})
            continue
        remote_candidates += 1
        if len(remote_deleted) >= max_remote_deletes:
            skipped.append({"scope": "remote", "branch": branch, "reason": "max_remote_deletes_reached"})
            continue
        if not apply:
            skipped.append({"scope": "remote", "branch": branch, "reason": "dry_run"})
            continue
        deleted, reason = _delete_remote_branch(repo, remote, branch)
        if deleted:
            remote_deleted.append(
                {
                    "branch": branch,
                    "age_days": age_days,
                    "basis": "merged_into_base" if merged else "closed_pr_head",
                }
            )
        else:
            skipped.append({"scope": "remote", "branch": branch, "reason": f"delete_failed:{reason}"})

    for local_ref, ts in local_rows:
        branch = local_ref
        if branch in protected_branches:
            continue
        if branch == current:
            continue
        if not _is_prefix_match(branch, prefixes):
            continue
        age_days = _age_days(now_ts, ts)
        if age_days < stale_days:
            continue
        local_stale_prefix_count += 1
        merged = _merged_into_base(repo, f"refs/heads/{branch}", base_ref)
        closed_pr_head = branch in closed_heads if allow_closed_pr_delete else False
        if not merged and not closed_pr_head:
            local_blocked_unmerged_count += 1
            skipped.append({"scope": "local", "branch": branch, "reason": "not_merged_and_no_closed_pr"})
            continue
        local_candidates += 1
        if len(local_deleted) >= max_local_deletes:
            skipped.append({"scope": "local", "branch": branch, "reason": "max_local_deletes_reached"})
            continue
        if not apply:
            skipped.append({"scope": "local", "branch": branch, "reason": "dry_run"})
            continue
        deleted, reason = _delete_local_branch(repo, branch)
        if deleted:
            local_deleted.append(
                {
                    "branch": branch,
                    "age_days": age_days,
                    "basis": "merged_into_base" if merged else "closed_pr_head",
                }
            )
        else:
            worktree_path = _extract_worktree_path(reason)
            if apply and remove_worktree_locks and worktree_path is not None:
                worktree_remove_attempted_count += 1
                removed, remove_reason = _remove_worktree_if_clean(repo, worktree_path)
                if removed:
                    worktree_removed_count += 1
                    worktree_removed_paths.append(str(worktree_path))
                    deleted_retry, retry_reason = _delete_local_branch(repo, branch)
                    if deleted_retry:
                        local_deleted.append(
                            {
                                "branch": branch,
                                "age_days": age_days,
                                "basis": "merged_into_base" if merged else "closed_pr_head",
                                "worktree_reconciled": True,
                            }
                        )
                        continue
                    reason = f"{reason};retry_failed:{retry_reason}"
                else:
                    worktree_remove_failed_count += 1
                    reason = f"{reason};worktree_remove_failed:{remove_reason}"
            if "used by worktree at" in reason:
                local_blocked_worktree_count += 1
            skipped.append({"scope": "local", "branch": branch, "reason": f"delete_failed:{reason}"})

    report["remote_stale_prefix_count"] = remote_stale_prefix_count
    report["remote_candidate_count"] = remote_candidates
    report["remote_blocked_open_pr_count"] = remote_blocked_open_pr_count
    report["remote_blocked_unmerged_count"] = remote_blocked_unmerged_count
    report["remote_deleted_count"] = len(remote_deleted)
    report["remote_deleted"] = remote_deleted
    report["local_stale_prefix_count"] = local_stale_prefix_count
    report["local_candidate_count"] = local_candidates
    report["local_blocked_unmerged_count"] = local_blocked_unmerged_count
    report["local_blocked_worktree_count"] = local_blocked_worktree_count
    report["local_deleted_count"] = len(local_deleted)
    report["local_deleted"] = local_deleted
    report["worktree_remove_attempted_count"] = worktree_remove_attempted_count
    report["worktree_removed_count"] = worktree_removed_count
    report["worktree_remove_failed_count"] = worktree_remove_failed_count
    report["worktree_removed_paths"] = worktree_removed_paths
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
                remove_worktree_locks=bool(args.remove_worktree_locks),
                allow_closed_pr_delete=bool(args.allow_closed_pr_delete),
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
        "remote_candidate_count": sum(int(row.get("remote_candidate_count", 0) or 0) for row in repo_reports),
        "local_candidate_count": sum(int(row.get("local_candidate_count", 0) or 0) for row in repo_reports),
        "remote_blocked_open_pr_count": sum(int(row.get("remote_blocked_open_pr_count", 0) or 0) for row in repo_reports),
        "remote_blocked_unmerged_count": sum(int(row.get("remote_blocked_unmerged_count", 0) or 0) for row in repo_reports),
        "local_blocked_unmerged_count": sum(int(row.get("local_blocked_unmerged_count", 0) or 0) for row in repo_reports),
        "local_blocked_worktree_count": sum(int(row.get("local_blocked_worktree_count", 0) or 0) for row in repo_reports),
        "open_pr_head_count": sum(int(row.get("open_pr_head_count", 0) or 0) for row in repo_reports),
        "error_count": sum(len(row.get("errors", [])) for row in repo_reports if isinstance(row, dict)),
    }
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
        "apply": bool(args.apply),
        "repos": repo_reports,
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
