#!/usr/bin/env bash
# restore.sh — Restore from cloud backup to a fresh or rebuilt machine
set -euo pipefail

TELEMETRY_SCRIPT="restore"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/telemetry.sh"

KEYCHAIN_SERVICE="com.orxaq.restic-password"
KEYCHAIN_ACCOUNT="orxaq-backup"
DEV_DIR="${HOME}/dev"

start_timer "restore"

# --- Get password ---
export RESTIC_PASSWORD=$(security find-generic-password -s "${KEYCHAIN_SERVICE}" -a "${KEYCHAIN_ACCOUNT}" -w 2>/dev/null || true)
if [[ -z "${RESTIC_PASSWORD}" ]]; then
    emit_event "password_missing" "warn" source="keychain"
    read -rsp "Enter backup password manually: " RESTIC_PASSWORD
    echo ""
    export RESTIC_PASSWORD
fi

# --- Select source ---
BACKUP_ENV="${DEV_DIR}/.claude/resilience/.backup.env"
S3_REPO=""
GCS_REPO=""
if [[ -f "${BACKUP_ENV}" ]]; then
    source "${BACKUP_ENV}"
    S3_REPO="${RESTIC_REPOSITORY_S3:-}"
    GCS_REPO="${RESTIC_REPOSITORY_GCS:-}"
fi

REPO="${S3_REPO}"
REPO_NAME="aws-s3"
if [[ -z "${REPO}" ]]; then
    REPO="${GCS_REPO}"
    REPO_NAME="gcs"
fi

if [[ -z "${REPO}" ]]; then
    emit_event "no_backup_repo" "error"
    exit 1
fi

export RESTIC_REPOSITORY="${REPO}"
emit_event "restore_source" "info" target="${REPO_NAME}" repo="${REPO}"

# --- List available snapshots ---
restic snapshots --tag orxaq --latest 5

# --- Restore latest ---
start_timer "restic_restore"
restic restore latest \
    --target / \
    --tag orxaq \
    --include "${DEV_DIR}" \
    --include "${HOME}/.claude"
end_timer "restic_restore" "ok" source="${REPO_NAME}"

end_timer "restore" "ok"
