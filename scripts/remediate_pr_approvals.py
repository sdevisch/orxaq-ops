#!/usr/bin/env python3
"""Deterministically remediate open PR approvals with auditable outcomes."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = Path("artifacts/autonomy/pr_approval_remediation.json")


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_text(value: Any) -> str:
    return str(value).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    lowered = _as_text(value).lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )


def _gh_cmd(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    gh_bin = shutil.which("gh")
    if not gh_bin:
        raise RuntimeError("gh executable not found in PATH")
    return _run([gh_bin, *args], cwd=cwd)


def _gh_json(args: list[str], *, cwd: Path) -> tuple[bool, Any, str]:
    proc = _gh_cmd(args, cwd=cwd)
    raw = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return (False, None, raw)
    if not raw:
        return (True, {}, "")
    try:
        return (True, json.loads(raw), "")
    except Exception:  # noqa: BLE001
        return (False, None, f"invalid_json:{raw[:400]}")


def _actor_login(cwd: Path) -> str:
    ok, payload, _ = _gh_json(["api", "user"], cwd=cwd)
    if ok and isinstance(payload, dict):
        return _as_text(payload.get("login", ""))
    return ""


def _list_open_prs(repo: str, *, cwd: Path, limit: int) -> tuple[list[dict[str, Any]], str]:
    ok, payload, err = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,url,isDraft,reviewDecision,author",
        ],
        cwd=cwd,
    )
    if not ok:
        return ([], err)
    if not isinstance(payload, list):
        return ([], "invalid_pr_list_payload")
    rows: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            rows.append(item)
    return (rows, "")


def _approve_pr(repo: str, number: int, *, cwd: Path) -> tuple[bool, str]:
    proc = _gh_cmd(
        [
            "pr",
            "review",
            str(number),
            "--repo",
            repo,
            "--approve",
            "--body",
            "Automated elevated approval remediation run.",
        ],
        cwd=cwd,
    )
    if proc.returncode == 0:
        return (True, "")
    return (False, (proc.stderr or proc.stdout or "approval_failed").strip())


def _request_reviewers(repo: str, number: int, reviewers: list[str], *, cwd: Path) -> tuple[bool, str]:
    if not reviewers:
        return (True, "")
    args = ["pr", "edit", str(number), "--repo", repo]
    for reviewer in reviewers:
        args.extend(["--add-reviewer", reviewer])
    proc = _gh_cmd(args, cwd=cwd)
    if proc.returncode == 0:
        return (True, "")
    return (False, (proc.stderr or proc.stdout or "request_reviewer_failed").strip())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Approve eligible open PRs and report self-approval blockers.")
    parser.add_argument("--root", default=".", help="Workspace root for command execution.")
    parser.add_argument("--repo", action="append", default=[], help="GitHub repo slug owner/name (repeatable).")
    parser.add_argument("--limit", type=int, default=200, help="Maximum open PRs per repo to scan.")
    parser.add_argument(
        "--request-reviewer",
        action="append",
        default=[],
        help="Reviewer login to request for self-authored PRs (repeatable).",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON output artifact.")
    parser.add_argument("--json", action="store_true", help="Print full JSON payload.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = (root / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    repos = [_as_text(item) for item in args.repo if _as_text(item)]
    repos = sorted(dict.fromkeys(repos))
    actor = _actor_login(root)
    reviewers = sorted({r for r in (_as_text(item) for item in args.request_reviewer) if r and r != actor})

    repo_results: list[dict[str, Any]] = []
    total_seen = 0
    total_approved = 0
    total_self_blocked = 0
    total_other_blocked = 0

    for repo in repos:
        prs, pr_list_error = _list_open_prs(repo, cwd=root, limit=max(1, int(args.limit)))
        repo_row: dict[str, Any] = {
            "repo": repo,
            "ok": True,
            "error": "",
            "open_pr_count": len(prs),
            "approved_count": 0,
            "self_blocked_count": 0,
            "other_blocked_count": 0,
            "records": [],
        }
        if pr_list_error:
            repo_row["ok"] = False
            repo_row["error"] = pr_list_error
            repo_results.append(repo_row)
            total_other_blocked += 1
            continue

        for pr in prs:
            total_seen += 1
            number = int(pr.get("number", 0) or 0)
            title = _as_text(pr.get("title", ""))
            url = _as_text(pr.get("url", ""))
            draft = _as_bool(pr.get("isDraft", False), False)
            review_decision = _as_text(pr.get("reviewDecision", "")).upper()
            author = pr.get("author", {}) if isinstance(pr.get("author"), dict) else {}
            author_login = _as_text(author.get("login", ""))

            record: dict[str, Any] = {
                "number": number,
                "title": title,
                "url": url,
                "is_draft": draft,
                "review_decision": review_decision,
                "author_login": author_login,
                "actor_login": actor,
                "status": "skipped",
                "detail": "",
            }

            if draft:
                record["status"] = "skipped_draft"
                repo_row["records"].append(record)
                continue
            if review_decision == "APPROVED":
                record["status"] = "already_approved"
                repo_row["records"].append(record)
                continue
            if actor and author_login and actor.lower() == author_login.lower():
                record["status"] = "blocked_self_approval"
                total_self_blocked += 1
                repo_row["self_blocked_count"] += 1
                if reviewers:
                    ok_req, err_req = _request_reviewers(repo, number, reviewers, cwd=root)
                    if ok_req:
                        record["detail"] = "requested_reviewers:" + ",".join(reviewers)
                    else:
                        record["detail"] = f"request_reviewer_failed:{err_req}"
                        total_other_blocked += 1
                        repo_row["other_blocked_count"] += 1
                repo_row["records"].append(record)
                continue

            ok_approve, err_approve = _approve_pr(repo, number, cwd=root)
            if ok_approve:
                record["status"] = "approved"
                total_approved += 1
                repo_row["approved_count"] += 1
            else:
                record["status"] = "blocked_approval_failed"
                record["detail"] = err_approve
                total_other_blocked += 1
                repo_row["other_blocked_count"] += 1
            repo_row["records"].append(record)

        repo_results.append(repo_row)

    payload = {
        "schema_version": "pr-approval-remediation.v1",
        "generated_at_utc": _utc_now_iso(),
        "actor_login": actor,
        "repos": repos,
        "summary": {
            "open_prs_seen": total_seen,
            "approved_count": total_approved,
            "self_blocked_count": total_self_blocked,
            "other_blocked_count": total_other_blocked,
            "repo_count": len(repos),
        },
        "results": repo_results,
        "ok": total_other_blocked == 0,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
        print(
            "PR approval remediation "
            f"{'OK' if payload.get('ok', False) else 'PARTIAL'}: "
            f"seen={summary.get('open_prs_seen', 0)} "
            f"approved={summary.get('approved_count', 0)} "
            f"self_blocked={summary.get('self_blocked_count', 0)} "
            f"other_blocked={summary.get('other_blocked_count', 0)} "
            f"output={output}"
        )
    return 0 if payload.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
