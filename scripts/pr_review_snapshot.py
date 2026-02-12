#!/usr/bin/env python3
"""Generate a fast PR review snapshot to accelerate deterministic triage.

This script is intentionally artifact-first: it emits a JSON snapshot (and optional
Markdown) that summarizes mergeability, CI check state, and review readiness across
open PRs. It is designed to replace slow, manual per-PR inspection loops.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = Path("artifacts/autonomy/pr_review_snapshot.json")
DEFAULT_MARKDOWN = Path("artifacts/autonomy/pr_review_snapshot.md")


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


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )


def _gh_json(args: list[str], *, cwd: Path) -> tuple[bool, Any, str]:
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return (False, None, "gh_missing")
    proc = _run([gh_bin, *args], cwd=cwd)
    raw = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return (False, None, raw[:600] or "gh_failed")
    if not raw:
        return (True, {}, "")
    try:
        return (True, json.loads(raw), "")
    except Exception:  # noqa: BLE001
        return (False, None, f"invalid_json:{raw[:200]}")


def _actor_login(*, cwd: Path) -> str:
    ok, payload, _ = _gh_json(["api", "user"], cwd=cwd)
    if ok and isinstance(payload, dict):
        return _as_text(payload.get("login", ""))
    return ""


def _list_open_pr_numbers(repo: str, *, cwd: Path, limit: int) -> tuple[list[int], str]:
    ok, payload, err = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(max(1, limit)),
            "--json",
            "number",
        ],
        cwd=cwd,
    )
    if not ok:
        return ([], err)
    if not isinstance(payload, list):
        return ([], "invalid_pr_list_payload")
    out: list[int] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        number = _as_int(item.get("number", 0), 0)
        if number > 0:
            out.append(number)
    return (sorted(set(out)), "")


def _normalize_labels(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = _as_text(item.get("name", ""))
            if name:
                labels.append(name)
        else:
            text = _as_text(item)
            if text:
                labels.append(text)
    return sorted(set(labels))


def _evaluate_checks(status_rollup: Any) -> tuple[str, dict[str, int]]:
    """Return overall check state plus deterministic counts.

    State mapping:
    - no_checks: no check contexts attached to PR
    - success: all contexts succeeded or were skipped/neutral
    - pending: at least one context is pending/unknown, none failing
    - failure: at least one context failed/cancelled/timed-out/action-required
    """

    contexts = status_rollup if isinstance(status_rollup, list) else []
    if not contexts:
        return ("no_checks", {"total": 0, "success": 0, "failure": 0, "pending": 0})

    counts = {"total": 0, "success": 0, "failure": 0, "pending": 0}

    failing = {
        "FAILURE",
        "CANCELLED",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "STALE",
        "ERROR",
    }
    okish = {"SUCCESS", "SKIPPED", "NEUTRAL"}

    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        counts["total"] += 1
        typename = _as_text(ctx.get("__typename", "")).strip()
        if typename.lower() == "checkrun" or "conclusion" in ctx or "status" in ctx:
            status = _as_text(ctx.get("status", "")).upper()
            conclusion = _as_text(ctx.get("conclusion", "")).upper()
            if status and status != "COMPLETED":
                counts["pending"] += 1
            elif not conclusion:
                counts["pending"] += 1
            elif conclusion in failing:
                counts["failure"] += 1
            elif conclusion in okish:
                counts["success"] += 1
            else:
                counts["pending"] += 1
            continue

        state = _as_text(ctx.get("state", "")).upper()
        if not state:
            counts["pending"] += 1
        elif state in failing:
            counts["failure"] += 1
        elif state in okish:
            counts["success"] += 1
        else:
            counts["pending"] += 1

    if counts["failure"] > 0:
        return ("failure", counts)
    if counts["pending"] > 0:
        return ("pending", counts)
    return ("success", counts)


def _derive_next_action(item: dict[str, Any]) -> str:
    if _as_bool(item.get("is_draft", False), False):
        return "skip_draft"
    mergeable = _as_text(item.get("mergeable", "")).upper()
    checks_state = _as_text(item.get("checks_state", "")).lower()
    review_decision = _as_text(item.get("review_decision", "")).upper()

    if mergeable == "CONFLICTING":
        return "resolve_conflicts_rebase"
    if checks_state == "no_checks":
        return "trigger_ci"
    if checks_state == "pending":
        return "wait_for_ci"
    if checks_state == "failure":
        return "fix_ci"
    if review_decision == "REVIEW_REQUIRED":
        return "needs_review"
    if review_decision and review_decision != "APPROVED":
        return "needs_review"
    return "merge"


def _fetch_pr_view(repo: str, number: int, *, cwd: Path) -> tuple[dict[str, Any] | None, str, float]:
    start = time.perf_counter()
    ok, payload, err = _gh_json(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            (
                "number,title,url,isDraft,author,headRefName,baseRefName,mergeable,reviewDecision,"
                "statusCheckRollup,labels,additions,deletions,changedFiles,updatedAt"
            ),
        ],
        cwd=cwd,
    )
    duration_ms = (time.perf_counter() - start) * 1000.0
    if not ok or not isinstance(payload, dict):
        return (None, err or "pr_view_failed", duration_ms)
    return (payload, "", duration_ms)


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    rows = payload.get("pull_requests", []) if isinstance(payload.get("pull_requests"), list) else []

    lines: list[str] = []
    lines.append("# PR Review Snapshot")
    lines.append("")
    lines.append(f"- generated_at_utc: `{_as_text(payload.get('generated_at_utc', ''))}`")
    lines.append(f"- ok: `{_as_bool(payload.get('ok', False), False)}`")
    lines.append(f"- partial: `{_as_bool(payload.get('partial', False), False)}`")
    lines.append(
        "- totals:"
        f" pr_total={_as_int(summary.get('pr_total', 0), 0)}"
        f" conflicting={_as_int(summary.get('conflicting', 0), 0)}"
        f" failing_checks={_as_int(summary.get('failing_checks', 0), 0)}"
        f" pending_checks={_as_int(summary.get('pending_checks', 0), 0)}"
        f" no_checks={_as_int(summary.get('no_checks', 0), 0)}"
        f" needs_review={_as_int(summary.get('needs_review', 0), 0)}"
        f" self_authored={_as_int(summary.get('self_authored', 0), 0)}"
        f" ready_to_merge={_as_int(summary.get('ready_to_merge', 0), 0)}"
    )
    lines.append("")
    lines.append("| Repo | PR | Mergeable | Checks | Review | Next Action | Title |")
    lines.append("| --- | ---:| --- | --- | --- | --- | --- |")
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        repo = _as_text(row.get("repo", ""))
        number = _as_int(row.get("number", 0), 0)
        mergeable = _as_text(row.get("mergeable", ""))
        checks = _as_text(row.get("checks_state", ""))
        review = _as_text(row.get("review_decision", ""))
        action = _as_text(row.get("next_action", ""))
        title = _as_text(row.get("title", "")).replace("|", "\\|")
        lines.append(f"| {repo} | {number} | {mergeable} | {checks} | {review} | {action} | {title} |")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a fast PR review snapshot across repos.")
    parser.add_argument("--root", default=".", help="Workspace root for gh execution.")
    parser.add_argument("--repo", action="append", default=[], help="Repo slug owner/name (repeatable).")
    parser.add_argument("--limit", type=int, default=200, help="Maximum open PRs per repo to scan.")
    parser.add_argument("--max-workers", type=int, default=8, help="Maximum concurrent gh calls.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON output artifact.")
    parser.add_argument("--markdown", default=str(DEFAULT_MARKDOWN), help="Optional Markdown output artifact.")
    parser.add_argument("--json", action="store_true", help="Print full JSON payload.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = (root / output).resolve()
    markdown = Path(args.markdown).expanduser()
    if not markdown.is_absolute():
        markdown = (root / markdown).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    repos = [_as_text(item) for item in args.repo if _as_text(item)]
    repos = sorted(dict.fromkeys(repos))
    actor = _actor_login(cwd=root)

    start = time.perf_counter()
    pull_requests: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    fetched = 0

    pr_targets: list[tuple[str, int]] = []
    for repo in repos:
        numbers, err = _list_open_pr_numbers(repo, cwd=root, limit=max(1, int(args.limit)))
        if err:
            errors.append({"repo": repo, "number": 0, "error": err})
            continue
        for number in numbers:
            pr_targets.append((repo, number))

    max_workers = max(1, min(32, int(args.max_workers)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_pr_view, repo, number, cwd=root): (repo, number) for repo, number in pr_targets}
        for future in as_completed(futures):
            repo, number = futures[future]
            try:
                payload, err, duration_ms = future.result()
            except Exception as exc:  # noqa: BLE001
                errors.append({"repo": repo, "number": number, "error": f"exception:{type(exc).__name__}"})
                continue
            if payload is None:
                errors.append({"repo": repo, "number": number, "error": err or "pr_view_failed"})
                continue

            fetched += 1
            author = payload.get("author", {}) if isinstance(payload.get("author"), dict) else {}
            author_login = _as_text(author.get("login", ""))
            is_draft = _as_bool(payload.get("isDraft", False), False)
            review_decision = _as_text(payload.get("reviewDecision", "")).upper()
            mergeable = _as_text(payload.get("mergeable", "")).upper()
            labels = _normalize_labels(payload.get("labels", []))

            checks_state, checks_counts = _evaluate_checks(payload.get("statusCheckRollup", []))

            record: dict[str, Any] = {
                "repo": repo,
                "number": _as_int(payload.get("number", number), number),
                "title": _as_text(payload.get("title", "")),
                "url": _as_text(payload.get("url", "")),
                "is_draft": is_draft,
                "author_login": author_login,
                "author_is_bot": _as_bool(author.get("is_bot", author.get("isBot", False)), False),
                "actor_login": actor,
                "self_authored": bool(actor and author_login and actor.lower() == author_login.lower()),
                "head_ref": _as_text(payload.get("headRefName", "")),
                "base_ref": _as_text(payload.get("baseRefName", "")),
                "mergeable": mergeable,
                "review_decision": review_decision,
                "checks_state": checks_state,
                "checks_counts": checks_counts,
                "updated_at": _as_text(payload.get("updatedAt", "")),
                "labels": labels,
                "additions": _as_int(payload.get("additions", 0), 0),
                "deletions": _as_int(payload.get("deletions", 0), 0),
                "changed_files": _as_int(payload.get("changedFiles", 0), 0),
                "fetch_duration_ms": round(duration_ms, 2),
            }
            record["next_action"] = _derive_next_action(record)
            record["ready_to_merge"] = (
                (not is_draft)
                and mergeable == "MERGEABLE"
                and checks_state == "success"
                and (review_decision in {"", "APPROVED"})
            )
            pull_requests.append(record)

    pull_requests.sort(key=lambda item: (item.get("repo", ""), _as_int(item.get("number", 0), 0)))

    summary = {
        "repo_count": len(repos),
        "pr_total": len(pull_requests),
        "fetched": fetched,
        "error_count": len(errors),
        "conflicting": sum(1 for pr in pull_requests if _as_text(pr.get("mergeable", "")).upper() == "CONFLICTING"),
        "failing_checks": sum(1 for pr in pull_requests if _as_text(pr.get("checks_state", "")) == "failure"),
        "pending_checks": sum(1 for pr in pull_requests if _as_text(pr.get("checks_state", "")) == "pending"),
        "no_checks": sum(1 for pr in pull_requests if _as_text(pr.get("checks_state", "")) == "no_checks"),
        "needs_review": sum(1 for pr in pull_requests if _as_text(pr.get("next_action", "")) == "needs_review"),
        "self_authored": sum(1 for pr in pull_requests if _as_bool(pr.get("self_authored", False), False)),
        "ready_to_merge": sum(1 for pr in pull_requests if _as_bool(pr.get("ready_to_merge", False), False)),
        "duration_ms": round((time.perf_counter() - start) * 1000.0, 2),
        "max_workers": max_workers,
    }

    payload = {
        "schema_version": "pr-review-snapshot.v1",
        "generated_at_utc": _utc_now_iso(),
        "root_dir": str(root),
        "actor_login": actor,
        "ok": True,
        "partial": bool(errors),
        "repos": repos,
        "summary": summary,
        "pull_requests": pull_requests,
        "errors": errors,
    }

    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_markdown(markdown, payload)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

