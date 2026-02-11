"""Profile application helpers for local/lan/travel routing modes."""

from __future__ import annotations

from pathlib import Path


VALID_PROFILES = {"local", "lan", "travel"}


def profile_apply(*, root: Path, name: str) -> Path:
    profile = name.strip().lower()
    if profile not in VALID_PROFILES:
        raise ValueError(f"invalid profile: {name}")
    src = root / "profiles" / f"{profile}.yaml"
    if not src.exists():
        raise FileNotFoundError(f"profile file missing: {src}")
    dst = root / "config" / "providers.active.yaml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst
