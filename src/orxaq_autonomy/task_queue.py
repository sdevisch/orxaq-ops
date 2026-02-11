"""Task queue schema validation and checkpoint persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_KEYS = {"id", "owner", "priority", "title", "description"}
VALID_OWNERS = {"codex", "gemini"}


def validate_task_queue_payload(payload: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, list):
        return ["task queue must be a list"]
    seen_ids: set[str] = set()
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            errors.append(f"task[{idx}] must be an object")
            continue
        missing = REQUIRED_KEYS - set(item.keys())
        if missing:
            errors.append(f"task[{idx}] missing fields: {', '.join(sorted(missing))}")
        task_id = str(item.get("id", "")).strip()
        if not task_id:
            errors.append(f"task[{idx}] id must be non-empty")
        elif task_id in seen_ids:
            errors.append(f"duplicate task id: {task_id}")
        else:
            seen_ids.add(task_id)
        owner = str(item.get("owner", "")).strip().lower()
        if owner not in VALID_OWNERS:
            errors.append(f"task[{idx}] owner must be one of: codex, gemini")
        try:
            priority = int(item.get("priority", -1))
            if priority < 0:
                errors.append(f"task[{idx}] priority must be >= 0")
        except (TypeError, ValueError):
            errors.append(f"task[{idx}] priority must be integer")
    return errors


def validate_task_queue_file(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001
        return [f"invalid JSON: {err}"]
    return validate_task_queue_payload(payload)


def write_checkpoint(*, path: Path, run_id: str, cycle: int, state: dict[str, Any]) -> None:
    payload = {
        "run_id": run_id,
        "cycle": int(cycle),
        "state": state,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_checkpoint(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("checkpoint payload must be object")
    state = payload.get("state")
    if not isinstance(state, dict):
        raise ValueError("checkpoint payload missing state")
    return payload
