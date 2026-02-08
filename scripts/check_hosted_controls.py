#!/usr/bin/env python3
"""Validate hosted controls: branch protection and README badges."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class RepoSpec:
    repo: str
    branch: str
    readme: Path


def _default_specs(root: Path) -> list[RepoSpec]:
    return [
        RepoSpec(repo="sdevisch/orxaq", branch="main", readme=(root.parent / "orxaq" / "README.md").resolve()),
        RepoSpec(repo="sdevisch/orxaq-ops", branch="main", readme=(root / "README.md").resolve()),
    ]


def _gh_api_json(endpoint: str) -> tuple[bool, dict | None, str]:
    proc = subprocess.run(
        ["gh", "api", endpoint],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    raw = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, None, raw
    try:
        return True, json.loads(raw), ""
    except Exception:
        return False, None, f"Non-JSON response from gh api: {raw}"


def branch_protection_errors(repo: str, branch: str) -> list[str]:
    ok, payload, raw_error = _gh_api_json(f"repos/{repo}/branches/{branch}/protection")
    if not ok:
        lowered = raw_error.lower()
        if "http 404" in lowered or "branch not protected" in lowered:
            return [f"{repo}:{branch} is not protected."]
        if "upgrade to github pro" in lowered:
            return [
                f"{repo}:{branch} protection unavailable on current GitHub plan for private repos."
            ]
        return [f"{repo}:{branch} protection check failed: {raw_error}"]

    assert payload is not None
    errors: list[str] = []
    contexts = (payload.get("required_status_checks") or {}).get("contexts") or []
    reviews = payload.get("required_pull_request_reviews") or {}
    enforce_admins = (payload.get("enforce_admins") or {}).get("enabled") is True
    linear = (payload.get("required_linear_history") or {}).get("enabled") is True
    convo = (payload.get("required_conversation_resolution") or {}).get("enabled") is True
    if not contexts:
        errors.append(f"{repo}:{branch} has no required status checks.")
    if not enforce_admins:
        errors.append(f"{repo}:{branch} does not enforce protections for admins.")
    if int(reviews.get("required_approving_review_count", 0)) < 1:
        errors.append(f"{repo}:{branch} requires fewer than 1 approving review.")
    if not bool(reviews.get("require_code_owner_reviews", False)):
        errors.append(f"{repo}:{branch} does not require code owner reviews.")
    if not linear:
        errors.append(f"{repo}:{branch} does not require linear history.")
    if not convo:
        errors.append(f"{repo}:{branch} does not require conversation resolution.")
    return errors


def badge_urls_from_readme(readme: Path) -> list[str]:
    text = readme.read_text(encoding="utf-8")
    return re.findall(r"!\[[^\]]*\]\((https?://[^)]+)\)", text)


def _badge_url_error(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "orxaq-hosted-controls"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = resp.headers.get_content_type()
            if resp.status >= 400:
                return f"{url} returned HTTP {resp.status}"
            if not content_type.startswith("image/"):
                return f"{url} returned non-image content-type '{content_type}'"
    except Exception as exc:
        return f"{url} failed: {exc}"
    return None


def badge_errors(readme: Path) -> list[str]:
    if not readme.exists():
        return [f"Missing README for badge checks: {readme}"]
    urls = badge_urls_from_readme(readme)
    if not urls:
        return [f"No badges found in {readme}"]
    errors = [err for err in (_badge_url_error(url) for url in urls) if err]
    return errors


def parse_specs(root: Path, values: Iterable[str]) -> list[RepoSpec]:
    specs: list[RepoSpec] = []
    for value in values:
        parts = value.split("|")
        if len(parts) != 3:
            raise ValueError(f"Invalid --spec '{value}'. Expected: owner/repo|branch|/abs/path/README.md")
        repo, branch, readme = parts
        specs.append(RepoSpec(repo=repo.strip(), branch=branch.strip(), readme=Path(readme).expanduser().resolve()))
    return specs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate hosted collaboration controls.")
    parser.add_argument(
        "--root",
        default=".",
        help="Path to orxaq-ops root (default: current directory).",
    )
    parser.add_argument(
        "--spec",
        action="append",
        default=[],
        help="Override target spec as owner/repo|branch|/abs/path/README.md (repeatable).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    specs = parse_specs(root, args.spec) if args.spec else _default_specs(root)

    all_errors: list[str] = []
    for spec in specs:
        all_errors.extend(branch_protection_errors(spec.repo, spec.branch))
        all_errors.extend(badge_errors(spec.readme))

    if all_errors:
        print("Hosted controls check failed:")
        for err in all_errors:
            print(f"- {err}")
        return 1

    print("Hosted controls OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
