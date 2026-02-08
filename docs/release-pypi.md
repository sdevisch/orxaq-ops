# PyPI Release Runbook

## 1) One-time repository setup

1. In PyPI, create project `orxaq-autonomy` (if not already created).
2. Configure Trusted Publisher:
- Owner: your GitHub org/user
- Repository: `orxaq-ops` repo
- Workflow: `.github/workflows/release-pypi.yml`
- Environment: `pypi`
3. In GitHub repository settings, create environment `pypi`.

## 2) Validate locally

```bash
cd /Users/sdevisch/dev/orxaq-ops
make lint
make test
make version-check
make repo-hygiene
make package
```

## 3) Cut a release

```bash
make bump-patch   # or bump-minor / bump-major
git commit -am "Bump version for release"
git tag vX.Y.Z
git push origin vX.Y.Z
```

The `Publish to PyPI` workflow builds distributions and publishes them through OIDC trusted publishing.

## 4) Verify

- Check workflow success in GitHub Actions.
- Confirm package appears on PyPI.
- Optionally install and smoke test:

```bash
python3 -m pip install orxaq-autonomy
orxaq-autonomy --help
```
