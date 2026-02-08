#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT_DIR/src:${PYTHONPATH:-}"

exec python3 -m orxaq_autonomy.cli --root "$ROOT_DIR" workspace
