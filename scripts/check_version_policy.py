#!/usr/bin/env python3
"""Validate SemVer policy and optional tag alignment for orxaq-autonomy."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _detect_ci_tag(env: dict[str, str]) -> str | None:
    if env.get("GITHUB_REF_TYPE") == "tag" and env.get("GITHUB_REF_NAME"):
        return env["GITHUB_REF_NAME"].strip() or None
    ref = env.get("GITHUB_REF", "").strip()
    if ref.startswith("refs/tags/"):
        return ref[len("refs/tags/") :].strip() or None
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate SemVer version policy.")
    parser.add_argument("--pyproject", default="pyproject.toml")
    parser.add_argument("--tag", default=None)
    return parser


def main() -> int:
    from orxaq_autonomy.versioning import (
        load_project_version,
        validate_release_tag,
        validate_semver,
    )

    args = build_parser().parse_args()
    version = load_project_version(Path(args.pyproject).resolve())
    tag = args.tag if args.tag is not None else _detect_ci_tag(os.environ)
    errors = [*validate_semver(version), *validate_release_tag(version, tag)]
    if errors:
        print("Version policy check failed:")
        for message in errors:
            print(f"- {message}")
        return 1
    tag_segment = f", tag={tag}" if tag else ""
    print(f"Version policy OK: version={version}{tag_segment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
