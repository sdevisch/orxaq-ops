---
name: orxaq-autonomy-agent
description: Run resilient autonomous software-delivery cycles with protocol-driven prompts, optional MCP context injection, and cross-platform non-admin supervision.
---

# Orxaq Autonomy Agent

Use this skill when the task requires long-running autonomous delivery with resilient restart behavior, reproducible validation, and minimal human nudges.

## Workflow

1. Initialize protocol and context files if missing:
- `python3 -m orxaq_autonomy.cli --root /path/to/orxaq-ops init-skill-protocol`
- Copy `config/mcp_context.example.json` to a project-specific file when MCP context is needed.

2. Preflight runtime:
- `python3 -m orxaq_autonomy.cli --root /path/to/orxaq-ops preflight --allow-dirty`

3. Start supervisor:
- `python3 -m orxaq_autonomy.cli --root /path/to/orxaq-ops start`

4. Self-heal periodically:
- `python3 -m orxaq_autonomy.cli --root /path/to/orxaq-ops ensure`

5. Observe and iterate:
- `python3 -m orxaq_autonomy.cli --root /path/to/orxaq-ops status`
- `python3 -m orxaq_autonomy.cli --root /path/to/orxaq-ops logs`

## Guardrails

- Non-interactive only. Avoid commands that require TTY prompts.
- Preserve unknown/binary file types; avoid destructive rewrites.
- Respect non-admin execution constraints (especially on Windows).
- Retry transient failures before declaring blocks.

## Keepalive

- macOS: `python3 -m orxaq_autonomy.cli --root /path/to/orxaq-ops install-keepalive`
- Windows: same command installs a user-space Task Scheduler entry.
