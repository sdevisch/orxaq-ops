# Claude Architecture Review Lane Objective

Perform a high-rigor architecture/security/ethics review and land improvements.

Scope:
- Governance documentation and controls.
- CI/CD guardrails and release hygiene.
- Security-by-design checks and operator safety boundaries.

Boundary:
- Avoid conflicting with implementation/testing lanes.
- Prefer docs/policy/integration-control files in the lane boundary.

Execution:
- Work fully autonomously.
- Validate with `make lint` and `make test`.
- Commit and push contiguous changes.
