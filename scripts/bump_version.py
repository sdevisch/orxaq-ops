#!/usr/bin/env python3
"""SemVer bump helper for orxaq-autonomy."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _replace_version(path: Path, current: str, updated: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r'(^version\s*=\s*")' + re.escape(current) + r'(")', re.MULTILINE)
    changed, count = pattern.subn(r"\g<1>" + updated + r"\g<2>", text, count=1)
    if count != 1:
        raise ValueError(f"Could not update version '{current}' in {path}.")
    path.write_text(changed, encoding="utf-8")


def _replace_init_version(path: Path, updated: str) -> None:
    text = path.read_text(encoding="utf-8")
    changed, count = re.subn(
        r'(__version__\s*=\s*")[^"]+(")',
        r"\g<1>" + updated + r"\g<2>",
        text,
        count=1,
    )
    if count != 1:
        raise ValueError(f"Could not update __version__ in {path}.")
    path.write_text(changed, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bump project SemVer version.")
    parser.add_argument("--part", required=True, choices=("patch", "minor", "major"))
    parser.add_argument("--pyproject", default="pyproject.toml")
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    from orxaq_autonomy.versioning import bump_version, load_project_version, validate_semver

    args = build_parser().parse_args()
    pyproject = Path(args.pyproject).resolve()
    current = load_project_version(pyproject)
    if validate_semver(current):
        print(f"Current version '{current}' is not valid SemVer.")
        return 1

    updated = bump_version(current, args.part)
    if args.apply:
        _replace_version(pyproject, current, updated)
        _replace_init_version(ROOT / "src" / "orxaq_autonomy" / "__init__.py", updated)
        print(f"Updated version: {current} -> {updated}")
    else:
        print(f"Next version ({args.part}): {current} -> {updated}")

    print(f"Next release tag: v{updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
