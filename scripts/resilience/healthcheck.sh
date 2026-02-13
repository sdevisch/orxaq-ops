#!/usr/bin/env bash
# healthcheck.sh — Periodic health verification (every 5 minutes via launchd)
# Checks repo integrity, backup freshness, secret availability, process health,
# disk space, and LM Studio availability.
#
# NOTE: We intentionally do NOT use `set -e` here. Individual section failures
# must never prevent the status file from being written (see issue #56).
set -uo pipefail

TELEMETRY_SCRIPT="healthcheck"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/telemetry.sh"

DEV_DIR="${HOME}/dev"
LOG_DIR="${DEV_DIR}/.claude/resilience/logs"
VAULT_DIR="${HOME}/Library/Mobile Documents/com~apple~CloudDocs/orxaq-vault"
STATUS_FILE="${LOG_DIR}/health-status.json"
ICLOUD_ERROR_CACHE="${LOG_DIR}/.icloud-error-cache"
ICLOUD_ERROR_INTERVAL=3600  # Only log iCloud errors once per hour

# --- Configurable Thresholds ---
# Override via environment variables or .healthcheck.env
HEALTHCHECK_ENV="${DEV_DIR}/.claude/resilience/.healthcheck.env"
if [[ -f "${HEALTHCHECK_ENV}" ]]; then
    source "${HEALTHCHECK_ENV}"
fi

VAULT_STALE_HOURS="${ORXAQ_VAULT_STALE_HOURS:-24}"
BACKUP_STALE_HOURS="${ORXAQ_BACKUP_STALE_HOURS:-24}"
UNPUSHED_WARN_THRESHOLD="${ORXAQ_UNPUSHED_WARN_THRESHOLD:-5}"
DISK_LOW_GB="${ORXAQ_DISK_LOW_GB:-10}"
TELEMETRY_MAX_LINES="${ORXAQ_TELEMETRY_MAX_LINES:-50000}"

mkdir -p "${LOG_DIR}"

# --- iCloud error deduplication helpers (Issue #66) ---
# Only log iCloud-related errors once per ICLOUD_ERROR_INTERVAL seconds.
_icloud_error_should_log() {
    local error_key="$1"
    if [[ ! -f "${ICLOUD_ERROR_CACHE}" ]]; then
        return 0  # No cache file, should log
    fi
    local last_ts
    last_ts=$(grep "^${error_key}=" "${ICLOUD_ERROR_CACHE}" 2>/dev/null | tail -1 | cut -d= -f2 || echo 0)
    last_ts="${last_ts:-0}"
    local now_epoch
    now_epoch=$(date +%s)
    local elapsed=$(( now_epoch - last_ts ))
    if [[ ${elapsed} -ge ${ICLOUD_ERROR_INTERVAL} ]]; then
        return 0  # Enough time has passed, should log
    fi
    return 1  # Still within suppression window
}

_icloud_error_record() {
    local error_key="$1"
    local now_epoch
    now_epoch=$(date +%s)
    # Update or append the error timestamp in the cache file
    if [[ -f "${ICLOUD_ERROR_CACHE}" ]] && grep -q "^${error_key}=" "${ICLOUD_ERROR_CACHE}" 2>/dev/null; then
        # Replace existing entry (portable sed)
        local tmp="${ICLOUD_ERROR_CACHE}.tmp"
        grep -v "^${error_key}=" "${ICLOUD_ERROR_CACHE}" > "${tmp}" 2>/dev/null || true
        echo "${error_key}=${now_epoch}" >> "${tmp}"
        mv "${tmp}" "${ICLOUD_ERROR_CACHE}"
    else
        echo "${error_key}=${now_epoch}" >> "${ICLOUD_ERROR_CACHE}"
    fi
}

# Helper: test if an iCloud path is accessible (not just present)
_icloud_path_accessible() {
    local path="$1"
    # Try to list the path to check for permission errors
    ls "${path}" >/dev/null 2>&1
    return $?
}

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
ISSUES=()

