#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$ROOT_DIR/.env.autonomy}"
EXAMPLE_FILE="$ROOT_DIR/.env.autonomy.example"

ensure_file() {
  if [[ -f "$ENV_FILE" ]]; then
    return
  fi
  if [[ -f "$EXAMPLE_FILE" ]]; then
    cp "$EXAMPLE_FILE" "$ENV_FILE"
  else
    : >"$ENV_FILE"
  fi
}

upsert_kv() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    {
      if ($0 ~ "^[[:space:]]*(export[[:space:]]+)?" k "=") {
        if (!done) {
          print k "=" v
          done = 1
        }
        next
      }
      print
    }
    END {
      if (!done) print k "=" v
    }
  ' "$ENV_FILE" >"$tmp"
  mv "$tmp" "$ENV_FILE"
}

read_kv() {
  local key="$1"
  awk -F= -v k="$key" '
    $0 ~ "^[[:space:]]*(export[[:space:]]+)?" k "=" {
      v = substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
      print v
      exit
    }
  ' "$ENV_FILE"
}

select_value() {
  local key="$1"
  local placeholder="$2"
  local current
  current="$(read_kv "$key" || true)"
  if [[ -n "$current" && "$current" != replace_me* ]]; then
    printf '%s' "$current"
    return
  fi
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
    return
  fi
  printf '%s' "$placeholder"
}

ensure_file

upsert_kv "OPENAI_API_KEY" "$(select_value OPENAI_API_KEY replace_me_openai)"
upsert_kv "GEMINI_API_KEY" "$(select_value GEMINI_API_KEY replace_me_gemini)"
upsert_kv "ANTHROPIC_API_KEY" "$(select_value ANTHROPIC_API_KEY replace_me_anthropic)"
upsert_kv "OPENROUTER_API_KEY" "$(select_value OPENROUTER_API_KEY replace_me_openrouter)"
upsert_kv "GROQ_API_KEY" "$(select_value GROQ_API_KEY replace_me_groq)"
upsert_kv "ORXAQ_IMPL_REPO" "$(select_value ORXAQ_IMPL_REPO /Users/sdevisch/dev/orxaq)"
upsert_kv "ORXAQ_TEST_REPO" "$(select_value ORXAQ_TEST_REPO /Users/sdevisch/dev/orxaq_gemini)"

echo "Updated: $ENV_FILE"

required_missing=0
for key in OPENAI_API_KEY GEMINI_API_KEY ANTHROPIC_API_KEY; do
  value="$(read_kv "$key" || true)"
  if [[ -z "$value" || "$value" == replace_me* ]]; then
    echo "Missing required key: $key"
    required_missing=$((required_missing + 1))
  fi
done

if [[ $required_missing -gt 0 ]]; then
  echo "Skipping runtime checks until required keys are set."
  exit 0
fi

echo "Running automated checks..."
(
  cd "$ROOT_DIR"
  make preflight-autonomy
  make model-router-connectivity
)

echo "Done. Artifacts:"
echo "- $ROOT_DIR/artifacts/model_connectivity.json"
