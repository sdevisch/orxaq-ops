"""Context helpers for MCP/file-based augmentation."""

from __future__ import annotations

import json
from pathlib import Path


def summarize_filetypes_from_git_ls_files(repo: Path, limit: int = 8) -> str:
    from collections import Counter
    import subprocess

    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return "File-type profile unavailable."
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not files:
        return "File-type profile unavailable."
    counts: Counter[str] = Counter()
    for rel in files:
        suffix = Path(rel).suffix.lower().lstrip(".")
        counts[suffix or "(no_ext)"] += 1
    top = ", ".join(f"{ext}:{count}" for ext, count in counts.most_common(limit))
    return f"Top file types: {top}."


def write_default_skill_protocol(path: Path) -> None:
    payload = {
        "name": "orxaq-autonomy",
        "version": "1",
        "description": "Portable autonomy protocol for resilient multi-agent execution.",
        "required_behaviors": [
            "work-non-interactively",
            "retry-transient-failures",
            "recover-git-locks",
            "validate-and-report",
            "continue-after-partial-output",
        ],
        "filetype_policy": "Preserve unknown and binary file types; avoid destructive rewrites; use .gitattributes for explicit handling.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