# Emergency trap: always write a status file even if the script crashes
_write_emergency_status() {
    local exit_code=$?
    if [[ ${exit_code} -ne 0 ]] && [[ ! -f "${STATUS_FILE}" || "$(cat "${STATUS_FILE}" 2>/dev/null)" != *"${TIMESTAMP}"* ]]; then
        cat > "${STATUS_FILE}" << EEOF
{
    "timestamp": "${TIMESTAMP}",
    "status": "error",
    "issues": ["HEALTHCHECK_CRASHED:exit_code=${exit_code}"],
    "repos_checked": 0,
    "repos_healthy": 0,
    "vault_present": false,
    "disk_available_gb": 0,
    "thresholds": {}
}
EEOF
        emit_event "healthcheck_crash_recovery" "error" exit_code="${exit_code}" 2>/dev/null || true
    fi
}
trap '_write_emergency_status' EXIT

# --- Repo checks ---
start_timer "repo_checks"
REPOS_HEALTHY=0
REPOS_UNHEALTHY=0

for repo in orxaq orxaq-ops orxaq-pay swarm-orchestrator odyssey; do
    repo_dir="${DEV_DIR}/${repo}"
    if [[ ! -d "${repo_dir}/.git" ]]; then
        ISSUES+=("MISSING_REPO:${repo}")
        ((REPOS_UNHEALTHY++)) || true
        emit_event "repo_missing" "error" repo="${repo}"
        continue
    fi
    ((REPOS_HEALTHY++)) || true

    # Check for unpushed commits
    unpushed=$(git -C "${repo_dir}" log --oneline '@{u}..HEAD' 2>/dev/null | wc -l | tr -d '[:space:]' || true)
    unpushed="${unpushed:-0}"
    if [[ "${unpushed}" -gt "${UNPUSHED_WARN_THRESHOLD}" ]]; then
        ISSUES+=("UNPUSHED:${repo}:${unpushed}_commits")
        emit_event "unpushed_commits" "warn" repo="${repo}" count="${unpushed}"
    fi

    # Emit git metrics for each repo
    emit_git_metrics "${repo_dir}" "${repo}"
done

emit_metric "repos_healthy" "${REPOS_HEALTHY}"
emit_metric "repos_unhealthy" "${REPOS_UNHEALTHY}"
end_timer "repo_checks" "ok"

# --- iCloud vault check (Issue #54: graceful iCloud error handling) ---
start_timer "vault_check"
VAULT_ACCESSIBLE=false
if [[ ! -d "${VAULT_DIR}" ]]; then
    if _icloud_error_should_log "vault_missing"; then
        ISSUES+=("ICLOUD_VAULT_MISSING")
        emit_event "vault_missing" "warn" note="iCloud_vault_directory_not_found"
        _icloud_error_record "vault_missing"
    fi
elif ! _icloud_path_accessible "${VAULT_DIR}"; then
    # iCloud path exists but is not accessible (permission denied)
    if _icloud_error_should_log "vault_permission_denied"; then
        ISSUES+=("ICLOUD_VAULT_PERMISSION_DENIED")
        emit_event "vault_permission_denied" "warn" \
            note="iCloud_vault_exists_but_not_accessible" \
            path="${VAULT_DIR}"
        _icloud_error_record "vault_permission_denied"
    fi
else
    VAULT_ACCESSIBLE=true
    last_encrypted="${VAULT_DIR}/last_encrypted.txt"
    if [[ -f "${last_encrypted}" ]]; then
        # Guard against permission denied on iCloud vault (issue #56)
        last_ts=$(cat "${last_encrypted}" 2>/dev/null || true)
        if [[ -n "${last_ts}" ]]; then
            last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "${last_ts}" +%s 2>/dev/null || echo 0)
            now_epoch=$(date +%s)
            age_hours=$(( (now_epoch - last_epoch) / 3600 ))
            emit_metric "vault_age_hours" "${age_hours}" "hours"
            if [[ ${age_hours} -gt ${VAULT_STALE_HOURS} ]]; then
                ISSUES+=("VAULT_STALE:${age_hours}h_old")
                emit_event "vault_stale" "warn" age_hours="${age_hours}"
            fi
        else
            if _icloud_error_should_log "vault_read_error"; then
                ISSUES+=("VAULT_READ_ERROR:permission_denied_or_empty")
                emit_event "vault_read_error" "warn"
                _icloud_error_record "vault_read_error"
            fi
        fi
    else
        ISSUES+=("VAULT_NEVER_ENCRYPTED")
        emit_event "vault_never_encrypted" "warn"
    fi
