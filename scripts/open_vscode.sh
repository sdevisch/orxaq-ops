#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_FILE="$ROOT_DIR/orxaq-dual-agent.code-workspace"
VSCODE_BIN="/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"

if [[ ! -f "$WORKSPACE_FILE" ]]; then
  "$ROOT_DIR/scripts/generate_workspace.sh"
fi

if [[ -x "$VSCODE_BIN" ]]; then
  "$VSCODE_BIN" "$WORKSPACE_FILE"
  echo "opened with: $VSCODE_BIN"
  exit 0
fi

if command -v open >/dev/null 2>&1; then
  open -a "Visual Studio Code" "$WORKSPACE_FILE"
  echo "opened with: open -a \"Visual Studio Code\""
  exit 0
fi

echo "Visual Studio Code launcher not found."
echo "Expected: $VSCODE_BIN"
exit 1
