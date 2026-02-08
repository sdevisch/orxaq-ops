# Gemini RLN Tests Lane Objective

Deliver independent, adversarial test coverage across RLN, causal, mesh/CLI/RPA, and security/ethics in the test repository.

Scope:
- Stress compaction/detail-retention limits under small context windows.
- Add baseline-vs-RLN assertions and benchmark fixtures.
- Expand causal and orchestration regression coverage.
- Focus on tests/benchmarks/specs; avoid broad production edits.

Boundary:
- Operate in dedicated test paths only.
- Keep implementation edits minimal and only when necessary for executable tests.
- When issues are found, provide actionable Codex/OpenAI fix feedback with likely root cause and hints.

Execution:
- Work fully autonomously.
- Validate with `make lint` and `make test`.
- Commit and push contiguous changes.
