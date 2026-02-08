#!/usr/bin/env python3
"""Repository hygiene checks for orxaq-ops."""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
from pathlib import Path

REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "GOVERNANCE.md",
    "SUPPORT.md",
    "CITATION.cff",
    "docs/VERSIONING.md",
    "docs/AI_BEST_PRACTICES.md",
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
)

FORBIDDEN_TRACKED_PATTERNS = (
    ".venv/*",
    ".pkg-venv/*",
    "dist/*",
    "build/*",
    "*.egg-info",
    "*.egg-info/*",
    "__pycache__/*",
    "*/__pycache__/*",
    "*.pyc",
    "state/*",
    "artifacts/*",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate repository hygiene baseline.")
    parser.add_argument("--root", default=".")
    return parser


def _tracked_paths(root: Path) -> tuple[str, ...]:
    output = subprocess.run(
        ["git", "ls-files"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    missing = [rel for rel in REQUIRED_FILES if not (root / rel).exists()]
    tracked = _tracked_paths(root)
    forbidden = sorted(
        {
            rel
            for rel in tracked
            for pattern in FORBIDDEN_TRACKED_PATTERNS
            if fnmatch.fnmatch(rel, pattern) and (root / rel).exists()
        }
    )

    if not missing and not forbidden:
        print(f"Repo hygiene OK: root={root}")
        return 0

    print("Repo hygiene check failed:")
    if missing:
        print("- Missing required files:")
        for rel in missing:
            print(f"  - {rel}")
    if forbidden:
        print("- Forbidden tracked paths:")
        for rel in forbidden:
            print(f"  - {rel}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
