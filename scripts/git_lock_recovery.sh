#!/bin/bash
# Enhanced Git Lock Recovery Script
# Version: 1.1.0
# Implements advanced git lock detection, recovery, and logging strategies

# Configuration
CONFIG_LOG_DIR="/var/log/orxaq-autonomy"
LOCK_FILE=".git/index.lock"
MAX_LOCK_AGE=300     # 5 minutes
CRITICAL_LOCK_AGE=1800  # 30 minutes
RECOVERY_LOG="$CONFIG_LOG_DIR/git_lock_recovery.log"

# Ensure log directory exists
mkdir -p "$CONFIG_LOG_DIR"

# Logging function
log_event() {
    local message="$1"
    local log_level="${2:-INFO}"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$log_level] $message" >> "$RECOVERY_LOG"
}

# Safely get process information
get_lock_process_info() {
    local lock_pid="$1"
    if [[ -n "$lock_pid" ]]; then
        ps -p "$lock_pid" -o pid,comm,start | tail -n +2
    fi
}

# Advanced Git Lock Recovery
detect_and_recover_git_locks() {
    local lock_file="$1"
    local lock_pid

    # Check if lock file exists
    if [[ ! -f "$lock_file" ]]; then
        log_event "No git lock found at $lock_file" "DEBUG"
        return 0
    }

    # Calculate lock age
    local lock_age=$(($(date +%s) - $(stat -f %m "$lock_file")))
    lock_pid=$(cat "$lock_file" 2>/dev/null || echo "")

    # Logging lock details
    log_event "Detected git lock: Age=$lock_age seconds, PID=$lock_pid" "INFO"

    # Process lock recovery strategies
    if [[ "$lock_age" -gt "$MAX_LOCK_AGE" ]]; then
        if [[ -n "$lock_pid" ]] && ! kill -0 "$lock_pid" 2>/dev/null; then
            # Remove stale lock if process does not exist
            rm -f "$lock_file"
            log_event "Removed stale git lock: $lock_file (PID $lock_pid not running)" "WARN"
        elif [[ "$lock_age" -gt "$CRITICAL_LOCK_AGE" ]]; then
            # Aggressive recovery for long-held locks
            local process_info=$(get_lock_process_info "$lock_pid")
            log_event "Critical lock detected. Process details: $process_info" "ERROR"

            # Forcibly terminate the process
            kill -KILL "$lock_pid" 2>/dev/null
            rm -rf .git/index.lock*
            git clean -fd

            log_event "Forcibly reset git state for critical lock" "CRITICAL"
        else
            # Graceful termination attempts
            kill -TERM "$lock_pid" 2>/dev/null
            sleep 2
            if ! kill -0 "$lock_pid" 2>/dev/null; then
                rm -f "$lock_file"
                log_event "Gracefully removed git lock for PID $lock_pid" "INFO"
            else
                log_event "Unable to remove git lock for PID $lock_pid" "WARN"
            fi
        fi
    fi
}

# Resilient Git Operation Wrapper
git_resilient_operation() {
    local max_retries=5
    local base_delay=5
    local max_delay=300
    local delay=$base_delay
    local operation_log="$CONFIG_LOG_DIR/git_operations.log"

    detect_and_recover_git_locks "$LOCK_FILE"

    for ((attempt=1; attempt<=max_retries; attempt++)); do
        if git "$@"; then
            log_event "Successful git operation: $*" "INFO"
            return 0
        fi

        log_event "Git operation failed (Attempt $attempt): $*" "WARN"
        sleep $delay
        delay=$((delay * 2))
        delay=$((delay > max_delay ? max_delay : delay))
        detect_and_recover_git_locks "$LOCK_FILE"
    done

    log_event "Git operation failed after $max_retries attempts: $*" "ERROR"
    return 1
}

# Main execution
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    git_resilient_operation "$@"
fi