fi
end_timer "vault_check" "ok"

# --- Cloud backup check ---
start_timer "backup_check"
last_backup="${LOG_DIR}/last_backup.txt"
if [[ -f "${last_backup}" ]]; then
    last_ts=$(cat "${last_backup}" 2>/dev/null || true)
    if [[ -n "${last_ts}" ]]; then
        last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "${last_ts}" +%s 2>/dev/null || echo 0)
        now_epoch=$(date +%s)
        age_hours=$(( (now_epoch - last_epoch) / 3600 ))
        emit_metric "backup_age_hours" "${age_hours}" "hours"
        if [[ ${age_hours} -gt ${BACKUP_STALE_HOURS} ]]; then
            ISSUES+=("BACKUP_STALE:${age_hours}h_old")
            emit_event "backup_stale" "warn" age_hours="${age_hours}"
        fi
    fi
else
    ISSUES+=("BACKUP_NEVER_RUN")
    emit_event "backup_never_run" "warn"
fi
end_timer "backup_check" "ok"

# --- Claude memory check ---
CLAUDE_MEM="${HOME}/.claude/projects/-Users-sdevisch-dev/memory/MEMORY.md"
if [[ ! -f "${CLAUDE_MEM}" ]]; then
    ISSUES+=("CLAUDE_MEMORY_MISSING")
    emit_event "claude_memory_missing" "error"
fi

# --- Process health checks ---
start_timer "process_checks"

# Check if autonomy runner is alive (via PID file)
ARTIFACTS_DIR="${DEV_DIR}/orxaq-ops/artifacts/autonomy"
RUNNER_PID_FILE="${ARTIFACTS_DIR}/runner.pid"
if [[ -f "${RUNNER_PID_FILE}" ]]; then
    runner_pid=$(cat "${RUNNER_PID_FILE}" 2>/dev/null || true)
    if [[ -n "${runner_pid}" ]] && ! kill -0 "${runner_pid}" 2>/dev/null; then
        ISSUES+=("RUNNER_PROCESS_DEAD:pid=${runner_pid}")
        emit_event "runner_dead" "error" pid="${runner_pid}"
    fi
fi

# Check supervisor PID
SUPERVISOR_PID_FILE="${ARTIFACTS_DIR}/supervisor.pid"
if [[ -f "${SUPERVISOR_PID_FILE}" ]]; then
    sup_pid=$(cat "${SUPERVISOR_PID_FILE}" 2>/dev/null || true)
    if [[ -n "${sup_pid}" ]] && ! kill -0 "${sup_pid}" 2>/dev/null; then
        ISSUES+=("SUPERVISOR_PROCESS_DEAD:pid=${sup_pid}")
        emit_event "supervisor_dead" "error" pid="${sup_pid}"
    fi
fi

# Check heartbeat freshness
HEARTBEAT_FILE="${ARTIFACTS_DIR}/heartbeat.json"
if [[ -f "${HEARTBEAT_FILE}" ]]; then
    # Extract timestamp from heartbeat JSON using python (stdlib only)
    hb_age=$(python3 -c "
import json, sys
from datetime import datetime, timezone
try:
    d = json.load(open('${HEARTBEAT_FILE}'))
    ts = d.get('timestamp', '')
    if ts:
        hb = datetime.fromisoformat(ts)
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - hb).total_seconds()
        print(int(age))
    else:
        print(-1)
except Exception:
    print(-1)
" 2>/dev/null || echo "-1")
    emit_metric "heartbeat_age_sec" "${hb_age}" "seconds"
    if [[ "${hb_age}" -gt 600 ]]; then
        ISSUES+=("HEARTBEAT_STALE:${hb_age}s")
        emit_event "heartbeat_stale" "warn" age_sec="${hb_age}"
    fi
