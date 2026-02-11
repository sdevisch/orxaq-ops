"""GitHub PR automation helpers for autonomy workflows."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


SUCCESS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE"}
PENDING_STATUSES = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING"}


class GitOpsError(RuntimeError):
    """Raised when gitops automation cannot proceed safely."""


@dataclass
class PrStatus:
    number: int
    url: str
    state: str
    merge_state_status: str
    checks_passed: bool
    failed_checks: list[str]
    pending_checks: list[str]


def detect_repo(root: Path) -> str:
    cp = _run(
        ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
        cwd=root,
    )
    if cp.returncode != 0:
        raise GitOpsError("Unable to detect repository remote; pass --repo explicitly.")
    remote = cp.stdout.strip()
    if not remote:
        raise GitOpsError("Repository remote URL is empty; pass --repo explicitly.")
    remote = remote.removesuffix(".git")
    https_match = re.search(r"github\.com[:/](?P<repo>[^/]+/[^/]+)$", remote)
    if not https_match:
        raise GitOpsError("Unsupported remote URL format for GitHub repository detection.")
    return https_match.group("repo")


def detect_head_branch(root: Path) -> str:
    cp = _run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if cp.returncode != 0:
        raise GitOpsError("Unable to detect current branch; pass --head explicitly.")
    branch = cp.stdout.strip()
    if not branch:
        raise GitOpsError("Current branch is empty; pass --head explicitly.")
    return branch


def open_pr(
    *,
    repo: str,
    base: str,
    head: str,
    title: str,
    body: str,
    draft: bool,
) -> dict[str, Any]:
    if _gh_available():
        argv = [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            argv.append("--draft")
        cp = _run(argv)
        if cp.returncode == 0:
            url = _extract_url(cp.stdout)
            return {
                "ok": True,
                "source": "gh",
                "repo": repo,
                "base": base,
                "head": head,
                "number": _extract_pr_number(url),
                "url": url,
            }
    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
        "draft": draft,
    }
    data = _api_request("POST", repo, "/pulls", payload)
    return {
        "ok": True,
        "source": "api",
        "repo": repo,
        "base": base,
        "head": head,
        "number": int(data["number"]),
        "url": str(data["html_url"]),
    }


def get_pr_status(repo: str, pr_number: int) -> PrStatus:
    payload: dict[str, Any]
    if _gh_available():
        cp = _run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "number,state,mergeStateStatus,statusCheckRollup,url",
            ]
        )
        if cp.returncode == 0:
            payload = json.loads(cp.stdout)
            checks = _parse_rollup(payload.get("statusCheckRollup", []))
            return PrStatus(
                number=int(payload["number"]),
                url=str(payload["url"]),
                state=str(payload.get("state", "")),
                merge_state_status=str(payload.get("mergeStateStatus", "")),
                checks_passed=checks["checks_passed"],
                failed_checks=checks["failed_checks"],
                pending_checks=checks["pending_checks"],
            )
    pr_data = _api_request("GET", repo, f"/pulls/{pr_number}")
    commit_status = _api_request("GET", repo, f"/commits/{pr_data['head']['sha']}/status")
    failed_checks: list[str] = []
    pending_checks: list[str] = []
    for status in commit_status.get("statuses", []):
        name = str(status.get("context") or "unknown")
        state = str(status.get("state") or "").upper()
        if state == "SUCCESS":
            continue
        if state in {"PENDING", ""}:
            pending_checks.append(name)
            continue
        failed_checks.append(name)
    checks_passed = not failed_checks and not pending_checks
    return PrStatus(
        number=int(pr_data["number"]),
        url=str(pr_data["html_url"]),
        state=str(pr_data.get("state", "")).upper(),
        merge_state_status=str(pr_data.get("mergeable_state", "")).upper(),
        checks_passed=checks_passed,
        failed_checks=failed_checks,
        pending_checks=pending_checks,
    )


def wait_for_pr(
    *,
    repo: str,
    pr_number: int,
    interval_sec: int,
    max_attempts: int,
    failure_threshold: int,
    close_on_failure: bool,
    open_issue_on_failure: bool,
) -> dict[str, Any]:
    failure_streak = 0
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        status = get_pr_status(repo, pr_number)
        if status.state != "OPEN":
            return {
                "ok": False,
                "reason": f"PR state is {status.state}",
                "attempts": attempt,
                "status": _status_payload(status),
            }
        if status.failed_checks:
            failure_streak += 1
        else:
            failure_streak = 0
        if status.checks_passed:
            return {
                "ok": True,
                "reason": "All checks passed.",
                "attempts": attempt,
                "status": _status_payload(status),
            }
        if failure_streak >= max(1, failure_threshold):
            reason = (
                f"Repeated CI failures ({failure_streak} consecutive attempts): "
                f"{', '.join(status.failed_checks[:8])}"
            )
            _close_and_issue_if_configured(
                repo=repo,
                pr_number=pr_number,
                close_on_failure=close_on_failure,
                open_issue_on_failure=open_issue_on_failure,
                reason=reason,
            )
            return {
                "ok": False,
                "reason": reason,
                "attempts": attempt,
                "status": _status_payload(status),
            }
        if attempt < attempts:
            time.sleep(max(1, interval_sec))
    status = get_pr_status(repo, pr_number)
    reason = "Timed out waiting for CI checks to pass."
    _close_and_issue_if_configured(
        repo=repo,
        pr_number=pr_number,
        close_on_failure=close_on_failure,
        open_issue_on_failure=open_issue_on_failure,
        reason=reason,
    )
    return {"ok": False, "reason": reason, "attempts": attempts, "status": _status_payload(status)}


def merge_pr(
    *,
    repo: str,
    pr_number: int,
    method: str,
    delete_branch: bool,
    min_swarm_health: float,
    swarm_health_score: float | None,
    require_ci_green: bool,
) -> dict[str, Any]:
    status = get_pr_status(repo, pr_number)
    if status.state != "OPEN":
        raise GitOpsError(f"Cannot merge PR in state {status.state}.")
    if require_ci_green and not status.checks_passed:
        details = []
        if status.failed_checks:
            details.append(f"failed checks: {', '.join(status.failed_checks[:8])}")
        if status.pending_checks:
            details.append(f"pending checks: {', '.join(status.pending_checks[:8])}")
        raise GitOpsError("CI not green; " + "; ".join(details))
    if swarm_health_score is None:
        raise GitOpsError("Swarm health score is required for merge policy enforcement.")
    if swarm_health_score < min_swarm_health:
        raise GitOpsError(
            f"Swarm health score {swarm_health_score:.1f} below minimum {min_swarm_health:.1f}."
        )
    if method not in {"merge", "squash", "rebase"}:
        raise GitOpsError("Invalid merge method; choose merge, squash, or rebase.")

    if _gh_available():
        argv = [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            repo,
            f"--{method}",
        ]
        if delete_branch:
            argv.append("--delete-branch")
        cp = _run(argv)
        if cp.returncode == 0:
            return {
                "ok": True,
                "source": "gh",
                "repo": repo,
                "pr_number": pr_number,
                "method": method,
                "delete_branch": delete_branch,
                "swarm_health_score": swarm_health_score,
            }
    _api_request("PUT", repo, f"/pulls/{pr_number}/merge", {"merge_method": method})
    return {
        "ok": True,
        "source": "api",
        "repo": repo,
        "pr_number": pr_number,
        "method": method,
        "delete_branch": delete_branch,
        "swarm_health_score": swarm_health_score,
    }


def read_swarm_health_score(path: Path) -> float:
    if not path.exists():
        raise GitOpsError(f"Swarm health file missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GitOpsError(f"Invalid swarm health JSON: {path}") from exc
    for key in ("score", "health_score"):
        if key in payload:
            return float(payload[key])
    summary = payload.get("summary")
    if isinstance(summary, dict) and "score" in summary:
        return float(summary["score"])
    raise GitOpsError("Swarm health score not found in JSON payload.")


def close_pr(repo: str, pr_number: int, reason: str) -> None:
    if _gh_available():
        cp = _run(
            [
                "gh",
                "pr",
                "close",
                str(pr_number),
                "--repo",
                repo,
                "--comment",
                reason,
            ]
        )
        if cp.returncode == 0:
            return
    _api_request("PATCH", repo, f"/pulls/{pr_number}", {"state": "closed"})


def open_issue(repo: str, title: str, body: str) -> dict[str, Any]:
    if _gh_available():
        cp = _run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        if cp.returncode == 0:
            url = _extract_url(cp.stdout)
            return {"source": "gh", "url": url}
    data = _api_request("POST", repo, "/issues", {"title": title, "body": body})
    return {"source": "api", "url": str(data["html_url"])}


def _close_and_issue_if_configured(
    *,
    repo: str,
    pr_number: int,
    close_on_failure: bool,
    open_issue_on_failure: bool,
    reason: str,
) -> None:
    if close_on_failure:
        close_pr(repo, pr_number, reason)
    if open_issue_on_failure:
        open_issue(
            repo,
            title=f"AUTONOMY: PR #{pr_number} stopped after repeated CI failures",
            body=(
                "Autonomous PR pipeline halted.\n\n"
                f"- PR: #{pr_number}\n"
                f"- Reason: {reason}\n"
                "- Action: inspect failing checks and create smallest viable fix."
            ),
        )


def _status_payload(status: PrStatus) -> dict[str, Any]:
    return {
        "number": status.number,
        "url": status.url,
        "state": status.state,
        "merge_state_status": status.merge_state_status,
        "checks_passed": status.checks_passed,
        "failed_checks": status.failed_checks,
        "pending_checks": status.pending_checks,
    }


def _parse_rollup(items: list[dict[str, Any]]) -> dict[str, Any]:
    failed_checks: list[str] = []
    pending_checks: list[str] = []
    for item in items:
        name = str(item.get("name") or item.get("context") or "unknown")
        status = str(item.get("status") or "").upper()
        conclusion = str(item.get("conclusion") or "").upper()
        if conclusion in FAILED_CONCLUSIONS:
            failed_checks.append(name)
            continue
        if conclusion in SUCCESS_CONCLUSIONS:
            continue
        if status and status not in {"COMPLETED", "DONE"}:
            pending_checks.append(name)
            continue
        if conclusion in PENDING_STATUSES or not conclusion:
            pending_checks.append(name)
    return {
        "checks_passed": not failed_checks and not pending_checks,
        "failed_checks": failed_checks,
        "pending_checks": pending_checks,
    }


def _extract_url(text: str) -> str:
    match = re.search(r"https://github\.com/[^\s]+", text)
    if not match:
        raise GitOpsError("Unable to parse GitHub URL from command output.")
    return match.group(0).rstrip(")")


def _extract_pr_number(url: str) -> int:
    match = re.search(r"/pull/(\d+)$", url.rstrip("/"))
    if not match:
        raise GitOpsError("Unable to parse PR number from URL.")
    return int(match.group(1))


def _api_request(method: str, repo: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise GitOpsError("GitHub token missing for API fallback (set GITHUB_TOKEN or GH_TOKEN).")
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=f"https://api.github.com/repos/{repo}{path}",
        method=method,
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "orxaq-autonomy-gitops",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8") or "{}"
            return json.loads(text)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:800]
        raise GitOpsError(f"GitHub API request failed ({exc.code}): {detail}") from exc
    except error.URLError as exc:
        raise GitOpsError(f"GitHub API request failed: {exc.reason}") from exc


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("CI", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("PIP_NO_INPUT", "1")
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
