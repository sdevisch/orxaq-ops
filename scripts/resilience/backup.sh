#!/usr/bin/env bash
# backup.sh — Encrypted incremental backup to two cloud failovers
# Failover 1: AWS S3 (via restic)
# Failover 2: Google Cloud Storage (via restic)
set -euo pipefail

TELEMETRY_SCRIPT="backup"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/telemetry.sh"

KEYCHAIN_SERVICE="com.orxaq.restic-password"
KEYCHAIN_ACCOUNT="orxaq-backup"
DEV_DIR="${HOME}/dev"
LOG_DIR="${DEV_DIR}/.claude/resilience/logs"
LOCK_FILE="/tmp/orxaq-backup.lock"

mkdir -p "${LOG_DIR}"

# --- Locking ---
if [[ -f "${LOCK_FILE}" ]]; then
    pid=$(cat "${LOCK_FILE}")
    if kill -0 "${pid}" 2>/dev/null; then
        emit_event "backup_skipped" "info" reason="already_running" pid="${pid}"
        exit 0
    fi
    rm -f "${LOCK_FILE}"
fi
echo $$ > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"' EXIT

# --- Password Management ---
get_or_create_password() {
    local existing
    existing=$(security find-generic-password -s "${KEYCHAIN_SERVICE}" -a "${KEYCHAIN_ACCOUNT}" -w 2>/dev/null || true)
    if [[ -n "${existing}" ]]; then
        emit_event "password_found" "ok" source="keychain"
        echo "${existing}"
        return
    fi
    local password
    password=$(openssl rand -base64 32)
    security add-generic-password \
        -s "${KEYCHAIN_SERVICE}" \
        -a "${KEYCHAIN_ACCOUNT}" \
        -w "${password}" \
        -T "" \
        -U 2>/dev/null || true
    emit_event "password_created" "ok"
    echo "${password}"
}

export RESTIC_PASSWORD=$(get_or_create_password)

# --- Backup Targets ---
S3_REPO="${RESTIC_REPOSITORY_S3:-}"
GCS_REPO="${RESTIC_REPOSITORY_GCS:-}"

BACKUP_ENV="${DEV_DIR}/.claude/resilience/.backup.env"
if [[ -f "${BACKUP_ENV}" ]]; then
    source "${BACKUP_ENV}"
    S3_REPO="${RESTIC_REPOSITORY_S3:-${S3_REPO}}"
    GCS_REPO="${RESTIC_REPOSITORY_GCS:-${GCS_REPO}}"
fi

# --- What to back up ---
INCLUDE_PATHS=(
    "${DEV_DIR}/orxaq"
    "${DEV_DIR}/orxaq-ops"
    "${DEV_DIR}/orxaq-pay"
    "${DEV_DIR}/swarm-orchestrator"
    "${DEV_DIR}/odyssey"
    "${DEV_DIR}/.claude"
    "${HOME}/.claude/projects/-Users-sdevisch-dev/memory"
)

EXCLUDE_PATTERNS=(
    "--exclude=.venv"
    "--exclude=__pycache__"
    "--exclude=*.pyc"
    "--exclude=node_modules"
    "--exclude=.git"
    "--exclude=*.egg-info"
    "--exclude=dist"
    "--exclude=build"
    "--exclude=.mypy_cache"
    "--exclude=.ruff_cache"
    "--exclude=.pytest_cache"
    "--exclude=.hypothesis"
    "--exclude=htmlcov"
    "--exclude=logs"
    "--exclude=checkpoints"
    "--exclude=budget_data"
)

run_backup() {
    local repo="$1"
    local name="$2"
    local log="${LOG_DIR}/backup-${name}-$(date +%Y%m%d).log"

    if [[ -z "${repo}" ]]; then
        emit_event "backup_target_skip" "info" target="${name}" reason="not_configured"
        return 1
    fi

    start_timer "backup_${name}"
    export RESTIC_REPOSITORY="${repo}"

    # Initialize repo if needed
    restic snapshots &>/dev/null || restic init 2>&1

    local paths=()
    for p in "${INCLUDE_PATHS[@]}"; do
        if [[ -e "$p" ]]; then
            paths+=("$p")
        fi
    done

    if restic backup \
        "${EXCLUDE_PATTERNS[@]}" \
        --tag "orxaq" \
        --tag "$(date +%Y-%m-%d)" \
        "${paths[@]}" \
        >> "${log}" 2>&1; then

        # Extract snapshot stats
        local snapshot_id
        snapshot_id=$(restic snapshots --tag orxaq --latest 1 --json 2>/dev/null | grep -o '"short_id":"[^"]*"' | head -1 | cut -d'"' -f4 || echo "unknown")

        emit_event "backup_success" "ok" target="${name}" snapshot="${snapshot_id}"

        # Prune old snapshots
        restic forget \
            --keep-daily 7 \
            --keep-weekly 4 \
            --keep-monthly 6 \
            --prune \
            >> "${log}" 2>&1

        end_timer "backup_${name}" "ok" target="${name}"
        return 0
    else
        emit_event "backup_failed" "error" target="${name}" log="${log}"
        end_timer "backup_${name}" "error" target="${name}"
        return 1
    fi
}

# --- Main ---
start_timer "backup_total"

SUCCESS_COUNT=0
FAIL_COUNT=0

if run_backup "${S3_REPO}" "aws-s3"; then
    ((SUCCESS_COUNT++))
else
    ((FAIL_COUNT++))
fi

if run_backup "${GCS_REPO}" "gcs"; then
    ((SUCCESS_COUNT++))
else
    ((FAIL_COUNT++))
fi

# Also encrypt secrets to iCloud vault
start_timer "vault_sync"
if [[ -x "${SCRIPT_DIR}/encrypt_secrets.sh" ]]; then
    bash "${SCRIPT_DIR}/encrypt_secrets.sh"
fi
end_timer "vault_sync" "ok"

# Record timestamp
date -u +"%Y-%m-%dT%H:%M:%SZ" > "${LOG_DIR}/last_backup.txt"

emit_metric "backup_targets_success" "${SUCCESS_COUNT}"
emit_metric "backup_targets_failed" "${FAIL_COUNT}"

# Emit disk usage for all repos
for repo in orxaq orxaq-ops orxaq-pay swarm-orchestrator odyssey; do
    emit_disk_usage "${DEV_DIR}/${repo}" "${repo}"
done

end_timer "backup_total" "$([ ${FAIL_COUNT} -lt 2 ] && echo ok || echo error)" \
    success="${SUCCESS_COUNT}" failed="${FAIL_COUNT}"

if [[ ${FAIL_COUNT} -eq 2 ]]; then
    exit 1
fi
