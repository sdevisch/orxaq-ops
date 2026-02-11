# Local Dashboard

`orxaq-autonomy dashboard` provides a local-only artifact browser for swarm run outputs.

## Start

```bash
python3 -m orxaq_autonomy.cli --root . dashboard --artifacts-dir ./artifacts --host 127.0.0.1 --port 8787
# or
make dashboard
```

Open: `http://127.0.0.1:8787/`

## What It Shows

- `health.json` and `health.md` files
- `W*_run.json` and `W*_summary.md` reports
- RPA evidence directories/files under `artifacts/rpa_evidence/`
- JSON index endpoint: `/api/index`

## Security Model

- Binds to localhost by default.
- Serves files only from the configured artifacts directory.
- Rejects path traversal attempts outside the artifacts root.
