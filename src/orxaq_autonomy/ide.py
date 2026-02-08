"""IDE-independent workspace generation and launcher helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


IDE_COMMANDS: dict[str, list[str]] = {
    "vscode": [
        "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
        "code",
        "code-insiders",
    ],
    "cursor": [
        "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
        "cursor",
    ],
    "pycharm": ["charm", "pycharm", "pycharm64.exe", "idea64.exe"],
}


def generate_workspace(root: Path, impl_repo: Path, test_repo: Path, output_file: Path) -> Path:
    payload = {
        "folders": [
            {"name": "orxaq", "path": str(impl_repo)},
            {"name": "orxaq_gemini", "path": str(test_repo)},
            {"name": "orxaq_ops", "path": str(root)},
        ]
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output_file


def resolve_ide_command(ide: str) -> str | None:
    candidates = IDE_COMMANDS.get(ide, [])
    for candidate in candidates:
        if candidate.startswith("/") and Path(candidate).exists():
            return candidate
        path = shutil.which(candidate)
        if path:
            return path
    return None


def open_in_ide(*, ide: str, root: Path, workspace_file: Path | None = None) -> str:
    command = resolve_ide_command(ide)
    if command is None:
        raise RuntimeError(f"IDE command not found for {ide}")

    target = str(workspace_file if workspace_file and ide in {"vscode", "cursor"} else root)
    proc = subprocess.Popen([command, target], cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"opened with: {command} (pid={proc.pid})"
