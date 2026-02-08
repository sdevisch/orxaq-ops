#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT_DIR/src:${PYTHONPATH:-}"

cmd="${1:-install}"
case "$cmd" in
  install)
    exec python3 -m orxaq_autonomy.cli --root "$ROOT_DIR" install-keepalive
    ;;
  uninstall)
    exec python3 -m orxaq_autonomy.cli --root "$ROOT_DIR" uninstall-keepalive
    ;;
  status)
    exec python3 -m orxaq_autonomy.cli --root "$ROOT_DIR" keepalive-status
    ;;
  *)
    echo "Usage: scripts/install_keepalive.sh [install|uninstall|status]"
    exit 2
    ;;
esac
