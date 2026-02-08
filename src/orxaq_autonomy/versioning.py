"""SemVer helpers for orxaq-autonomy."""

from __future__ import annotations

import re
from pathlib import Path

try:  # pragma: no cover - exercised on Python <3.11 only
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python <3.11 only
    import tomli as tomllib  # type: ignore[no-redef]


SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:(a|b|rc)(0|[1-9]\d*))?(?:\.post(0|[1-9]\d*))?(?:\.dev(0|[1-9]\d*))?$"
)
SEMVER_CORE_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def load_project_version(pyproject_path: Path) -> str:
    parsed = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = parsed.get("project")
    if not isinstance(project, dict):
        raise ValueError("Missing [project] table in pyproject.toml.")
    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Missing non-empty [project].version in pyproject.toml.")
    return version.strip()


def validate_semver(version: str) -> list[str]:
    errors: list[str] = []
    if not SEMVER_PATTERN.match(version):
        errors.append(
            f"Version '{version}' violates policy. Expected SemVer 'MAJOR.MINOR.PATCH'."
        )
    return errors


def validate_release_tag(version: str, tag: str | None) -> list[str]:
    if not tag:
        return []
    errors: list[str] = []
    expected = f"v{version}"
    if tag != expected:
        errors.append(f"Release tag '{tag}' must match project version '{expected}'.")
    if not re.match(
        r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:(a|b|rc)(0|[1-9]\d*))?(?:\.post(0|[1-9]\d*))?(?:\.dev(0|[1-9]\d*))?$",
        tag,
    ):
        errors.append(
            f"Release tag '{tag}' violates policy. Expected SemVer tag 'vMAJOR.MINOR.PATCH'."
        )
    return errors


def bump_version(version: str, part: str) -> str:
    match = SEMVER_CORE_PATTERN.match(version)
    if not match:
        raise ValueError(f"Version '{version}' is not a SemVer core value.")
    major, minor, patch = (int(piece) for piece in match.groups())
    if part == "patch":
        patch += 1
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        raise ValueError(f"Unsupported bump part '{part}'.")
    return f"{major}.{minor}.{patch}"
