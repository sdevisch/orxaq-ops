#!/usr/bin/env python3
"""Validate branch-count and stale-branch hygiene for repository operations."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_POLICY = Path("config/git_hygiene_policy.json")
DEFAULT_OUTPUT = Path("artifacts/autonomy/git_hygiene_health.json")
DEFAULT_BASELINE = Path("artifacts/autonomy/git_hygiene_baseline.json")


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def _parse_iso_datetime(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _compile_patterns(raw_patterns: Any) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    if not isinstance(raw_patterns, list):
        return patterns
    for item in raw_patterns:
        text = _as_text(item)
        if not text:
            continue
        try:
            patterns.append(re.compile(text))
        except re.error:
            continue
    return patterns


def _matches_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    for pattern in patterns:
        if pattern.search(text):
            return True
    return False


def load_branch_inventory(repo_root: Path) -> dict[str, Any]:
    cmd = _git_cmd(
        repo_root,
        [
            "for-each-ref",
            "--format=%(refname)\t%(refname:short)\t%(committerdate:iso8601)",
            "refs/heads",
            "refs/remotes",
        ],
    )
    if cmd.returncode != 0:
        raise RuntimeError(cmd.stderr.strip() or cmd.stdout.strip() or "unable_to_read_refs")

    refs: list[dict[str, Any]] = []
    for raw in cmd.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        refname = _as_text(parts[0])
        short = _as_text(parts[1])
        if refname.startswith("refs/remotes/") and short.endswith("/HEAD"):
            continue
        committed_at = _parse_iso_datetime(_as_text(parts[2]))
        refs.append(
            {
                "refname": refname,
                "short": short,
                "is_local": refname.startswith("refs/heads/"),
                "is_remote": refname.startswith("refs/remotes/"),
                "committed_at": committed_at.isoformat() if committed_at is not None else "",
                "committed_at_ts": committed_at.timestamp() if committed_at is not None else 0.0,
            }
        )
    return {"refs": refs}


def _baseline_payload(summary: dict[str, Any], *, repo_root: Path, policy_file: Path) -> dict[str, Any]:
    return {
        "schema_version": "git-hygiene-baseline.v1",
        "generated_at_utc": _utc_now_iso(),
        "repo_root": str(repo_root),
        "policy_file": str(policy_file),
        "local_branch_count": _as_int(summary.get("local_branch_count", 0), 0),
        "remote_branch_count": _as_int(summary.get("remote_branch_count", 0), 0),
        "total_branch_count": _as_int(summary.get("total_branch_count", 0), 0),
        "stale_local_branch_count": _as_int(summary.get("stale_local_branch_count", 0), 0),
    }


def _load_baseline(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not payload:
        return {}
    if str(payload.get("schema_version", "")).strip() != "git-hygiene-baseline.v1":
        return {}
    return payload


def evaluate(*, policy: dict[str, Any], inventory: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    branch_cfg = (
        policy.get("branch_inventory", {})
        if isinstance(policy.get("branch_inventory"), dict)
        else {}
    )
    monitoring_cfg = (
        policy.get("monitoring", {})
        if isinstance(policy.get("monitoring"), dict)
        else {}
    )
    max_local = max(1, _as_int(branch_cfg.get("max_local_branches", 80), 80))
    max_remote = max(1, _as_int(branch_cfg.get("max_remote_branches", 160), 160))
    max_total = max(1, _as_int(branch_cfg.get("max_total_branches", 140), 140))
    stale_days = max(1, _as_int(branch_cfg.get("stale_days", 30), 30))
    max_stale_local = max(0, _as_int(branch_cfg.get("max_stale_local_branches", 40), 40))
    protected_patterns = _compile_patterns(branch_cfg.get("protected_branch_patterns", []))
    use_baseline_guard = _as_bool(monitoring_cfg.get("use_baseline_guard", True), True)
    allow_missing_baseline = _as_bool(monitoring_cfg.get("allow_missing_baseline", True), True)
    allow_legacy_above_threshold = _as_bool(
        monitoring_cfg.get("allow_legacy_above_threshold_when_nonincreasing", True),
        True,
    )
    require_nonincreasing = _as_bool(monitoring_cfg.get("require_nonincreasing", True), True)
    max_delta_local = max(0, _as_int(monitoring_cfg.get("max_delta_local_branches", 0), 0))
    max_delta_remote = max(0, _as_int(monitoring_cfg.get("max_delta_remote_branches", 0), 0))
    max_delta_total = max(0, _as_int(monitoring_cfg.get("max_delta_total_branches", 0), 0))

    refs_raw = inventory.get("refs", [])
    refs = [item for item in refs_raw if isinstance(item, dict)]
    now_utc = datetime.now(UTC)

    local_refs = [item for item in refs if bool(item.get("is_local", False))]
    remote_refs = [item for item in refs if bool(item.get("is_remote", False))]
    total_refs = len(local_refs) + len(remote_refs)

    stale_local: list[dict[str, Any]] = []
    for item in local_refs:
        short = _as_text(item.get("short", ""))
        if _matches_any(protected_patterns, short):
            continue
        committed_raw = _as_text(item.get("committed_at", ""))
        committed_at = _parse_iso_datetime(committed_raw)
        if committed_at is None:
            age_days = 10_000
        else:
            age_days = max(0, int((now_utc - committed_at).total_seconds() // 86400))
        if age_days >= stale_days:
            stale_local.append(
                {
                    "branch": short,
                    "age_days": age_days,
                    "committed_at": committed_raw,
                }
            )
    stale_local.sort(key=lambda item: (-_as_int(item.get("age_days", 0), 0), _as_text(item.get("branch", ""))))

    violations: list[dict[str, Any]] = []
    warnings: list[str] = []
    absolute_violations: list[dict[str, Any]] = []
    if len(local_refs) > max_local:
        absolute_violations.append(
            {
                "type": "local_branch_count_exceeded",
                "message": f"local branches {len(local_refs)} exceeds max {max_local}",
            }
        )
    if len(remote_refs) > max_remote:
        absolute_violations.append(
            {
                "type": "remote_branch_count_exceeded",
                "message": f"remote branches {len(remote_refs)} exceeds max {max_remote}",
            }
        )
    if total_refs > max_total:
        absolute_violations.append(
            {
                "type": "total_branch_count_exceeded",
                "message": f"total branches {total_refs} exceeds max {max_total}",
            }
        )
    if len(stale_local) > max_stale_local:
        absolute_violations.append(
            {
                "type": "stale_local_branch_count_exceeded",
                "message": f"stale local branches {len(stale_local)} exceeds max {max_stale_local}",
            }
        )

    baseline_local = _as_int(baseline.get("local_branch_count", -1), -1)
    baseline_remote = _as_int(baseline.get("remote_branch_count", -1), -1)
    baseline_total = _as_int(baseline.get("total_branch_count", -1), -1)
    baseline_found = baseline_local >= 0 and baseline_remote >= 0 and baseline_total >= 0
    baseline_delta_local = len(local_refs) - baseline_local if baseline_found else 0
    baseline_delta_remote = len(remote_refs) - baseline_remote if baseline_found else 0
    baseline_delta_total = total_refs - baseline_total if baseline_found else 0
    baseline_guard_ok = False
    if use_baseline_guard:
        if baseline_found:
            baseline_guard_ok = (
                baseline_delta_local <= max_delta_local
                and baseline_delta_remote <= max_delta_remote
                and baseline_delta_total <= max_delta_total
            )
            if require_nonincreasing:
                baseline_guard_ok = baseline_guard_ok and (
                    len(local_refs) <= baseline_local
                    and len(remote_refs) <= baseline_remote
                    and total_refs <= baseline_total
                )
        else:
            baseline_guard_ok = allow_missing_baseline

    suppressed_legacy_violations: list[dict[str, Any]] = []
    if use_baseline_guard and baseline_found and baseline_guard_ok and allow_legacy_above_threshold:
        for row in absolute_violations:
            row_type = _as_text(row.get("type", "")).lower()
            if row_type in {
                "local_branch_count_exceeded",
                "remote_branch_count_exceeded",
                "total_branch_count_exceeded",
            }:
                suppressed_legacy_violations.append(row)
                continue
            violations.append(row)
        if suppressed_legacy_violations:
            warnings.append(
                "legacy_absolute_thresholds_suppressed_nonincreasing:"
                + ",".join(_as_text(item.get("type", "")) for item in suppressed_legacy_violations)
            )
    else:
        violations.extend(absolute_violations)

    if use_baseline_guard and baseline_found and not baseline_guard_ok:
        violations.append(
            {
                "type": "baseline_guard_regression",
                "message": (
                    "branch counts regressed beyond baseline guard "
                    f"(delta_local={baseline_delta_local}, delta_remote={baseline_delta_remote}, "
                    f"delta_total={baseline_delta_total})"
                ),
            }
        )

    return {
        "ok": len(violations) == 0,
        "summary": {
            "local_branch_count": len(local_refs),
            "remote_branch_count": len(remote_refs),
            "total_branch_count": total_refs,
            "stale_local_branch_count": len(stale_local),
            "max_local_branches": max_local,
            "max_remote_branches": max_remote,
            "max_total_branches": max_total,
            "stale_days": stale_days,
            "max_stale_local_branches": max_stale_local,
            "violation_count": len(violations),
            "warning_count": len(warnings),
            "baseline_guard_enabled": use_baseline_guard,
            "baseline_found": baseline_found,
            "baseline_guard_ok": baseline_guard_ok,
            "baseline_delta_local_branches": baseline_delta_local,
            "baseline_delta_remote_branches": baseline_delta_remote,
            "baseline_delta_total_branches": baseline_delta_total,
            "suppressed_legacy_violation_count": len(suppressed_legacy_violations),
        },
        "violations": violations,
        "warnings": warnings,
        "suppressed_legacy_violations": suppressed_legacy_violations,
        "stale_local_preview": stale_local[:20],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check deterministic git branch-hygiene policy health.")
    parser.add_argument("--root", default=".", help="Path to orxaq-ops root.")
    parser.add_argument("--repo-root", default=".", help="Path to repository root for git branch checks.")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY), help="Path to git hygiene policy JSON.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path to output JSON artifact.")
    parser.add_argument("--baseline-file", default="", help="Baseline JSON file path for non-regression guard.")
    parser.add_argument("--capture-baseline", action="store_true", help="Capture current branch counts as baseline.")
    parser.add_argument("--json", action="store_true", help="Print JSON payload.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    repo_root = Path(args.repo_root).expanduser().resolve()
    policy_file = Path(args.policy_file).expanduser().resolve()
    if not policy_file.is_absolute():
        policy_file = (root / policy_file).resolve()
    output_file = Path(args.output).expanduser().resolve()
    if not output_file.is_absolute():
        output_file = (root / output_file).resolve()

    policy = _load_json(policy_file)
    if not policy:
        policy = {
            "branch_inventory": {
                "max_local_branches": 80,
                "max_remote_branches": 160,
                "max_total_branches": 140,
                "stale_days": 30,
                "max_stale_local_branches": 40,
                "protected_branch_patterns": ["^main$", "^master$", "^develop$", "^release/.+$"],
            },
            "monitoring": {
                "baseline_file": str(DEFAULT_BASELINE),
                "use_baseline_guard": True,
                "allow_missing_baseline": True,
                "allow_legacy_above_threshold_when_nonincreasing": True,
                "require_nonincreasing": True,
                "max_delta_local_branches": 0,
                "max_delta_remote_branches": 0,
                "max_delta_total_branches": 0,
            },
        }

    monitoring_cfg = (
        policy.get("monitoring", {})
        if isinstance(policy.get("monitoring"), dict)
        else {}
    )
    baseline_raw = _as_text(args.baseline_file) or _as_text(monitoring_cfg.get("baseline_file", ""))
    baseline_file = Path(baseline_raw).expanduser() if baseline_raw else DEFAULT_BASELINE
    if not baseline_file.is_absolute():
        baseline_file = (root / baseline_file).resolve()

    try:
        inventory = load_branch_inventory(repo_root)
        baseline = _load_baseline(baseline_file)
        evaluated = evaluate(policy=policy, inventory=inventory, baseline=baseline)
        if args.capture_baseline:
            baseline_payload = _baseline_payload(
                evaluated.get("summary", {}) if isinstance(evaluated.get("summary"), dict) else {},
                repo_root=repo_root,
                policy_file=policy_file,
            )
            _save_json(baseline_file, baseline_payload)
        payload = {
            "schema_version": "git-hygiene-health.v1",
            "generated_at_utc": _utc_now_iso(),
            "root_dir": str(root),
            "repo_root": str(repo_root),
            "policy_file": str(policy_file),
            "baseline_file": str(baseline_file),
            "baseline_capture_used": bool(args.capture_baseline),
            **evaluated,
        }
    except Exception as err:  # noqa: BLE001
        payload = {
            "schema_version": "git-hygiene-health.v1",
            "generated_at_utc": _utc_now_iso(),
            "root_dir": str(root),
            "repo_root": str(repo_root),
            "policy_file": str(policy_file),
            "baseline_file": str(baseline_file),
            "baseline_capture_used": bool(args.capture_baseline),
            "ok": False,
            "summary": {
                "local_branch_count": 0,
                "remote_branch_count": 0,
                "total_branch_count": 0,
                "stale_local_branch_count": 0,
                "max_local_branches": 0,
                "max_remote_branches": 0,
                "max_total_branches": 0,
                "stale_days": 0,
                "max_stale_local_branches": 0,
                "violation_count": 1,
                "warning_count": 0,
            },
            "violations": [{"type": "git_inventory_failed", "message": str(err)}],
            "warnings": [],
            "suppressed_legacy_violations": [],
            "stale_local_preview": [],
        }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
        print(
            "Git hygiene health "
            f"{'OK' if _as_bool(payload.get('ok', False), False) else 'FAILED'}: "
            f"local={_as_int(summary.get('local_branch_count', 0), 0)} "
            f"remote={_as_int(summary.get('remote_branch_count', 0), 0)} "
            f"total={_as_int(summary.get('total_branch_count', 0), 0)} "
            f"stale_local={_as_int(summary.get('stale_local_branch_count', 0), 0)} "
            f"violations={_as_int(summary.get('violation_count', 0), 0)}"
        )
    return 0 if _as_bool(payload.get("ok", False), False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
