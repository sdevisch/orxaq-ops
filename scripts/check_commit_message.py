#!/usr/bin/env python3
"""Enforce commit message quality for collaborative automation."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|test|chore|perf|build|ci|revert)(\([a-z0-9._/-]+\))?:\s.+$"
)
VERB_FIRST_RE = re.compile(
    r"^(add|fix|update|remove|refactor|harden|enforce|improve|implement|document|test|build|bump|rename|revert|release|optimize|clean|stabilize|guard|prevent)\b"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate commit message format.")
    parser.add_argument("commit_msg_file", help="Path to commit message file provided by commit-msg hook.")
    return parser


def _load_subject(path: Path) -> tuple[str, list[str]]:
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    lines: list[str] = []
    for line in raw_lines:
        if line.startswith("#"):
            continue
        lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    subject = lines[0].strip() if lines else ""
    return subject, lines


def main() -> int:
    args = build_parser().parse_args()
    msg_file = Path(args.commit_msg_file).resolve()
    if not msg_file.exists():
        print(f"Commit message policy failed: message file not found: {msg_file}")
        return 1

    subject, lines = _load_subject(msg_file)
    if not subject:
        print("Commit message policy failed: empty commit subject.")
        return 1

    if len(subject) > 72:
        print(f"Commit message policy failed: subject exceeds 72 chars ({len(subject)}).")
        return 1

    if subject.endswith("."):
        print("Commit message policy failed: subject should not end with a period.")
        return 1

    if len(lines) > 1 and lines[1].strip():
        print("Commit message policy failed: add a blank line between subject and body.")
        return 1

    if subject.startswith("Merge ") or subject.startswith("Revert "):
        print("Commit message policy OK: merge/revert subject allowed.")
        return 0

    if CONVENTIONAL_RE.match(subject) or VERB_FIRST_RE.match(subject):
        print("Commit message policy OK.")
        return 0

    print(
        "Commit message policy failed: subject must be conventional "
        "(e.g. 'feat(scope): ...') or verb-first imperative (e.g. 'add ...')."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
