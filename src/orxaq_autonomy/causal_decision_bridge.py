"""Causal metadata policy gate for disruptive coordinator interventions."""

from __future__ import annotations

import os
from typing import Any


DISRUPTIVE_ACTIONS = {"restart_many", "isolate_node"}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def should_require_causal_metadata(action: str, requested_lane: str) -> bool:
    normalized_action = str(action).strip().lower()
    normalized_lane = str(requested_lane).strip().lower()
    if normalized_action in DISRUPTIVE_ACTIONS:
        return True
    return normalized_action == "scale_down" and normalized_lane in {"all", "all_enabled"}


def enforce_causal_metadata_gate(
    *,
    action: str,
    requested_lane: str,
    causal_hypothesis_id: str,
) -> dict[str, Any]:
    required = should_require_causal_metadata(action, requested_lane)
    mode = str(os.environ.get("ORXAQ_AUTONOMY_CAUSAL_GATE_MODE", "advisory")).strip().lower() or "advisory"
    if mode not in {"advisory", "enforced"}:
        mode = "advisory"
    hypothesis = str(causal_hypothesis_id).strip()
    has_metadata = bool(hypothesis)
    if not required:
        return {
            "required": False,
            "mode": mode,
            "allowed": True,
            "status": "not_required",
            "evidence_summary": "non_disruptive_action",
        }
    if has_metadata:
        return {
            "required": True,
            "mode": mode,
            "allowed": True,
            "status": "accepted",
            "evidence_summary": f"causal_hypothesis_id={hypothesis}",
        }
    allowed = mode != "enforced"
    return {
        "required": True,
        "mode": mode,
        "allowed": allowed,
        "status": "missing_hypothesis_advisory" if allowed else "missing_hypothesis_rejected",
        "evidence_summary": "missing_causal_hypothesis_id",
    }
