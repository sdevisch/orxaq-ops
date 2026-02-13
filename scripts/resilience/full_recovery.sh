#!/usr/bin/env bash
# full_recovery.sh — One-command full recovery on a fresh Mac
set -euo pipefail

VAULT_DIR="${HOME}/Library/Mobile Documents/com~apple~CloudDocs/orxaq-vault"
DEV_DIR="${HOME}/dev"
LOG_DIR="${DEV_DIR}/.claude/resilience/logs"

mkdir -p "${LOG_DIR}"

# Minimal logging before telemetry is available
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "${LOG_DIR}/recovery.log"; }

log "=== ORXAQ FULL RECOVERY STARTED ==="

# Step 1: Wait for iCloud sync
if [[ ! -d "${VAULT_DIR}" ]]; then
    log "Waiting for iCloud vault to sync..."
    for i in $(seq 1 60); do
        if [[ -d "${VAULT_DIR}" ]]; then
            break
        fi
        sleep 5
    done
    if [[ ! -d "${VAULT_DIR}" ]]; then
        log "TIMEOUT: iCloud vault not found after 5 minutes."
    fi
fi

# Step 2: Ensure Homebrew + git
if ! command -v brew &>/dev/null; then
    log "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
fi

if ! command -v gh &>/dev/null; then
    brew install gh
fi

# Step 3: Set up resilience scripts from vault
mkdir -p "${DEV_DIR}/.claude/resilience"

if [[ -d "${VAULT_DIR}" ]]; then
    for script in bootstrap_fresh_mac.sh decrypt_secrets.sh encrypt_secrets.sh backup.sh restore.sh telemetry.sh healthcheck.sh; do
        if [[ -f "${VAULT_DIR}/${script}" ]]; then
            cp "${VAULT_DIR}/${script}" "${DEV_DIR}/.claude/resilience/${script}"
            log "Copied: ${script}"
        fi
    done
    chmod +x "${DEV_DIR}/.claude/resilience"/*.sh 2>/dev/null || true
fi

# Now source telemetry if available
TELEMETRY_SCRIPT="full_recovery"
if [[ -f "${DEV_DIR}/.claude/resilience/telemetry.sh" ]]; then
    source "${DEV_DIR}/.claude/resilience/telemetry.sh"
fi

start_timer "full_recovery"

# Step 4: Try cloud restore first
RESTORED=false
if [[ -f "${DEV_DIR}/.claude/resilience/restore.sh" ]]; then
    log "Attempting cloud restore..."
    if bash "${DEV_DIR}/.claude/resilience/restore.sh" 2>/dev/null; then
        RESTORED=true
        emit_event "cloud_restore_success" "ok"
    else
        emit_event "cloud_restore_failed" "warn" reason="falling_back_to_bootstrap"
    fi
fi

# Step 5: Run bootstrap
BOOTSTRAP="${DEV_DIR}/.claude/resilience/bootstrap_fresh_mac.sh"
if [[ -f "${BOOTSTRAP}" ]]; then
    start_timer "bootstrap"
    bash "${BOOTSTRAP}"
    end_timer "bootstrap" "ok"
else
    emit_event "bootstrap_missing" "error"
    log "Bootstrap script not found. Clone orxaq-ops first."
    exit 1
fi

end_timer "full_recovery" "ok" cloud_restored="${RESTORED}"
log "=== RECOVERY COMPLETE ==="
