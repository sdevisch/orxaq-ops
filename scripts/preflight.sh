#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ORXAQ_AUTONOMY_ENV_FILE:-$ROOT_DIR/.env.autonomy}"
IMPL_REPO="${ORXAQ_IMPL_REPO:-$ROOT_DIR/../orxaq}"
TEST_REPO="${ORXAQ_TEST_REPO:-$ROOT_DIR/../orxaq_gemini}"
GEMINI_SETTINGS="$HOME/.gemini/settings.json"

load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$ENV_FILE"
    set +a
  fi
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "missing command: $cmd"
    exit 1
  }
}

repo_must_be_clean() {
  local repo="$1"
  if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "not a git repo: $repo"
    exit 1
  fi
  if ! git -C "$repo" diff --quiet; then
    echo "unstaged changes in $repo"
    exit 1
  fi
  if ! git -C "$repo" diff --cached --quiet; then
    echo "staged changes in $repo"
    exit 1
  fi
  if [[ -n "$(git -C "$repo" ls-files --others --exclude-standard)" ]]; then
    echo "untracked files in $repo"
    exit 1
  fi
}

check_codex_auth() {
  if codex login status >/dev/null 2>&1; then
    return
  fi
  if [[ -n "${OPENAI_API_KEY:-}" && "${OPENAI_API_KEY:-}" != "replace_me" ]]; then
    return
  fi
  echo "codex auth missing (run: codex login)"
  exit 1
}

check_gemini_auth_source() {
  if [[ -n "${GEMINI_API_KEY:-}" && "${GEMINI_API_KEY:-}" != "replace_me" ]]; then
    return
  fi
  if [[ "${GOOGLE_GENAI_USE_VERTEXAI:-}" == "true" || "${GOOGLE_GENAI_USE_GCA:-}" == "true" ]]; then
    return
  fi
  if [[ -f "$GEMINI_SETTINGS" ]] && grep -q '"selectedType"' "$GEMINI_SETTINGS"; then
    return
  fi
  echo "gemini auth source missing (.env or ~/.gemini/settings.json)"
  exit 1
}

check_gemini_runtime() {
  local out
  if ! out="$(gemini -p "Return exactly: ok" --output-format text 2>&1)"; then
    echo "gemini runtime check failed"
    echo "$out"
    exit 1
  fi
  if ! printf "%s" "$out" | grep -qi '\bok\b'; then
    echo "gemini runtime check did not return expected token"
    echo "$out"
    exit 1
  fi
}

main() {
  load_env

  require_cmd git
  require_cmd codex
  require_cmd gemini
  require_cmd code

  [[ -d "$IMPL_REPO" ]] || { echo "missing impl repo: $IMPL_REPO"; exit 1; }
  [[ -d "$TEST_REPO" ]] || { echo "missing test repo: $TEST_REPO"; exit 1; }

  repo_must_be_clean "$IMPL_REPO"
  repo_must_be_clean "$TEST_REPO"

  check_codex_auth
  check_gemini_auth_source
  check_gemini_runtime

  echo "preflight ok"
  echo "impl repo: $IMPL_REPO"
  echo "test repo: $TEST_REPO"
}

main "$@"
