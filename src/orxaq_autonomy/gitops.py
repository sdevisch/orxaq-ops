"""GitOps helper commands for branch/PR/wait/merge automation."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    completed = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False)
    return completed.returncode, completed.stdout, completed.stderr


def _health_score(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    score = payload.get("score", 0)
    try:
        return int(score)
    except (TypeError, ValueError):
        return 0


def pr_open(*, root: Path, title: str, body: str, base: str = "main", head: str = "") -> dict[str, Any]:
    cmd = ["gh", "pr", "create", "--base", base, "--title", title, "--body", body]
    if head:
        cmd.extend(["--head", head])
    rc, out, err = _run(cmd, cwd=root)
    return {"ok": rc == 0, "command": cmd, "stdout": out.strip(), "stderr": err.strip()}


def pr_wait(*, root: Path, pr: str | None, timeout_sec: int, interval_sec: int = 30) -> dict[str, Any]:
    started = time.time()
    ref = pr or ""
    while True:
        cmd = ["gh", "pr", "checks"]
        if ref:
            cmd.append(ref)
        rc, out, err = _run(cmd, cwd=root)
        text = f"{out}\n{err}".lower()
        if rc == 0 and "fail" not in text and "pending" not in text:
            return {"ok": True, "stdout": out.strip(), "stderr": err.strip(), "elapsed_sec": int(time.time()-started)}
        if time.time() - started >= timeout_sec:
            return {"ok": False, "stdout": out.strip(), "stderr": err.strip(), "elapsed_sec": int(time.time()-started)}
        time.sleep(max(1, interval_sec))


def pr_merge(
    *,
    root: Path,
    pr: str | None,
    health_report_path: Path,
    min_score: int,
    strategy: str = "squash",
) -> dict[str, Any]:
    score = _health_score(health_report_path)
    if score < min_score:
        return {"ok": False, "reason": f"health score {score} < {min_score}", "score": score}

    cmd = ["gh", "pr", "merge"]
    if pr:
        cmd.append(pr)
    if strategy == "merge":
        cmd.append("--merge")
    elif strategy == "rebase":
        cmd.append("--rebase")
    else:
        cmd.append("--squash")
    cmd.append("--auto")
    rc, out, err = _run(cmd, cwd=root)
    return {
        "ok": rc == 0,
        "score": score,
        "command": cmd,
        "stdout": out.strip(),
        "stderr": err.strip(),
    }
