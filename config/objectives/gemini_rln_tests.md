# Gemini RLN Tests Lane Objective

Deliver independent, adversarial RLN test coverage in the test repository.

Scope:
- Stress compaction/detail-retention limits under small context windows.
- Add baseline-vs-RLN assertions and benchmark fixtures.
- Focus on tests/benchmarks/specs; avoid broad production edits.

Boundary:
- Operate in dedicated test paths only.
- Keep implementation edits minimal and only when necessary for executable tests.

Execution:
- Work fully autonomously.
- Validate with `make lint` and `make test`.
- Commit and push contiguous changes.
