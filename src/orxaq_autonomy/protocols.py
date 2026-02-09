"""Reusable protocol definitions for autonomy runtimes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class SkillProtocolSpec:
    """Portable skill contract for agent behavior and recovery expectations."""

    name: str = "orxaq-autonomy"
    version: str = "2"
    description: str = "Reusable autonomy protocol for multi-agent delivery."
    required_behaviors: list[str] = field(
        default_factory=lambda: [
            "work-non-interactively",
            "issue-first-workflow",
            "branch-from-issue",
            "maintain-clean-synced-environment",
            "commit-and-push-regularly",
            "request-cross-model-review",
            "attach-review-evidence",
            "resolve-conflicts-in-pr",
            "avoid-artificial-file-blocks",
            "retry-transient-failures",
            "recover-git-locks",
            "validate-and-report",
            "continue-after-partial-output",
            "respect-non-admin-boundaries",
        ]
    )
    filetype_policy: str = (
        "Preserve unknown/binary file formats, avoid destructive conversions, and add gitattributes when needed."
    )

    @classmethod
    def from_json_file(cls, path: Path) -> "SkillProtocolSpec":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Skill protocol file must be a JSON object: {path}")
        return cls(
            name=str(payload.get("name", cls.name)),
            version=str(payload.get("version", cls.version)),
            description=str(payload.get("description", cls.description)),
            required_behaviors=[str(x) for x in payload.get("required_behaviors", [])] or cls().required_behaviors,
            filetype_policy=str(payload.get("filetype_policy", cls.filetype_policy)),
        )


class ContextProvider(Protocol):
    """Protocol for optional context sources (MCP, files, services)."""

    def render_context(self) -> str: ...


@dataclass(frozen=True)
class MCPContextBundle:
    """Simplified MCP context payload used by prompts."""

    source: str
    snippets: list[str]

    def render_context(self) -> str:
        if not self.snippets:
            return ""
        body = "\n".join(f"- {snippet}" for snippet in self.snippets)
        return f"MCP context ({self.source}):\n{body}"


def load_skill_protocol(path: Path | None) -> SkillProtocolSpec:
    if path is None or not path.exists():
        return SkillProtocolSpec()
    return SkillProtocolSpec.from_json_file(path)


def load_mcp_context(path: Path | None, max_snippets: int = 8, max_chars: int = 240) -> MCPContextBundle | None:
    if path is None or not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    resources = payload.get("resources", []) if isinstance(payload, dict) else []
    snippets: list[str] = []
    for item in resources:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        snippets.append(text[:max_chars])
        if len(snippets) >= max_snippets:
            break
    return MCPContextBundle(source=str(path), snippets=snippets)
