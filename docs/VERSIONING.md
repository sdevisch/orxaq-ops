# Versioning Strategy

`orxaq-autonomy` follows SemVer (`MAJOR.MINOR.PATCH`).

## Bump Policy

1. Patch (`x.y.Z`): bug fixes and low-risk hardening.
2. Minor (`x.Y.0`): most feature and behavior additions.
3. Major (`X.0.0`): intentional breaking changes.

## Automation

- Validate version policy: `make version-check`
- Apply bumps:
  - `make bump-patch`
  - `make bump-minor`
  - `make bump-major`

## Release Tags

- Tags must match project version exactly: `v<version>`.
- Example: if `pyproject.toml` is `0.1.1`, tag must be `v0.1.1`.
