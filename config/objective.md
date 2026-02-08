# Autonomous Build Objective

Deliver a production-grade Orxaq platform (core + causal + mesh + rlm/rln + rpa + cli) for
high-fidelity proprietary-context AI on Windows user-space (no admin required), with strong
security, ethics, and test coverage.

## Operating Rules

- Do not wait for user approval between normal tasks.
- Always pick and execute the next highest-impact task.
- Implement code, tests, benchmarks, docs, and CI updates as needed.
- Validate after each batch using `make lint` and `make test`.
- Continue automatically after each report.
- Stop only for:
  - missing credentials or external resources,
  - destructive or irreversible actions,
  - true product tradeoffs that require human decision.

## Done Criteria

- All modules production-ready with measurable quality gates.
- End-to-end tests proving value vs baseline, including compaction/detail-retention benchmarks.
- Security and ethics requirements explicitly documented and tested.
- Repository passes lint/tests cleanly and is ready to ship.
