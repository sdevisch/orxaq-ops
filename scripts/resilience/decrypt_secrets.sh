#!/usr/bin/env bash
# decrypt_secrets.sh — Restore secrets from encrypted iCloud vault
set -euo pipefail

TELEMETRY_SCRIPT="decrypt_secrets"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/telemetry.sh"

VAULT_DIR="${HOME}/Library/Mobile Documents/com~apple~CloudDocs/orxaq-vault"
KEYCHAIN_SERVICE="com.orxaq.age-key"
KEYCHAIN_ACCOUNT="orxaq-secrets"
DEV_DIR="${HOME}/dev"

if [[ ! -d "${VAULT_DIR}" ]]; then
    emit_event "vault_missing" "error" path="${VAULT_DIR}"
    exit 1
fi

# Retrieve age key from Keychain
start_timer "keychain_lookup"
SECRET_KEY=$(security find-generic-password -s "${KEYCHAIN_SERVICE}" -a "${KEYCHAIN_ACCOUNT}" -w 2>/dev/null || true)
if [[ -z "${SECRET_KEY}" ]]; then
    emit_event "keychain_missing" "error" service="${KEYCHAIN_SERVICE}"
    end_timer "keychain_lookup" "error"
    exit 1
fi
end_timer "keychain_lookup" "ok"

# Decrypt .env files into repos
start_timer "decryption"
DECRYPTED_COUNT=0
SKIPPED_COUNT=0

for repo_dir in "${VAULT_DIR}/secrets"/*/; do
    repo_name=$(basename "${repo_dir}")
    target="${DEV_DIR}/${repo_name}"

    if [[ ! -d "${target}" ]]; then
        emit_event "repo_skip" "info" repo="${repo_name}" reason="not_cloned"
        ((SKIPPED_COUNT++))
        continue
    fi

    for encrypted in "${repo_dir}"*.age; do
        if [[ -f "${encrypted}" ]]; then
            dest_name=$(basename "${encrypted}" .age)
            echo "${SECRET_KEY}" | age -d -i - "${encrypted}" > "${target}/${dest_name}"
            chmod 600 "${target}/${dest_name}"
            ((DECRYPTED_COUNT++))
            emit_event "file_decrypted" "ok" repo="${repo_name}" file="${dest_name}"
        fi
    done
done

emit_metric "files_decrypted" "${DECRYPTED_COUNT}"
emit_metric "repos_skipped" "${SKIPPED_COUNT}"
end_timer "decryption" "ok" decrypted="${DECRYPTED_COUNT}"

# Restore Claude memory
start_timer "memory_restore"
CLAUDE_MEM="${HOME}/.claude/projects/-Users-sdevisch-dev/memory"
mkdir -p "${CLAUDE_MEM}"
MEMORY_RESTORED=0
if [[ -d "${VAULT_DIR}/claude-memory" ]]; then
    for f in "${VAULT_DIR}/claude-memory"/*.md; do
        if [[ -f "$f" ]]; then
            cp "$f" "${CLAUDE_MEM}/$(basename "$f")"
            ((MEMORY_RESTORED++))
        fi
    done
fi
emit_metric "memory_files_restored" "${MEMORY_RESTORED}"

# Restore CLAUDE.md
if [[ -f "${VAULT_DIR}/CLAUDE.md" ]]; then
    mkdir -p "${DEV_DIR}/.claude"
    cp "${VAULT_DIR}/CLAUDE.md" "${DEV_DIR}/.claude/CLAUDE.md"
    emit_event "claude_md_restored" "ok"
fi

# Restore agents
AGENTS_RESTORED=0
if [[ -d "${VAULT_DIR}/agents" ]]; then
    mkdir -p "${DEV_DIR}/.claude/agents"
    for f in "${VAULT_DIR}/agents"/*.md; do
        if [[ -f "$f" ]]; then
            cp "$f" "${DEV_DIR}/.claude/agents/"
            ((AGENTS_RESTORED++))
        fi
    done
fi
emit_metric "agents_restored" "${AGENTS_RESTORED}"
end_timer "memory_restore" "ok" memory="${MEMORY_RESTORED}" agents="${AGENTS_RESTORED}"
