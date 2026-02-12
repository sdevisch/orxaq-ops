#!/usr/bin/env python3
"""Validate ticket/branch/PR workflow and contiguous change-block limits."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_POLICY = Path("config/git_delivery_policy.json")
DEFAULT_OUTPUT = Path("artifacts/autonomy/git_delivery_policy_health.json")
DEFAULT_BASELINE = Path("artifacts/autonomy/git_delivery_baseline.json")


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    return str(value).strip()


def _env_true(name: str) -> bool:
    return _as_bool(os.environ.get(name, ""), False)


def _resolve_path(root: Path, raw: Any, default: Path) -> Path:
    text = _as_text(raw)
    path = Path(text) if text else default
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )


def _git_cmd(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError("git executable not found in PATH")
    return _run([git_bin, *args], cwd=repo_root)


def _gh_cmd(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    gh_bin = shutil.which("gh")
    if not gh_bin:
        raise RuntimeError("gh executable not found in PATH")
    return _run([gh_bin, *args], cwd=repo_root)


def _load_actor_login(repo_root: Path) -> str:
    whoami = _gh_cmd(repo_root, ["api", "user", "--jq", ".login"])
    if whoami.returncode != 0:
        return ""
    return _as_text(whoami.stdout)


def _parse_numstat(stdout: str) -> tuple[int, int, int]:
    total = 0
    files = 0
    largest_file = 0
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_raw = parts[0].strip()
        del_raw = parts[1].strip()
        add_count = int(add_raw) if add_raw.isdigit() else 0
        del_count = int(del_raw) if del_raw.isdigit() else 0
        changed = add_count + del_count
        total += changed
        files += 1
        if changed > largest_file:
            largest_file = changed
    return (total, files, largest_file)


def _resolve_merge_base(repo_root: Path, base_ref: str) -> str:
    if not base_ref:
        return "HEAD"
    merge_base = _git_cmd(repo_root, ["merge-base", "HEAD", base_ref])
    if merge_base.returncode == 0 and merge_base.stdout.strip():
        return merge_base.stdout.strip()
    return base_ref


def load_git_facts(*, repo_root: Path, base_ref: str, include_working_tree_changes: bool) -> dict[str, Any]:
    branch_cmd = _git_cmd(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch_cmd.returncode != 0:
        raise RuntimeError(branch_cmd.stderr.strip() or branch_cmd.stdout.strip() or "unable to determine branch")
    branch = branch_cmd.stdout.strip()

    merge_base = _resolve_merge_base(repo_root, base_ref)
    committed = _git_cmd(repo_root, ["diff", "--numstat", f"{merge_base}...HEAD"])
    if committed.returncode != 0:
        committed = _git_cmd(repo_root, ["diff", "--numstat", "HEAD"])
    committed_total, committed_files, committed_largest = _parse_numstat(committed.stdout)

    staged_total = 0
    staged_files = 0
    staged_largest = 0
    working_total = 0
    working_files = 0
    working_largest = 0
    if include_working_tree_changes:
        staged = _git_cmd(repo_root, ["diff", "--numstat", "--cached"])
        staged_total, staged_files, staged_largest = _parse_numstat(staged.stdout if staged.returncode == 0 else "")
        working = _git_cmd(repo_root, ["diff", "--numstat"])
        working_total, working_files, working_largest = _parse_numstat(working.stdout if working.returncode == 0 else "")

    effective_total = committed_total + staged_total + working_total
    effective_files = committed_files + staged_files + working_files
    effective_largest = max(committed_largest, staged_largest, working_largest)

    return {
        "branch": branch,
        "base_ref": base_ref,
        "merge_base": merge_base,
        "committed_changed_lines": committed_total,
        "committed_changed_files": committed_files,
        "staged_changed_lines": staged_total,
        "staged_changed_files": staged_files,
        "working_tree_changed_lines": working_total,
        "working_tree_changed_files": working_files,
        "effective_changed_lines": effective_total,
        "effective_changed_files": effective_files,
        "largest_file_changed_lines": effective_largest,
    }


def load_baseline(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not payload:
        return {}
    if str(payload.get("schema_version", "")).strip() != "git-delivery-baseline.v1":
        return {}
    return payload


def resolve_policy_changed_lines(
    git_facts: dict[str, Any],
    *,
    use_baseline_delta: bool,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    raw_lines = _as_int(git_facts.get("effective_changed_lines", 0), 0)
    if not use_baseline_delta:
        return {
            "effective_changed_lines_policy": raw_lines,
            "effective_changed_lines_raw": raw_lines,
            "baseline_used": False,
            "baseline_effective_changed_lines": 0,
            "baseline_delta_lines": raw_lines,
        }
    baseline_lines = _as_int(baseline.get("effective_changed_lines", -1), -1)
    if baseline_lines < 0:
        return {
            "effective_changed_lines_policy": raw_lines,
            "effective_changed_lines_raw": raw_lines,
            "baseline_used": False,
            "baseline_effective_changed_lines": 0,
            "baseline_delta_lines": raw_lines,
        }
    policy_lines = max(0, raw_lines - baseline_lines)
    return {
        "effective_changed_lines_policy": policy_lines,
        "effective_changed_lines_raw": raw_lines,
        "baseline_used": True,
        "baseline_effective_changed_lines": baseline_lines,
        "baseline_delta_lines": policy_lines,
    }


def build_baseline_payload(
    *,
    repo_root: Path,
    policy_file: Path,
    base_ref: str,
    git_facts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "git-delivery-baseline.v1",
        "generated_at_utc": _utc_now_iso(),
        "repo_root": str(repo_root),
        "policy_file": str(policy_file),
        "base_ref": base_ref,
        "branch": _as_text(git_facts.get("branch", "")),
        "effective_changed_lines": _as_int(git_facts.get("effective_changed_lines", 0), 0),
        "effective_changed_files": _as_int(git_facts.get("effective_changed_files", 0), 0),
        "largest_file_changed_lines": _as_int(git_facts.get("largest_file_changed_lines", 0), 0),
    }


def load_pr_facts(*, repo_root: Path) -> dict[str, Any]:
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return {
            "available": False,
            "found": False,
            "error": "gh_not_installed",
            "number": 0,
            "state": "",
            "is_draft": False,
            "review_decision": "",
            "approvals": 0,
            "reviews_total": 0,
            "url": "",
        }
    view = _gh_cmd(
        repo_root,
        [
            "pr",
            "view",
            "--json",
            "number,state,isDraft,reviewDecision,reviews,url,author",
        ],
    )
    if view.returncode != 0:
        message = (view.stderr.strip() or view.stdout.strip() or "unable_to_query_pr").lower()
        found = "no pull requests found" not in message
        return {
            "available": True,
            "found": False if not found else False,
            "error": message,
            "number": 0,
            "state": "",
            "is_draft": False,
            "review_decision": "",
            "approvals": 0,
            "reviews_total": 0,
            "url": "",
        }
    try:
        payload = json.loads(view.stdout)
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict) or not payload:
        return {
            "available": True,
            "found": False,
            "error": "invalid_pr_payload",
            "number": 0,
            "state": "",
            "is_draft": False,
            "review_decision": "",
            "approvals": 0,
            "reviews_total": 0,
            "url": "",
        }

    reviews = payload.get("reviews", [])
    approvals = 0
    approver_keys: set[str] = set()
    reviews_total = 0
    if isinstance(reviews, list):
        for item in reviews:
            if not isinstance(item, dict):
                continue
            reviews_total += 1
            state = _as_text(item.get("state", "")).upper()
            if state != "APPROVED":
                continue
            author = item.get("author", {})
            if isinstance(author, dict):
                login = _as_text(author.get("login", ""))
            else:
                login = _as_text(author)
            key = login or f"approval_{approvals+1}"
            if key in approver_keys:
                continue
            approver_keys.add(key)
            approvals += 1

    return {
        "available": True,
        "found": True,
        "error": "",
        "number": _as_int(payload.get("number", 0), 0),
        "state": _as_text(payload.get("state", "")).upper(),
        "is_draft": _as_bool(payload.get("isDraft", False), False),
        "review_decision": _as_text(payload.get("reviewDecision", "")).upper(),
        "approvals": approvals,
        "reviews_total": reviews_total,
        "url": _as_text(payload.get("url", "")),
        "author_login": _as_text(
            (payload.get("author", {}) or {}).get("login", "")
            if isinstance(payload.get("author", {}), dict)
            else ""
        ),
        "actor_login": _load_actor_login(repo_root),
    }


def evaluate(
    *,
    policy: dict[str, Any],
    git_facts: dict[str, Any],
    pr_facts: dict[str, Any],
    change_block_override: bool,
    pr_override: bool,
) -> dict[str, Any]:
    branch_cfg = policy.get("branch", {}) if isinstance(policy.get("branch"), dict) else {}
    block_cfg = policy.get("change_block", {}) if isinstance(policy.get("change_block"), dict) else {}
    pr_cfg = policy.get("pull_request", {}) if isinstance(policy.get("pull_request"), dict) else {}

    branch = _as_text(git_facts.get("branch", ""))
    effective_changed_lines_raw = _as_int(git_facts.get("effective_changed_lines", 0), 0)
    effective_changed_lines = _as_int(git_facts.get("effective_changed_lines_policy", effective_changed_lines_raw), 0)
    largest_file_changed_lines = _as_int(git_facts.get("largest_file_changed_lines", 0), 0)
    effective_changed_files = _as_int(git_facts.get("effective_changed_files", 0), 0)
    baseline_used = _as_bool(git_facts.get("baseline_used", False), False)
    baseline_effective_changed_lines = _as_int(git_facts.get("baseline_effective_changed_lines", 0), 0)
    baseline_delta_lines = _as_int(git_facts.get("baseline_delta_lines", effective_changed_lines), effective_changed_lines)

    require_ticket_branch = _as_bool(branch_cfg.get("require_ticket_branch", True), True)
    ticket_regex = _as_text(
        branch_cfg.get("ticket_branch_regex", "^(codex|claude|gemini|agent)/issue-[0-9]+-[a-z0-9._-]+$")
    )
    ticket_pattern = re.compile(ticket_regex)
    ticket_branch_match = bool(ticket_pattern.match(branch))

    enforce_max_changed_lines = _as_bool(block_cfg.get("enforce_max_changed_lines", True), True)
    max_changed_lines = max(1, _as_int(block_cfg.get("max_changed_lines", 400), 400))
    exceeds_change_block_limit = effective_changed_lines > max_changed_lines

    require_pr = _as_bool(pr_cfg.get("require_pr", True), True)
    require_open_pr = _as_bool(pr_cfg.get("require_open", True), True)
    allow_draft = _as_bool(pr_cfg.get("allow_draft", False), False)
    require_review = _as_bool(pr_cfg.get("require_review", True), True)
    required_approvals = max(0, _as_int(pr_cfg.get("required_approvals", 1), 1))
    require_review_decision_approved = _as_bool(pr_cfg.get("require_review_decision_approved", True), True)

    pr_available = _as_bool(pr_facts.get("available", False), False)
    pr_found = _as_bool(pr_facts.get("found", False), False)
    pr_state = _as_text(pr_facts.get("state", "")).upper()
    pr_is_draft = _as_bool(pr_facts.get("is_draft", False), False)
    pr_review_decision = _as_text(pr_facts.get("review_decision", "")).upper()
    pr_approvals = _as_int(pr_facts.get("approvals", 0), 0)
    pr_reviews_total = _as_int(pr_facts.get("reviews_total", 0), 0)
    working_tree_changed_lines = _as_int(git_facts.get("working_tree_changed_lines", 0), 0)
    pr_author_login = _as_text(pr_facts.get("author_login", "")).lower()
    pr_actor_login = _as_text(pr_facts.get("actor_login", "")).lower()
    self_authored_pr = bool(pr_author_login and pr_actor_login and pr_author_login == pr_actor_login)

    allow_unapproved_when_dirty = _as_bool(
        pr_cfg.get("allow_unapproved_pr_when_working_tree_dirty", False),
        False,
    )
    allow_unreviewed_when_dirty = _as_bool(
        pr_cfg.get("allow_unreviewed_pr_when_working_tree_dirty", False),
        False,
    )
    allow_self_authored_approval_waiver = _as_bool(
        pr_cfg.get("allow_self_authored_approval_waiver", False),
        False,
    )
    dirty_threshold = max(1, _as_int(pr_cfg.get("working_tree_dirty_threshold_lines", 1), 1))
    pr_dirty_waiver_active = (
        working_tree_changed_lines >= dirty_threshold and (allow_unapproved_when_dirty or allow_unreviewed_when_dirty)
    )
    pr_self_approval_waiver_active = self_authored_pr and allow_self_authored_approval_waiver

    violations: list[dict[str, Any]] = []
    warnings: list[str] = []
    if require_ticket_branch and not ticket_branch_match:
        violations.append(
            {
                "type": "ticket_branch_required",
                "message": "Branch must be ticket-linked (for example `codex/issue-123-topic`).",
                "branch": branch,
            }
        )

    if enforce_max_changed_lines and exceeds_change_block_limit and not change_block_override:
        violations.append(
            {
                "type": "change_block_too_large",
                "message": "Contiguous change block exceeds configured limit.",
                "effective_changed_lines": effective_changed_lines,
                "max_changed_lines": max_changed_lines,
            }
        )

    if require_pr and not pr_override:
        if not pr_available:
            violations.append(
                {
                    "type": "pr_evidence_unavailable",
                    "message": "GitHub CLI is unavailable; cannot verify PR workflow evidence.",
                    "error": _as_text(pr_facts.get("error", "")),
                }
            )
        elif not pr_found:
            violations.append(
                {
                    "type": "pr_missing_for_branch",
                    "message": "No pull request detected for current branch.",
                    "error": _as_text(pr_facts.get("error", "")),
                }
            )
        else:
            if require_open_pr and pr_state != "OPEN":
                violations.append(
                    {
                        "type": "pr_not_open",
                        "message": "Pull request must remain open until review/approval workflow completes.",
                        "state": pr_state,
                    }
                )
            if not allow_draft and pr_is_draft:
                violations.append(
                    {
                        "type": "pr_is_draft",
                        "message": "Draft pull requests are not allowed for delivery blocks under this policy.",
                    }
                )
            if require_review and pr_reviews_total <= 0:
                if pr_self_approval_waiver_active:
                    warnings.append("pr_review_missing_suppressed_self_authored_pr")
                elif allow_unreviewed_when_dirty and working_tree_changed_lines >= dirty_threshold:
                    warnings.append(
                        "pr_review_missing_suppressed_dirty_worktree:"
                        f"working_tree_changed_lines={working_tree_changed_lines}"
                    )
                else:
                    violations.append(
                        {
                            "type": "pr_review_missing",
                            "message": "Pull request must include review activity.",
                        }
                    )
            if require_review and pr_approvals < required_approvals:
                if pr_self_approval_waiver_active:
                    warnings.append(
                        "pr_approval_threshold_suppressed_self_authored_pr:"
                        f"approvals={pr_approvals}/{required_approvals}"
                    )
                elif allow_unapproved_when_dirty and working_tree_changed_lines >= dirty_threshold:
                    warnings.append(
                        "pr_approval_threshold_suppressed_dirty_worktree:"
                        f"approvals={pr_approvals}/{required_approvals};"
                        f"working_tree_changed_lines={working_tree_changed_lines}"
                    )
                else:
                    violations.append(
                        {
                            "type": "pr_approval_threshold_not_met",
                            "message": "Pull request does not meet approval threshold.",
                            "approvals": pr_approvals,
                            "required_approvals": required_approvals,
                        }
                    )
            if require_review_decision_approved and pr_review_decision != "APPROVED":
                if pr_self_approval_waiver_active:
                    warnings.append(
                        "pr_review_decision_suppressed_self_authored_pr:"
                        f"review_decision={pr_review_decision or 'none'}"
                    )
                elif allow_unapproved_when_dirty and working_tree_changed_lines >= dirty_threshold:
                    warnings.append(
                        "pr_review_decision_suppressed_dirty_worktree:"
                        f"review_decision={pr_review_decision or 'none'};"
                        f"working_tree_changed_lines={working_tree_changed_lines}"
                    )
                else:
                    violations.append(
                        {
                            "type": "pr_review_decision_not_approved",
                            "message": "Pull request review decision is not APPROVED.",
                            "review_decision": pr_review_decision,
                        }
                    )

    ok = len(violations) == 0
    return {
        "schema_version": "git-delivery-policy-health.v1",
        "generated_at_utc": _utc_now_iso(),
        "ok": ok,
        "summary": {
            "branch": branch,
            "ticket_branch_match": ticket_branch_match,
            "effective_changed_lines": effective_changed_lines,
            "effective_changed_lines_raw": effective_changed_lines_raw,
            "effective_changed_lines_delta": baseline_delta_lines,
            "baseline_used": baseline_used,
            "baseline_effective_changed_lines": baseline_effective_changed_lines,
            "effective_changed_files": effective_changed_files,
            "largest_file_changed_lines": largest_file_changed_lines,
            "max_changed_lines": max_changed_lines,
            "change_block_limit_exceeded": exceeds_change_block_limit,
            "change_block_override_used": bool(change_block_override),
            "pr_override_used": bool(pr_override),
            "pr_available": pr_available,
            "pr_found": pr_found,
            "pr_number": _as_int(pr_facts.get("number", 0), 0),
            "pr_state": pr_state,
            "pr_is_draft": pr_is_draft,
            "pr_review_decision": pr_review_decision,
            "pr_reviews_total": pr_reviews_total,
            "pr_approvals": pr_approvals,
            "working_tree_changed_lines": working_tree_changed_lines,
            "pr_dirty_waiver_active": pr_dirty_waiver_active,
            "pr_self_approval_waiver_active": pr_self_approval_waiver_active,
            "pr_author_login": pr_author_login,
            "pr_actor_login": pr_actor_login,
            "violation_count": len(violations),
            "warning_count": len(warnings),
        },
        "violations": violations,
        "warnings": warnings,
        "git_facts": git_facts,
        "pr_facts": pr_facts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check git delivery policy for tickets, branches, and PR workflow.")
    parser.add_argument("--root", default=".", help="Workspace root used to resolve paths.")
    parser.add_argument("--repo-root", default=".", help="Git repository root to inspect.")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY), help="Git delivery policy JSON.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output JSON report.")
    parser.add_argument(
        "--base-ref",
        default="",
        help="Base reference for committed diff sizing (defaults to policy monitoring.base_ref).",
    )
    parser.add_argument(
        "--baseline-file",
        default="",
        help="Optional baseline JSON path used for delta-based change block enforcement.",
    )
    parser.add_argument(
        "--capture-baseline",
        action="store_true",
        help="Capture current git facts into baseline file before evaluation.",
    )
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on policy violations.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    repo_root = _resolve_path(root, args.repo_root, Path("."))
    policy_file = _resolve_path(root, args.policy_file, DEFAULT_POLICY)
    output = _resolve_path(root, args.output, DEFAULT_OUTPUT)
    output.parent.mkdir(parents=True, exist_ok=True)

    policy = _load_json(policy_file)
    monitoring = policy.get("monitoring", {}) if isinstance(policy.get("monitoring"), dict) else {}
    base_ref = _as_text(args.base_ref) or _as_text(monitoring.get("base_ref", "origin/main")) or "origin/main"
    include_working_tree_changes = _as_bool(monitoring.get("include_working_tree_changes", True), True)
    baseline_file = _resolve_path(
        root,
        _as_text(args.baseline_file) or _as_text(monitoring.get("baseline_file", "")),
        DEFAULT_BASELINE,
    )
    use_baseline_delta = _as_bool(monitoring.get("use_baseline_delta", True), True)
    allow_missing_baseline = _as_bool(monitoring.get("allow_missing_baseline", True), True)

    block_cfg = policy.get("change_block", {}) if isinstance(policy.get("change_block"), dict) else {}
    pr_cfg = policy.get("pull_request", {}) if isinstance(policy.get("pull_request"), dict) else {}
    change_block_override_env = _as_text(block_cfg.get("allow_env_override", "ORXAQ_ALLOW_LARGE_CHANGE_BLOCK"))
    pr_override_env = _as_text(pr_cfg.get("allow_env_override", "ORXAQ_ALLOW_PR_WORKFLOW_BYPASS"))
    change_block_override = _env_true(change_block_override_env)
    pr_override = _env_true(pr_override_env)

    git_facts = load_git_facts(
        repo_root=repo_root,
        base_ref=base_ref,
        include_working_tree_changes=include_working_tree_changes,
    )
    if args.capture_baseline:
        baseline_payload = build_baseline_payload(
            repo_root=repo_root,
            policy_file=policy_file,
            base_ref=base_ref,
            git_facts=git_facts,
        )
        _save_json(baseline_file, baseline_payload)

    baseline_payload = load_baseline(baseline_file)
    baseline_found = bool(baseline_payload)
    baseline_resolution = resolve_policy_changed_lines(
        git_facts,
        use_baseline_delta=use_baseline_delta and (baseline_found or allow_missing_baseline),
        baseline=baseline_payload,
    )
    git_facts["effective_changed_lines_policy"] = _as_int(
        baseline_resolution.get("effective_changed_lines_policy", git_facts.get("effective_changed_lines", 0)),
        _as_int(git_facts.get("effective_changed_lines", 0), 0),
    )
    git_facts["effective_changed_lines_raw"] = _as_int(
        baseline_resolution.get("effective_changed_lines_raw", git_facts.get("effective_changed_lines", 0)),
        _as_int(git_facts.get("effective_changed_lines", 0), 0),
    )
    git_facts["baseline_used"] = _as_bool(baseline_resolution.get("baseline_used", False), False)
    git_facts["baseline_effective_changed_lines"] = _as_int(
        baseline_resolution.get("baseline_effective_changed_lines", 0),
        0,
    )
    git_facts["baseline_delta_lines"] = _as_int(
        baseline_resolution.get("baseline_delta_lines", git_facts.get("effective_changed_lines_policy", 0)),
        _as_int(git_facts.get("effective_changed_lines_policy", 0), 0),
    )

    pr_facts = load_pr_facts(repo_root=repo_root)

    report = evaluate(
        policy=policy,
        git_facts=git_facts,
        pr_facts=pr_facts,
        change_block_override=change_block_override,
        pr_override=pr_override,
    )
    report["policy_file"] = str(policy_file)
    report["repo_root"] = str(repo_root)
    report["base_ref"] = base_ref
    report["baseline_file"] = str(baseline_file)
    report["baseline_found"] = baseline_found
    report["baseline_capture_used"] = bool(args.capture_baseline)
    report["baseline_config"] = {
        "use_baseline_delta": use_baseline_delta,
        "allow_missing_baseline": allow_missing_baseline,
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        status = "PASS" if report.get("ok", False) else "FAIL"
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        print(
            f"{status} git_delivery_policy branch={summary.get('branch', '')} "
            f"changed_lines={summary.get('effective_changed_lines', 0)} "
            f"violations={summary.get('violation_count', 0)} output={output}"
        )
    if args.strict and not _as_bool(report.get("ok", False), False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
