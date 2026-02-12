from __future__ import annotations

import json
from pathlib import Path

from orxaq_autonomy.phase2_core import (
    FileJsonlSink,
    build_event,
    deterministic_lane_randomizer,
    lane_ate,
    read_events,
)


def test_file_jsonl_sink_appends_and_validates(tmp_path: Path) -> None:
    sink = FileJsonlSink(tmp_path / "swarm_events.jsonl")
    event = build_event(
        event_type="task_end",
        work_id="w1",
        task_type="docs",
        risk_level="low",
        lane="L0",
        provider="lmstudio",
        model="local-small",
        status="success",
        duration_ms=120,
    )
    sink.emit(event)
    sink.emit({**event, "event_id": "abc1234567890def", "work_id": "w2"})
    rows = read_events(tmp_path / "swarm_events.jsonl")
    assert len(rows) == 2


def test_lane_randomizer_is_deterministic() -> None:
    a = deterministic_lane_randomizer(work_id="x", task_type="docs", risk_level="low", seed=17)
    b = deterministic_lane_randomizer(work_id="x", task_type="docs", risk_level="low", seed=17)
    assert a == b


def test_lane_ate_smoke() -> None:
    events = [
        {
            "event_id": "1",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event_type": "task_end",
            "work_id": "a",
            "task_type": "docs",
            "risk_level": "low",
            "lane": "L0",
            "provider": "lmstudio",
            "model": "m",
            "status": "success",
            "duration_ms": 100,
        },
        {
            "event_id": "2",
            "timestamp": "2026-01-01T00:00:01+00:00",
            "event_type": "task_end",
            "work_id": "b",
            "task_type": "docs",
            "risk_level": "low",
            "lane": "L2",
            "provider": "cloud",
            "model": "m",
            "status": "failed",
            "duration_ms": 150,
            "cost_usd": 0.02,
        },
    ]
    report = lane_ate(events)
    assert "docs" in report["task_types"]
    assert report["task_types"]["docs"]["n_control"] == 1
