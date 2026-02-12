#!/usr/bin/env python3
"""Enforce PR-tier mix: majority T1 for basic coding unless escalated."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_POLICY = Path("config/pr_tier_policy.json")
DEFAULT_OUTPUT = Path("artifacts/autonomy/pr_tier_policy_health.json")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    return str(value).strip()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for item in values:
        text = _as_text(item)
        if text:
            out.append(text)
    return out


def _parse_iso(value: Any) -> datetime | None:
    text = _as_text(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _gh_json(repo_root: Path, args: list[str]) -> tuple[bool, Any, str]:
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return False, None, "gh_missing"
    proc = subprocess.run(
        [gh_bin, *args],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        check=False,
    )
    raw = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, None, raw[:600]
    if not raw:
        return True, [], ""
    try:
        return True, json.loads(raw), ""
    except Exception:  # noqa: BLE001
        return False, None, f"invalid_json:{raw[:200]}"


def _collect_repo_prs(
    *,
    root: Path,
    repo: str,
    state: str,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    ok, payload, err = _gh_json(
        root,
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(max(1, limit)),
            "--json",
            "number,title,url,headRefName,isDraft,labels,createdAt,updatedAt,mergedAt,closedAt",
        ],
    )
    if not ok:
        return [], err or "pr_list_failed"
    if not isinstance(payload, list):
        return [], "invalid_pr_payload"
    rows: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            rows.append(item)
    return rows, ""


def _normalize_pr_labels(item: dict[str, Any]) -> list[str]:
    labels_raw = item.get("labels", [])
    if not isinstance(labels_raw, list):
        return []
    out: list[str] = []
    for row in labels_raw:
        if isinstance(row, dict):
            name = _as_text(row.get("name", "")).lower()
            if name:
                out.append(name)
        else:
            value = _as_text(row).lower()
            if value:
                out.append(value)
    return sorted(set(out))


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def evaluate_policy(
    *,
    policy: dict[str, Any],
    repo_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    enabled = _as_bool(policy.get("enabled", True), True)
    min_t1_ratio = _as_float(policy.get("min_t1_ratio", 0.7), 0.7)
    max_escalated_ratio = _as_float(policy.get("max_escalated_ratio", 0.3), 0.3)
    max_unlabeled_ratio = _as_float(policy.get("max_unlabeled_ratio", 0.1), 0.1)
    require_tier_label = _as_bool(policy.get("require_tier_label", True), True)
    min_sample_prs = max(0, _as_int(policy.get("min_sample_prs", 0), 0))
    enforce_min_sample = _as_bool(policy.get("enforce_min_sample", False), False)
    max_violations = max(0, _as_int(policy.get("max_violations", 0), 0))

    labels_cfg = policy.get("labels", {}) if isinstance(policy.get("labels"), dict) else {}
    t1_labels = {item.lower() for item in _normalize_list(labels_cfg.get("t1", []))}
    escalated_labels = {item.lower() for item in _normalize_list(labels_cfg.get("escalated", []))}

    repo_reports: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    total_prs = 0
    total_t1 = 0
    total_escalated = 0
    total_unlabeled = 0
    total_conflict = 0

    for repo_row in repo_payloads:
        repo = _as_text(repo_row.get("repo", ""))
        state = _as_text(repo_row.get("state", "all"))
        fetch_error = _as_text(repo_row.get("fetch_error", ""))
        prs = repo_row.get("pull_requests", []) if isinstance(repo_row.get("pull_requests"), list) else []

        repo_t1 = 0
        repo_escalated = 0
        repo_unlabeled = 0
        repo_conflict = 0
        reviewed = 0
        samples: list[dict[str, Any]] = []

        if fetch_error:
            violations.append(
                {
                    "type": "repo_fetch_failed",
                    "repo": repo,
                    "state": state,
                    "message": fetch_error,
                }
            )

        for pr in prs:
            if not isinstance(pr, dict):
                continue
            labels = _normalize_pr_labels(pr)
            has_t1 = bool(t1_labels.intersection(labels))
            has_escalated = bool(escalated_labels.intersection(labels))
            tier = "unlabeled"
            if has_t1 and has_escalated:
                tier = "conflict"
                repo_conflict += 1
            elif has_t1:
                tier = "t1"
                repo_t1 += 1
            elif has_escalated:
                tier = "escalated"
                repo_escalated += 1
            else:
                repo_unlabeled += 1
            reviewed += 1

            if len(samples) < 20:
                samples.append(
                    {
                        "number": _as_int(pr.get("number", 0), 0),
                        "title": _as_text(pr.get("title", ""))[:180],
                        "head_ref": _as_text(pr.get("headRefName", "")),
                        "created_at": _as_text(pr.get("createdAt", "")),
                        "tier": tier,
                        "labels": labels,
                    }
                )

        total_prs += reviewed
        total_t1 += repo_t1
        total_escalated += repo_escalated
        total_unlabeled += repo_unlabeled
        total_conflict += repo_conflict

        repo_total_ratio = repo_t1 + repo_escalated + repo_unlabeled + repo_conflict
        repo_report = {
            "repo": repo,
            "state": state,
            "fetch_error": fetch_error,
            "reviewed_prs": reviewed,
            "t1_count": repo_t1,
            "escalated_count": repo_escalated,
            "unlabeled_count": repo_unlabeled,
            "conflict_count": repo_conflict,
            "t1_ratio": _ratio(repo_t1, repo_total_ratio),
            "escalated_ratio": _ratio(repo_escalated, repo_total_ratio),
            "unlabeled_ratio": _ratio(repo_unlabeled, repo_total_ratio),
            "samples": samples,
        }
        repo_reports.append(repo_report)

    total_ratio_base = total_t1 + total_escalated + total_unlabeled + total_conflict
    t1_ratio = _ratio(total_t1, total_ratio_base)
    escalated_ratio = _ratio(total_escalated, total_ratio_base)
    unlabeled_ratio = _ratio(total_unlabeled, total_ratio_base)

    if total_conflict > 0:
        violations.append(
            {
                "type": "tier_label_conflict",
                "message": "One or more PRs contain both T1 and escalated labels.",
                "count": total_conflict,
            }
        )

    if require_tier_label and total_unlabeled > 0:
        violations.append(
            {
                "type": "missing_tier_labels",
                "message": "PRs are missing required tier labels.",
                "count": total_unlabeled,
                "unlabeled_ratio": unlabeled_ratio,
            }
        )

    if unlabeled_ratio > max_unlabeled_ratio:
        violations.append(
            {
                "type": "unlabeled_ratio_exceeded",
                "message": "Unlabeled PR ratio exceeds policy threshold.",
                "unlabeled_ratio": unlabeled_ratio,
                "max_unlabeled_ratio": max_unlabeled_ratio,
            }
        )

    if t1_ratio < min_t1_ratio:
        violations.append(
            {
                "type": "t1_ratio_below_threshold",
                "message": "T1 PR ratio below policy minimum.",
                "t1_ratio": t1_ratio,
                "min_t1_ratio": min_t1_ratio,
            }
        )

    if escalated_ratio > max_escalated_ratio:
        violations.append(
            {
                "type": "escalated_ratio_exceeded",
                "message": "Escalated PR ratio above policy maximum.",
                "escalated_ratio": escalated_ratio,
                "max_escalated_ratio": max_escalated_ratio,
            }
        )

    if total_ratio_base < min_sample_prs:
        sample_payload = {
            "type": "insufficient_sample_size",
            "message": "PR sample size below policy minimum.",
            "sample_size": total_ratio_base,
            "min_sample_prs": min_sample_prs,
        }
        if enforce_min_sample:
            violations.append(sample_payload)
        else:
            warnings.append(sample_payload)

    violation_count = len(violations)
    ok = (violation_count <= max_violations) if enabled else True

    return {
        "schema_version": "pr-tier-policy-report.v1",
        "generated_at_utc": _utc_now_iso(),
        "ok": ok,
        "enabled": enabled,
        "summary": {
            "reviewed_prs": total_prs,
            "ratio_base_prs": total_ratio_base,
            "t1_count": total_t1,
            "escalated_count": total_escalated,
            "unlabeled_count": total_unlabeled,
            "conflict_count": total_conflict,
            "t1_ratio": t1_ratio,
            "escalated_ratio": escalated_ratio,
            "unlabeled_ratio": unlabeled_ratio,
            "violation_count": violation_count,
            "warning_count": len(warnings),
        },
        "policy": {
            "min_t1_ratio": min_t1_ratio,
            "max_escalated_ratio": max_escalated_ratio,
            "max_unlabeled_ratio": max_unlabeled_ratio,
            "require_tier_label": require_tier_label,
            "min_sample_prs": min_sample_prs,
            "enforce_min_sample": enforce_min_sample,
            "max_violations": max_violations,
            "t1_labels": sorted(t1_labels),
            "escalated_labels": sorted(escalated_labels),
        },
        "repos": repo_reports,
        "violations": violations,
        "warnings": warnings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enforce T1-majority pull request mix policy.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY), help="Path to PR tier policy JSON.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output report JSON path.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when policy is not satisfied.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary line.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()

    policy_file = Path(args.policy_file)
    if not policy_file.is_absolute():
        policy_file = (root / policy_file).resolve()

    output_file = Path(args.output)
    if not output_file.is_absolute():
        output_file = (root / output_file).resolve()

    policy = _load_json(policy_file)
    repos = _normalize_list(policy.get("repos", []))
    states = _normalize_list(policy.get("states", ["all"]))
    if not states:
        states = ["all"]
    lookback_days = max(1, _as_int(policy.get("lookback_days", 30), 30))
    include_drafts = _as_bool(policy.get("include_drafts", False), False)
    max_prs_per_repo = max(1, _as_int(policy.get("max_prs_per_repo", 200), 200))

    if not repos:
        report = {
            "schema_version": "pr-tier-policy-report.v1",
            "generated_at_utc": _utc_now_iso(),
            "ok": False,
            "enabled": _as_bool(policy.get("enabled", True), True),
            "summary": {
                "reviewed_prs": 0,
                "ratio_base_prs": 0,
                "t1_count": 0,
                "escalated_count": 0,
                "unlabeled_count": 0,
                "conflict_count": 0,
                "t1_ratio": 0.0,
                "escalated_ratio": 0.0,
                "unlabeled_ratio": 0.0,
                "violation_count": 1,
                "warning_count": 0,
            },
            "violations": [
                {
                    "type": "repos_not_configured",
                    "message": "No repositories configured for PR tier policy.",
                }
            ],
            "warnings": [],
            "repos": [],
        }
    else:
        cutoff = _now_utc() - timedelta(days=lookback_days)
        repo_payloads: list[dict[str, Any]] = []
        for repo in repos:
            for state in states:
                prs, fetch_error = _collect_repo_prs(
                    root=root,
                    repo=repo,
                    state=state,
                    limit=max_prs_per_repo,
                )
                filtered: list[dict[str, Any]] = []
                if not fetch_error:
                    for pr in prs:
                        if not include_drafts and _as_bool(pr.get("isDraft", False), False):
                            continue
                        created = _parse_iso(pr.get("createdAt"))
                        if created is None:
                            continue
                        if created < cutoff:
                            continue
                        filtered.append(pr)
                repo_payloads.append(
                    {
                        "repo": repo,
                        "state": state,
                        "fetch_error": fetch_error,
                        "pull_requests": filtered,
                    }
                )

        report = evaluate_policy(policy=policy, repo_payloads=repo_payloads)

    report["artifacts"] = {
        "policy_file": str(policy_file),
        "output_file": str(output_file),
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    payload = {
        "output": str(output_file),
        "ok": bool(report.get("ok", False)),
        "reviewed_prs": int((report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}).get("reviewed_prs", 0) or 0),
        "violation_count": int((report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}).get("violation_count", 0) or 0),
        "t1_ratio": float((report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}).get("t1_ratio", 0.0) or 0.0),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "pr-tier-policy "
            f"ok={payload['ok']} reviewed={payload['reviewed_prs']} "
            f"violations={payload['violation_count']} t1_ratio={payload['t1_ratio']:.3f}"
        )

    if args.strict and not bool(report.get("ok", False)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
