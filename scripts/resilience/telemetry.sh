#!/usr/bin/env bash
# telemetry.sh — Shared instrumentation library for all resilience scripts
# Source this file: source "$(dirname "$0")/telemetry.sh"
# All events are structured JSON written to a single telemetry log + stdout

TELEMETRY_DIR="${HOME}/dev/.claude/resilience/logs"
TELEMETRY_LOG="${TELEMETRY_DIR}/telemetry.jsonl"
TELEMETRY_VAULT="${HOME}/Library/Mobile Documents/com~apple~CloudDocs/orxaq-vault/telemetry.jsonl"
TELEMETRY_SCRIPT="${TELEMETRY_SCRIPT:-$(basename "$0")}"
TELEMETRY_RUN_ID="${TELEMETRY_RUN_ID:-$(date +%s)-$$}"
TELEMETRY_HOST="$(hostname -s 2>/dev/null || echo unknown)"
TELEMETRY_MACHINE_ID="$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'"' '/IOPlatformSerialNumber/{print $4}' || echo unknown)"

mkdir -p "${TELEMETRY_DIR}"

# --- Core emit function ---
# Usage: emit_event <event_type> <status> [key=value ...]
# Example: emit_event "backup_start" "info" target=aws-s3 repo_count=5
emit_event() {
    local event_type="$1"
    local status="$2"
    shift 2

    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")
    local epoch
    epoch=$(date +%s)

    # Build extra fields JSON
    local extras=""
    for kv in "$@"; do
        local key="${kv%%=*}"
        local val="${kv#*=}"
        # Escape quotes in value
        val="${val//\"/\\\"}"
        extras="${extras}\"${key}\":\"${val}\","
    done

    local json
    json=$(printf '{"ts":"%s","epoch":%s,"run_id":"%s","host":"%s","machine":"%s","script":"%s","event":"%s","status":"%s",%s"pid":%s}' \
        "${ts}" "${epoch}" "${TELEMETRY_RUN_ID}" "${TELEMETRY_HOST}" \
        "${TELEMETRY_MACHINE_ID}" "${TELEMETRY_SCRIPT}" \
        "${event_type}" "${status}" "${extras}" "$$")

    echo "${json}" >> "${TELEMETRY_LOG}" 2>/dev/null || true

    # Mirror to iCloud vault if available and accessible (non-blocking)
    # Issue #54: Only attempt iCloud write if the directory is accessible
    local vault_parent
    vault_parent="$(dirname "${TELEMETRY_VAULT}")"
    if [[ -d "${vault_parent}" ]] && ls "${vault_parent}" >/dev/null 2>&1; then
        echo "${json}" >> "${TELEMETRY_VAULT}" 2>/dev/null || true
    fi
}

# --- Timer functions ---
# Usage: start_timer "operation_name"
#        ... do work ...
#        end_timer "operation_name" "ok" [extra=fields]
declare -A _TIMERS 2>/dev/null || true

start_timer() {
    local name="$1"
    eval "_TIMER_${name//[^a-zA-Z0-9_]/_}=$(date +%s)"
    emit_event "${name}_start" "info"
}

end_timer() {
    local name="$1"
    local status="${2:-ok}"
    shift 2 2>/dev/null || true

    local var_name="_TIMER_${name//[^a-zA-Z0-9_]/_}"
    local start_epoch
    start_epoch=$(eval echo "\$${var_name}" 2>/dev/null || echo 0)
    local end_epoch
    end_epoch=$(date +%s)
    local duration_s=$(( end_epoch - start_epoch ))

    emit_event "${name}_end" "${status}" duration_s="${duration_s}" "$@"
}

# --- Metric emit ---
# Usage: emit_metric "metric_name" <value> [unit]
emit_metric() {
    local name="$1"
    local value="$2"
    local unit="${3:-count}"
    emit_event "metric" "info" metric="${name}" value="${value}" unit="${unit}"
}

# --- Error trap ---
# Automatically emit error events on script failure
_telemetry_error_trap() {
    local exit_code=$?
    local line_no="${BASH_LINENO[0]:-unknown}"
    if [[ ${exit_code} -ne 0 ]]; then
        emit_event "script_error" "error" exit_code="${exit_code}" line="${line_no}"
    fi
}
trap '_telemetry_error_trap' ERR 2>/dev/null || true

# --- Script lifecycle ---
emit_event "script_start" "info" args="$*"

_telemetry_exit_trap() {
    local exit_code=$?
    emit_event "script_end" "$([ ${exit_code} -eq 0 ] && echo ok || echo error)" exit_code="${exit_code}"
}
trap '_telemetry_exit_trap' EXIT

# --- Disk usage metric ---
emit_disk_usage() {
    local path="$1"
    local label="${2:-$(basename "$path")}"
    if [[ -d "${path}" ]]; then
        local size_kb
        size_kb=$(du -sk "${path}" 2>/dev/null | cut -f1 || echo 0)
        emit_metric "disk_usage_kb_${label}" "${size_kb}" "KB"
    fi
}

# --- Git repo metrics ---
emit_git_metrics() {
    local repo_path="$1"
    local repo_name="${2:-$(basename "$repo_path")}"
    if [[ -d "${repo_path}/.git" ]]; then
        local branch
        branch=$(git -C "${repo_path}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        local commits
        commits=$(git -C "${repo_path}" rev-list --count HEAD 2>/dev/null || echo 0)
        local dirty
        dirty=$(git -C "${repo_path}" status --porcelain 2>/dev/null | wc -l | tr -d '[:space:]')
        local unpushed
        unpushed=$(git -C "${repo_path}" log --oneline '@{u}..HEAD' 2>/dev/null | wc -l | tr -d '[:space:]' || echo 0)
        unpushed="${unpushed:-0}"
        emit_event "git_status" "info" \
            repo="${repo_name}" branch="${branch}" \
            total_commits="${commits}" dirty_files="${dirty}" unpushed="${unpushed}"
    fi
}