fi

end_timer "process_checks" "ok"

# --- Disk space check ---
start_timer "disk_check"
# Get available disk space in GB (macOS df output)
avail_kb=$(df -k "${HOME}" 2>/dev/null | tail -1 | awk '{print $4}')
avail_gb=$(( ${avail_kb:-0} / 1048576 ))
emit_metric "disk_available_gb" "${avail_gb}" "GB"
if [[ ${avail_gb} -lt ${DISK_LOW_GB} ]]; then
    ISSUES+=("DISK_LOW:${avail_gb}GB_available")
    emit_event "disk_low" "warn" available_gb="${avail_gb}" threshold_gb="${DISK_LOW_GB}"
fi
end_timer "disk_check" "ok"

# --- LM Studio availability check ---
start_timer "lmstudio_check"
LMSTUDIO_URL="${ORXAQ_LMSTUDIO_URL:-http://localhost:1234}"
if curl -s --connect-timeout 2 --max-time 3 "${LMSTUDIO_URL}/v1/models" >/dev/null 2>&1; then
    emit_event "lmstudio_up" "ok" url="${LMSTUDIO_URL}"
else
    emit_event "lmstudio_down" "info" url="${LMSTUDIO_URL}"
    # Not an issue per se (user may be traveling), but worth noting
fi
end_timer "lmstudio_check" "ok"

# --- Telemetry log size check ---
TELEMETRY_LOG="${LOG_DIR}/telemetry.jsonl"
if [[ -f "${TELEMETRY_LOG}" ]]; then
    log_lines=$(wc -l < "${TELEMETRY_LOG}" | tr -d '[:space:]')
    log_size_kb=$(du -k "${TELEMETRY_LOG}" | cut -f1)
    emit_metric "telemetry_log_lines" "${log_lines}"
    emit_metric "telemetry_log_size_kb" "${log_size_kb}" "KB"
    # Auto-rotate if telemetry log exceeds threshold
    if [[ "${log_lines}" -gt "${TELEMETRY_MAX_LINES}" ]]; then
        tail -n $(( TELEMETRY_MAX_LINES / 2 )) "${TELEMETRY_LOG}" > "${TELEMETRY_LOG}.tmp"
        mv "${TELEMETRY_LOG}.tmp" "${TELEMETRY_LOG}"
        emit_event "telemetry_rotated" "info" \
            before_lines="${log_lines}" after_lines="$(( TELEMETRY_MAX_LINES / 2 ))"
    fi
fi

# --- Write status ---
if [[ ${#ISSUES[@]} -eq 0 ]]; then
    STATUS="healthy"
else
    STATUS="degraded"
fi

emit_event "health_result" "${STATUS}" issue_count="${#ISSUES[@]}"

# Build issues JSON array safely (handles empty array without printf crash)
ISSUES_JSON="[]"
if [[ ${#ISSUES[@]} -gt 0 ]]; then
    ISSUES_JSON="["
    first=true
    for issue in "${ISSUES[@]}"; do
        if [[ "${first}" == "true" ]]; then
            first=false
        else
            ISSUES_JSON+=","
        fi
        # Escape quotes in issue string
        escaped="${issue//\"/\\\"}"
        ISSUES_JSON+="\"${escaped}\""
    done
    ISSUES_JSON+="]"
fi

cat > "${STATUS_FILE}" << EOF
{
    "timestamp": "${TIMESTAMP}",
    "status": "${STATUS}",
    "issues": ${ISSUES_JSON},
    "repos_checked": 5,
    "repos_healthy": ${REPOS_HEALTHY},
    "vault_present": ${VAULT_ACCESSIBLE},
    "disk_available_gb": ${avail_gb},
    "thresholds": {
        "vault_stale_hours": ${VAULT_STALE_HOURS},
        "backup_stale_hours": ${BACKUP_STALE_HOURS},
        "unpushed_warn_threshold": ${UNPUSHED_WARN_THRESHOLD},
        "disk_low_gb": ${DISK_LOW_GB}
    }
}
EOF
