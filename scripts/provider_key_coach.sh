#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$ROOT_DIR/.env.autonomy}"
EXAMPLE_FILE="$ROOT_DIR/.env.autonomy.example"

ensure_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    return
  fi
  if [[ -f "$EXAMPLE_FILE" ]]; then
    cp "$EXAMPLE_FILE" "$ENV_FILE"
  else
    : >"$ENV_FILE"
  fi
}

get_env_value() {
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

set_env_value() {
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

is_missing() {
  local key="$1"
  local value
  value="$(get_env_value "$key" || true)"
  [[ -z "$value" || "$value" == replace_me* ]]
}

coach_one_key() {
  local key="$1"
  local provider="$2"
  local url="$3"
  local hint="$4"

  echo
  echo "Next key: $key ($provider)"
  echo "1) Open: $url"
  echo "2) Create a new API key for $provider."
  echo "3) Paste it here when ready (input hidden)."
  echo "Tip: $hint"
  echo
  read -r -p "Press Enter when ready to paste (or type 'skip'): " ready
  ready_lc="$(printf '%s' "$ready" | tr '[:upper:]' '[:lower:]')"
  if [[ "$ready_lc" == "skip" ]]; then
    echo "Skipped $key."
    return
  fi
  read -r -s -p "Paste $key now: " pasted
  echo
  if [[ -z "$pasted" ]]; then
    echo "No value entered. Leaving $key unchanged."
    return
  fi
  set_env_value "$key" "$pasted"
  echo "Saved $key in $ENV_FILE"
}

ensure_env_file

required_keys=(
  "OPENAI_API_KEY|OpenAI|https://platform.openai.com/api-keys|Keep one active project key with billing enabled."
  "ANTHROPIC_API_KEY|Anthropic|https://console.anthropic.com/settings/keys|Use a workspace key with API access."
  "GEMINI_API_KEY|Google Gemini|https://aistudio.google.com/app/apikey|If using Gemini CLI auth instead, keep this skipped."
)

optional_keys=(
  "OPENROUTER_API_KEY|OpenRouter|https://openrouter.ai/keys|Optional fallback provider for OpenAI-compatible routing."
  "GROQ_API_KEY|Groq|https://console.groq.com/keys|Optional fallback provider for OpenAI-compatible routing."
)

if [[ ! -t 0 ]]; then
  for row in "${required_keys[@]}"; do
    IFS='|' read -r key provider url hint <<<"$row"
    if is_missing "$key"; then
      echo "First missing key: $key ($provider)"
      echo "Open: $url"
      echo "Then run: bash $ROOT_DIR/scripts/provider_key_coach.sh"
      exit 0
    fi
  done
  for row in "${optional_keys[@]}"; do
    IFS='|' read -r key provider url hint <<<"$row"
    if is_missing "$key"; then
      echo "Next optional key: $key ($provider)"
      echo "Open: $url"
      echo "Then run: bash $ROOT_DIR/scripts/provider_key_coach.sh"
      exit 0
    fi
  done
  echo "All required and optional keys already look set in $ENV_FILE."
  exit 0
fi

for row in "${required_keys[@]}"; do
  IFS='|' read -r key provider url hint <<<"$row"
  if is_missing "$key"; then
    coach_one_key "$key" "$provider" "$url" "$hint"
    read -r -p "Continue to the next key? [Y/n]: " ans
    if [[ "${ans:-y}" =~ ^[Nn]$ ]]; then
      echo "Stopping here. Re-run anytime to continue."
      exit 0
    fi
  fi
done

for row in "${optional_keys[@]}"; do
  IFS='|' read -r key provider url hint <<<"$row"
  if is_missing "$key"; then
    echo
    read -r -p "Configure optional key $key now? [Y/n]: " opt
    if [[ "${opt:-y}" =~ ^[Nn]$ ]]; then
      continue
    fi
    coach_one_key "$key" "$provider" "$url" "$hint"
    read -r -p "Continue to the next key? [Y/n]: " ans
    if [[ "${ans:-y}" =~ ^[Nn]$ ]]; then
      echo "Stopping here. Re-run anytime to continue."
      exit 0
    fi
  fi
done

echo
echo "Provider key coaching complete."
echo "You can now run: make -C $ROOT_DIR provider-autobootstrap"
