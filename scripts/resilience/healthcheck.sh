#!/usr/bin/env bash
# healthcheck.sh — Periodic health verification (every 5 minutes via launchd)
# Checks repo integrity, backup freshness, secret availability
set -euo pipefail

TELEMETRY_SCRIPT="healthcheck"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/telemetry.sh"

DEV_DIR="${HOME}/dev"
LOG_DIR="${DEV_DIR}/.claude/resilience/logs"
VAULT_DIR="${HOME}/Library/Mobile Documents/com~apple~CloudDocs/orxaq-vault"
STATUS_FILE="${LOG_DIR}/health-status.json"

mkdir -p "${LOG_DIR}"

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
ISSUES=()

# --- Repo checks ---
start_timer "repo_checks"
REPOS_HEALTHY=0
REPOS_UNHEALTHY=0

for repo in orxaq orxaq-ops orxaq-pay swarm-orchestrator odyssey; do
    repo_dir="${DEV_DIR}/${repo}"
    if [[ ! -d "${repo_dir}/.git" ]]; then
        ISSUES+=("MISSING_REPO:${repo}")
        ((REPOS_UNHEALTHY++))
        emit_event "repo_missing" "error" repo="${repo}"
        continue
    fi
    ((REPOS_HEALTHY++))

    # Check for unpushed commits
    unpushed=$(git -C "${repo_dir}" log --oneline '@{u}..HEAD' 2>/dev/null | wc -l | tr -d '[:space:]' || true)
    unpushed="${unpushed:-0}"
    if [[ "${unpushed}" -gt 5 ]]; then
        ISSUES+=("UNPUSHED:${repo}:${unpushed}_commits")
        emit_event "unpushed_commits" "warn" repo="${repo}" count="${unpushed}"
    fi

    # Emit git metrics for each repo
    emit_git_metrics "${repo_dir}" "${repo}"
done

emit_metric "repos_healthy" "${REPOS_HEALTHY}"
emit_metric "repos_unhealthy" "${REPOS_UNHEALTHY}"
end_timer "repo_checks" "ok"

# --- iCloud vault check ---
start_timer "vault_check"
if [[ ! -d "${VAULT_DIR}" ]]; then
    ISSUES+=("ICLOUD_VAULT_MISSING")
    emit_event "vault_missing" "error"
else
    last_encrypted="${VAULT_DIR}/last_encrypted.txt"
    if [[ -f "${last_encrypted}" ]]; then
        last_ts=$(cat "${last_encrypted}")
        last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "${last_ts}" +%s 2>/dev/null || echo 0)
        now_epoch=$(date +%s)
        age_hours=$(( (now_epoch - last_epoch) / 3600 ))
        emit_metric "vault_age_hours" "${age_hours}" "hours"
        if [[ ${age_hours} -gt 24 ]]; then
            ISSUES+=("VAULT_STALE:${age_hours}h_old")
            emit_event "vault_stale" "warn" age_hours="${age_hours}"
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
    last_ts=$(cat "${last_backup}")
    last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "${last_ts}" +%s 2>/dev/null || echo 0)
    now_epoch=$(date +%s)
    age_hours=$(( (now_epoch - last_epoch) / 3600 ))
    emit_metric "backup_age_hours" "${age_hours}" "hours"
    if [[ ${age_hours} -gt 24 ]]; then
        ISSUES+=("BACKUP_STALE:${age_hours}h_old")
        emit_event "backup_stale" "warn" age_hours="${age_hours}"
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

# --- Telemetry log size check ---
TELEMETRY_LOG="${LOG_DIR}/telemetry.jsonl"
if [[ -f "${TELEMETRY_LOG}" ]]; then
    log_lines=$(wc -l < "${TELEMETRY_LOG}" | tr -d '[:space:]')
    log_size_kb=$(du -k "${TELEMETRY_LOG}" | cut -f1)
    emit_metric "telemetry_log_lines" "${log_lines}"
    emit_metric "telemetry_log_size_kb" "${log_size_kb}" "KB"
fi

# --- Write status ---
if [[ ${#ISSUES[@]} -eq 0 ]]; then
    STATUS="healthy"
else
    STATUS="degraded"
fi

emit_event "health_result" "${STATUS}" issue_count="${#ISSUES[@]}"

cat > "${STATUS_FILE}" << EOF
{
    "timestamp": "${TIMESTAMP}",
    "status": "${STATUS}",
    "issues": [$(printf '"%s",' "${ISSUES[@]}" 2>/dev/null | sed 's/,$//')],
    "repos_checked": 5,
    "repos_healthy": ${REPOS_HEALTHY},
    "vault_present": $(test -d "${VAULT_DIR}" && echo true || echo false)
}
EOF
