#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_FILE="$ROOT_DIR/orxaq-dual-agent.code-workspace"
IMPL_REPO="${ORXAQ_IMPL_REPO:-$ROOT_DIR/../orxaq}"
TEST_REPO="${ORXAQ_TEST_REPO:-$ROOT_DIR/../orxaq_gemini}"

cat >"$WORKSPACE_FILE" <<EOF
{
  "folders": [
    { "name": "orxaq", "path": "$IMPL_REPO" },
    { "name": "orxaq_gemini", "path": "$TEST_REPO" },
    { "name": "orxaq-ops", "path": "$ROOT_DIR" }
  ],
  "settings": {
    "terminal.integrated.defaultProfile.osx": "zsh",
    "files.trimTrailingWhitespace": true,
    "files.insertFinalNewline": true
  }
}
EOF

echo "wrote $WORKSPACE_FILE"
