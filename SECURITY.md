# Security Policy

## Reporting a Vulnerability

Do not disclose vulnerabilities in public issues.

Report privately to project maintainers with:

1. Affected component and version.
2. Reproduction steps or proof of concept.
3. Impact and suggested mitigation.

## Scope

- `src/orxaq_autonomy/*`
- runtime scripts in `/Users/sdevisch/dev/orxaq-ops/scripts`
- CI/release workflows in `/Users/sdevisch/dev/orxaq-ops/.github/workflows`

## Security Expectations

- No secrets in source control.
- Least-privilege, user-space automation by default.
- Non-interactive subprocess execution for safe unattended operation.
