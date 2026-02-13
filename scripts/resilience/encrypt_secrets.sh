#!/usr/bin/env bash
# encrypt_secrets.sh — Encrypt all .env and secret files with age
# The age key is stored in macOS Keychain (survives iCloud restore)
set -euo pipefail

TELEMETRY_SCRIPT="encrypt_secrets"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/telemetry.sh"

VAULT_DIR="${HOME}/Library/Mobile Documents/com~apple~CloudDocs/orxaq-vault"
KEYCHAIN_SERVICE="com.orxaq.age-key"
KEYCHAIN_ACCOUNT="orxaq-secrets"

mkdir -p "${VAULT_DIR}"

# --- Key Management ---

get_or_create_key() {
    local existing
    existing=$(security find-generic-password -s "${KEYCHAIN_SERVICE}" -a "${KEYCHAIN_ACCOUNT}" -w 2>/dev/null || true)
    if [[ -n "${existing}" ]]; then
        emit_event "key_found" "ok" source="keychain"
        echo "${existing}"
        return
    fi
    emit_event "key_generate" "info" reason="no_existing_key"
    local key_output
    key_output=$(age-keygen 2>&1)
    local secret_key
    secret_key=$(echo "${key_output}" | grep "AGE-SECRET-KEY")
    local public_key
    public_key=$(echo "${key_output}" | grep "public key:" | awk '{print $NF}')

    security add-generic-password \
        -s "${KEYCHAIN_SERVICE}" \
        -a "${KEYCHAIN_ACCOUNT}" \
        -w "${secret_key}" \
        -T "" \
        -U 2>/dev/null || true

    echo "${public_key}" > "${VAULT_DIR}/age-public-key.txt"
    emit_event "key_created" "ok" public_key="${public_key}"
    echo "${secret_key}"
}

ENCRYPTED_COUNT=0
SKIPPED_COUNT=0

encrypt_file() {
    local src="$1"
    local dest="$2"
    local public_key="$3"

    if [[ ! -f "${src}" ]]; then
        ((SKIPPED_COUNT++))
        return
    fi
    local src_size
    src_size=$(wc -c < "${src}" | tr -d '[:space:]')
    age -r "${public_key}" -o "${dest}" "${src}"
    ((ENCRYPTED_COUNT++))
    emit_event "file_encrypted" "ok" file="$(basename "${src}")" size_bytes="${src_size}"
}

# --- Main ---

start_timer "encryption"

SECRET_KEY=$(get_or_create_key)
PUBLIC_KEY=$(echo "${SECRET_KEY}" | age-keygen -y 2>/dev/null || cat "${VAULT_DIR}/age-public-key.txt")

# Encrypt .env files from all repos
DEV_DIR="${HOME}/dev"
REPOS=(
    "${DEV_DIR}/orxaq"
    "${DEV_DIR}/orxaq-ops"
    "${DEV_DIR}/orxaq-pay"
    "${DEV_DIR}/swarm-orchestrator"
    "${DEV_DIR}/odyssey"
)

for repo in "${REPOS[@]}"; do
    repo_name=$(basename "${repo}")
    mkdir -p "${VAULT_DIR}/secrets/${repo_name}"

    for env_file in "${repo}"/.env "${repo}"/.env.* ; do
        if [[ -f "${env_file}" && "$(basename "${env_file}")" != ".env.example" ]]; then
            encrypt_file "${env_file}" \
                "${VAULT_DIR}/secrets/${repo_name}/$(basename "${env_file}").age" \
                "${PUBLIC_KEY}"
        fi
    done
done

# Sync Claude memory files
mkdir -p "${VAULT_DIR}/claude-memory"
CLAUDE_MEM="${HOME}/.claude/projects/-Users-sdevisch-dev/memory"
MEMORY_FILES_SYNCED=0
if [[ -d "${CLAUDE_MEM}" ]]; then
    for f in "${CLAUDE_MEM}"/*.md; do
        if [[ -f "$f" ]]; then
            cp "$f" "${VAULT_DIR}/claude-memory/$(basename "$f")"
            ((MEMORY_FILES_SYNCED++))
        fi
    done
fi
emit_metric "memory_files_synced" "${MEMORY_FILES_SYNCED}"

# Copy CLAUDE.md
if [[ -f "${DEV_DIR}/.claude/CLAUDE.md" ]]; then
    cp "${DEV_DIR}/.claude/CLAUDE.md" "${VAULT_DIR}/CLAUDE.md"
    emit_event "claude_md_synced" "ok"
fi

# Copy agent definitions
mkdir -p "${VAULT_DIR}/agents"
AGENTS_SYNCED=0
if [[ -d "${DEV_DIR}/.claude/agents" ]]; then
    for f in "${DEV_DIR}/.claude/agents"/*.md; do
        if [[ -f "$f" ]]; then
            cp "$f" "${VAULT_DIR}/agents/"
            ((AGENTS_SYNCED++))
        fi
    done
fi
emit_metric "agents_synced" "${AGENTS_SYNCED}"

# Store repo manifest
cat > "${VAULT_DIR}/repos.json" << 'MANIFEST'
{
    "repos": [
        {"name": "orxaq", "github": "Orxaq/orxaq", "visibility": "private"},
        {"name": "orxaq-ops", "github": "Orxaq/orxaq-ops", "visibility": "public"},
        {"name": "orxaq-pay", "github": "Orxaq/orxaq-pay", "visibility": "private"},
        {"name": "swarm-orchestrator", "github": "Orxaq/swarm-orchestrator", "visibility": "private"},
        {"name": "odyssey", "github": "Orxaq/odyssey", "visibility": "private"}
    ],
    "python_version": "3.14",
    "tools": ["restic", "age", "gh", "ruff"],
    "launchagents": [
        "com.orxaq.resilience.backup",
        "com.orxaq.resilience.healthcheck"
    ]
}
MANIFEST

date -u +"%Y-%m-%dT%H:%M:%SZ" > "${VAULT_DIR}/last_encrypted.txt"

emit_metric "files_encrypted" "${ENCRYPTED_COUNT}"
emit_metric "files_skipped" "${SKIPPED_COUNT}"
emit_disk_usage "${VAULT_DIR}" "vault"

end_timer "encryption" "ok" encrypted="${ENCRYPTED_COUNT}" skipped="${SKIPPED_COUNT}"
