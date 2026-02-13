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
    version: str = "1"
    description: str = "Reusable autonomy protocol for multi-agent delivery."
    required_behaviors: list[str] = field(
        default_factory=lambda: [
            "work-non-interactively",
            "retry-transient-failures",
            "recover-git-locks",
            "validate-and-report",
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


@dataclass(frozen=True)
class SharedInstructionContract:
    """Team preferences for shared instruction contracts across model brands.

    Each field represents a team collaboration preference that applies
    regardless of which AI model brand is executing the work.
    """

    create_issue_first: bool = True
    branch_against_issue: bool = True
    clean_sync_environment: bool = True
    commit_regularly: bool = True
    push_regularly: bool = True
    request_review: bool = True
    include_review_evidence: bool = True
    resolve_merge_conflicts: bool = True

    def as_dict(self) -> dict[str, bool]:
        """Return contract preferences as a plain dictionary."""
        return {
            "create_issue_first": self.create_issue_first,
            "branch_against_issue": self.branch_against_issue,
            "clean_sync_environment": self.clean_sync_environment,
            "commit_regularly": self.commit_regularly,
            "push_regularly": self.push_regularly,
            "request_review": self.request_review,
            "include_review_evidence": self.include_review_evidence,
            "resolve_merge_conflicts": self.resolve_merge_conflicts,
        }


def load_shared_contract() -> SharedInstructionContract:
    """Return the default shared instruction contract with all preferences enabled."""
    return SharedInstructionContract()


_BRAND_PROMPT_FILES: dict[str, str] = {
    "claude": "swarm_claude_session_prompt.md",
    "codex": "swarm_codex_session_prompt.md",
    "gemini": "swarm_gemini_session_prompt.md",
}


def compose_startup_prompt(
    brand: str,
    contract: SharedInstructionContract,
    *,
    prompts_dir: Path | None = None,
) -> str:
    """Merge a shared instruction contract with brand-specific startup instructions.

    Parameters
    ----------
    brand:
        Model brand identifier (e.g. ``"claude"``, ``"codex"``, ``"gemini"``).
    contract:
        The shared team preferences contract.
    prompts_dir:
        Optional directory containing prompt markdown files.  When *None*,
        falls back to ``config/prompts/`` relative to the project root.

    Returns
    -------
    str
        Combined startup prompt with shared contract section followed by
        brand-specific instructions.
    """
    # Build shared contract section
    lines = ["## Shared Team Contract\n"]
    for key, value in contract.as_dict().items():
        label = key.replace("_", " ").title()
        status = "REQUIRED" if value else "OPTIONAL"
        lines.append(f"- **{label}**: {status}")
    shared_section = "\n".join(lines)

    # Load brand-specific prompt if available
    brand_section = ""
    filename = _BRAND_PROMPT_FILES.get(brand.lower())
    if filename:
        if prompts_dir is None:
            # Default: config/prompts/ relative to project root
            prompts_dir = Path(__file__).resolve().parents[2] / "config" / "prompts"
        brand_path = prompts_dir / filename
        if brand_path.exists():
            brand_section = brand_path.read_text(encoding="utf-8").strip()

    parts = [shared_section]
    if brand_section:
        parts.append(brand_section)
    return "\n\n".join(parts)
