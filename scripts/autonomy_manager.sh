#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTONOMY_DIR="$ROOT_DIR/artifacts/autonomy"
SUPERVISOR_PID_FILE="$AUTONOMY_DIR/supervisor.pid"
RUNNER_PID_FILE="$AUTONOMY_DIR/runner.pid"
LOG_FILE="$AUTONOMY_DIR/runner.log"
HEARTBEAT_FILE="$AUTONOMY_DIR/heartbeat.json"
LOCK_FILE="$AUTONOMY_DIR/runner.lock"
STATE_FILE="$ROOT_DIR/state/state.json"
ENV_FILE="${ORXAQ_AUTONOMY_ENV_FILE:-$ROOT_DIR/.env.autonomy}"

DEFAULT_IMPL_REPO="$ROOT_DIR/../orxaq"
IMPL_REPO="${ORXAQ_IMPL_REPO:-$DEFAULT_IMPL_REPO}"
DEFAULT_TEST_REPO="$ROOT_DIR/../orxaq_gemini"
TEST_REPO="${ORXAQ_TEST_REPO:-$DEFAULT_TEST_REPO}"
MAX_CYCLES="${ORXAQ_AUTONOMY_MAX_CYCLES:-10000}"
MAX_ATTEMPTS="${ORXAQ_AUTONOMY_MAX_ATTEMPTS:-8}"
MAX_RETRYABLE_BLOCKED_RETRIES="${ORXAQ_AUTONOMY_MAX_RETRYABLE_BLOCKED_RETRIES:-20}"
AGENT_TIMEOUT="${ORXAQ_AUTONOMY_AGENT_TIMEOUT_SEC:-3600}"
VALIDATE_TIMEOUT="${ORXAQ_AUTONOMY_VALIDATE_TIMEOUT_SEC:-1800}"
RETRY_BACKOFF_BASE_SEC="${ORXAQ_AUTONOMY_RETRY_BACKOFF_BASE_SEC:-30}"
RETRY_BACKOFF_MAX_SEC="${ORXAQ_AUTONOMY_RETRY_BACKOFF_MAX_SEC:-1800}"
GIT_LOCK_STALE_SEC="${ORXAQ_AUTONOMY_GIT_LOCK_STALE_SEC:-300}"
VALIDATION_RETRIES="${ORXAQ_AUTONOMY_VALIDATION_RETRIES:-1}"
IDLE_SLEEP_SEC="${ORXAQ_AUTONOMY_IDLE_SLEEP_SEC:-10}"
AUTONOMY_PYTHON="${ORXAQ_AUTONOMY_PYTHON:-$ROOT_DIR/.venv/bin/python3}"
SUPERVISOR_RESTART_DELAY_SEC="${ORXAQ_AUTONOMY_SUPERVISOR_RESTART_DELAY_SEC:-5}"
SUPERVISOR_MAX_BACKOFF_SEC="${ORXAQ_AUTONOMY_SUPERVISOR_MAX_BACKOFF_SEC:-300}"
SUPERVISOR_MAX_RESTARTS="${ORXAQ_AUTONOMY_SUPERVISOR_MAX_RESTARTS:-0}"
HEARTBEAT_POLL_SEC="${ORXAQ_AUTONOMY_HEARTBEAT_POLL_SEC:-20}"
HEARTBEAT_STALE_SEC="${ORXAQ_AUTONOMY_HEARTBEAT_STALE_SEC:-300}"

load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$ENV_FILE"
    set +a
  fi
}

require_bins() {
  command -v codex >/dev/null 2>&1 || { echo "codex CLI not found in PATH"; exit 1; }
  command -v gemini >/dev/null 2>&1 || { echo "gemini CLI not found in PATH"; exit 1; }
}

check_key() {
  if [[ -n "${OPENAI_API_KEY:-}" && "${OPENAI_API_KEY:-}" != "replace_me" ]]; then
    return
  fi
  if codex login status >/dev/null 2>&1; then
    return
  fi
  echo "Codex auth missing. Set OPENAI_API_KEY in $ENV_FILE or run: codex login"
  exit 1
}

check_gemini_auth() {
  if [[ -n "${GEMINI_API_KEY:-}" && "${GEMINI_API_KEY:-}" != "replace_me" ]]; then
    return
  fi
  if [[ "${GOOGLE_GENAI_USE_VERTEXAI:-}" == "true" || "${GOOGLE_GENAI_USE_GCA:-}" == "true" ]]; then
    return
  fi

  if [[ -f "$HOME/.gemini/settings.json" ]] && grep -q '"selectedType"' "$HOME/.gemini/settings.json"; then
    return
  fi

  echo "Gemini auth missing."
  echo "Set GEMINI_API_KEY (or GOOGLE_GENAI_USE_VERTEXAI/GOOGLE_GENAI_USE_GCA) in $ENV_FILE,"
  echo "or configure ~/.gemini/settings.json with security.auth.selectedType."
  exit 1
}

require_python() {
  if [[ -x "$AUTONOMY_PYTHON" ]]; then
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    AUTONOMY_PYTHON="python3"
    return
  fi
  echo "No Python interpreter found for autonomy runner."
  echo "Set ORXAQ_AUTONOMY_PYTHON in $ENV_FILE (or shell) to a valid python path."
  exit 1
}

runtime_python() {
  if [[ -x "$AUTONOMY_PYTHON" ]]; then
    printf "%s" "$AUTONOMY_PYTHON"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf "python3"
    return
  fi
  printf ""
}

ensure_runtime() {
  load_env
  require_bins
  check_key
  check_gemini_auth
  require_python
  mkdir -p "$AUTONOMY_DIR"
}

is_pid_running() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

read_pid() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  cat "$file"
}

is_supervisor_running() {
  local pid
  pid="$(read_pid "$SUPERVISOR_PID_FILE" 2>/dev/null || true)"
  if is_pid_running "$pid"; then
    return 0
  fi
  return 1
}

is_runner_running() {
  local pid
  pid="$(read_pid "$RUNNER_PID_FILE" 2>/dev/null || true)"
  if is_pid_running "$pid"; then
    return 0
  fi
  return 1
}

heartbeat_age_seconds() {
  local py
  if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    echo "-1"
    return 0
  fi

  py="$(runtime_python)"
  if [[ -z "$py" ]]; then
    echo "-1"
    return 0
  fi

  "$py" -c '
import datetime as dt
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    raw = json.loads(path.read_text(encoding="utf-8"))
    ts = str(raw.get("timestamp", "")).strip()
    if not ts:
        raise ValueError("missing timestamp")
    parsed = dt.datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    print(int((now - parsed).total_seconds()))
except Exception:
    print(-1)
' "$HEARTBEAT_FILE"
}

heartbeat_is_stale() {
  local age
  age="$(heartbeat_age_seconds)"
  if [[ "$age" == "-1" ]]; then
    return 1
  fi
  if (( age > HEARTBEAT_STALE_SEC )); then
    return 0
  fi
  return 1
}

cleanup_runner_pid() {
  local pid
  pid="$(read_pid "$RUNNER_PID_FILE" 2>/dev/null || true)"
  if ! is_pid_running "$pid"; then
    rm -f "$RUNNER_PID_FILE"
  fi
}

run_foreground() {
  ensure_runtime
  cd "$ROOT_DIR"

  echo "$$" >"$RUNNER_PID_FILE"
  # shellcheck disable=SC2064
  trap "rm -f '$RUNNER_PID_FILE'" EXIT

  "$AUTONOMY_PYTHON" scripts/autonomy_runner.py \
    --impl-repo "$IMPL_REPO" \
    --test-repo "$TEST_REPO" \
    --tasks-file "$ROOT_DIR/config/tasks.json" \
    --state-file "$STATE_FILE" \
    --objective-file "$ROOT_DIR/config/objective.md" \
    --codex-schema "$ROOT_DIR/config/codex_result.schema.json" \
    --artifacts-dir "$AUTONOMY_DIR" \
    --heartbeat-file "$HEARTBEAT_FILE" \
    --lock-file "$LOCK_FILE" \
    --max-cycles "$MAX_CYCLES" \
    --max-attempts "$MAX_ATTEMPTS" \
    --max-retryable-blocked-retries "$MAX_RETRYABLE_BLOCKED_RETRIES" \
    --retry-backoff-base-sec "$RETRY_BACKOFF_BASE_SEC" \
    --retry-backoff-max-sec "$RETRY_BACKOFF_MAX_SEC" \
    --git-lock-stale-sec "$GIT_LOCK_STALE_SEC" \
    --validation-retries "$VALIDATION_RETRIES" \
    --idle-sleep-sec "$IDLE_SLEEP_SEC" \
    --agent-timeout-sec "$AGENT_TIMEOUT" \
    --validate-timeout-sec "$VALIDATE_TIMEOUT"
}

supervise_foreground() {
  ensure_runtime
  cd "$ROOT_DIR"

  echo "$$" >"$SUPERVISOR_PID_FILE"
  # shellcheck disable=SC2064
  trap "rm -f '$SUPERVISOR_PID_FILE'" EXIT

  local restart_count=0
  local backoff="$SUPERVISOR_RESTART_DELAY_SEC"

  while true; do
    local child
    local rc

    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] supervisor: launching runner" >>"$LOG_FILE"
    set +e
    "$ROOT_DIR/scripts/autonomy_manager.sh" run >>"$LOG_FILE" 2>&1 &
    child=$!
    set -e
    echo "$child" >"$RUNNER_PID_FILE"

    while is_pid_running "$child"; do
      sleep "$HEARTBEAT_POLL_SEC" || true
      if heartbeat_is_stale; then
        local age
        age="$(heartbeat_age_seconds)"
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] supervisor: stale heartbeat (${age}s), restarting runner pid=$child" >>"$LOG_FILE"
        kill "$child" >/dev/null 2>&1 || true
        sleep 2 || true
        if is_pid_running "$child"; then
          kill -9 "$child" >/dev/null 2>&1 || true
        fi
        break
      fi
    done

    set +e
    wait "$child"
    rc=$?
    set -e
    cleanup_runner_pid

    if [[ "$rc" -eq 0 ]]; then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] supervisor: runner exited cleanly" >>"$LOG_FILE"
      return 0
    fi

    restart_count=$((restart_count + 1))
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] supervisor: runner exited rc=$rc restart_count=$restart_count" >>"$LOG_FILE"

    if (( SUPERVISOR_MAX_RESTARTS > 0 )) && (( restart_count >= SUPERVISOR_MAX_RESTARTS )); then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] supervisor: max restarts reached, stopping" >>"$LOG_FILE"
      return "$rc"
    fi

    sleep "$backoff" || true
    backoff=$((backoff * 2))
    if (( backoff > SUPERVISOR_MAX_BACKOFF_SEC )); then
      backoff="$SUPERVISOR_MAX_BACKOFF_SEC"
    fi
  done
}

start_background() {
  if is_supervisor_running; then
    echo "autonomy supervisor already running (pid=$(cat "$SUPERVISOR_PID_FILE"))"
    exit 0
  fi

  ensure_runtime
  mkdir -p "$AUTONOMY_DIR"
  nohup "$ROOT_DIR/scripts/autonomy_manager.sh" supervise >>"$LOG_FILE" 2>&1 &
  echo $! >"$SUPERVISOR_PID_FILE"

  sleep 2
  if is_supervisor_running; then
    echo "autonomy supervisor started (pid=$(cat "$SUPERVISOR_PID_FILE"))"
    echo "log file: $LOG_FILE"
    return
  fi

  echo "autonomy supervisor failed to stay up. Recent logs:"
  if [[ -f "$LOG_FILE" ]]; then
    tail -n 60 "$LOG_FILE"
  fi
  rm -f "$SUPERVISOR_PID_FILE"
  exit 1
}

stop_background() {
  local supervisor_pid
  local runner_pid

  supervisor_pid="$(read_pid "$SUPERVISOR_PID_FILE" 2>/dev/null || true)"
  runner_pid="$(read_pid "$RUNNER_PID_FILE" 2>/dev/null || true)"

  if is_pid_running "$supervisor_pid"; then
    kill "$supervisor_pid" >/dev/null 2>&1 || true
    sleep 1
    if is_pid_running "$supervisor_pid"; then
      kill -9 "$supervisor_pid" >/dev/null 2>&1 || true
    fi
  fi

  if is_pid_running "$runner_pid"; then
    kill "$runner_pid" >/dev/null 2>&1 || true
    sleep 1
    if is_pid_running "$runner_pid"; then
      kill -9 "$runner_pid" >/dev/null 2>&1 || true
    fi
  fi

  rm -f "$SUPERVISOR_PID_FILE" "$RUNNER_PID_FILE"
  echo "autonomy supervisor stopped"
}

status_background() {
  local age
  cleanup_runner_pid
  if ! is_supervisor_running; then
    rm -f "$SUPERVISOR_PID_FILE"
  fi
  age="$(heartbeat_age_seconds || echo -1)"

  if is_supervisor_running; then
    echo "autonomy supervisor running (pid=$(cat "$SUPERVISOR_PID_FILE"))"
  else
    echo "autonomy supervisor not running"
  fi

  if is_runner_running; then
    echo "autonomy runner running (pid=$(cat "$RUNNER_PID_FILE"))"
  else
    echo "autonomy runner not running"
  fi

  if [[ "$age" != "-1" ]]; then
    echo "heartbeat age: ${age}s (stale threshold: ${HEARTBEAT_STALE_SEC}s)"
  else
    echo "heartbeat age: unavailable"
  fi

  if [[ -f "$STATE_FILE" ]]; then
    echo "state file: $STATE_FILE"
  fi
  if [[ -f "$LOG_FILE" ]]; then
    echo "last log lines:"
    tail -n 20 "$LOG_FILE"
  fi
}

ensure_background() {
  if ! is_supervisor_running; then
    echo "autonomy supervisor not running; starting"
    start_background
    return
  fi

  if is_runner_running && heartbeat_is_stale; then
    local runner_pid
    runner_pid="$(cat "$RUNNER_PID_FILE")"
    echo "runner heartbeat stale; requesting restart of pid=$runner_pid"
    kill "$runner_pid" >/dev/null 2>&1 || true
    sleep 2
    if is_pid_running "$runner_pid"; then
      kill -9 "$runner_pid" >/dev/null 2>&1 || true
    fi
  fi

  echo "autonomy supervisor ensured"
}

reset_state() {
  rm -f "$STATE_FILE"
  echo "cleared state file: $STATE_FILE"
}

usage() {
  cat <<EOF
Usage: scripts/autonomy_manager.sh <command>

Commands:
  run        Run autonomy runner in foreground
  supervise  Run resilience supervisor in foreground
  start      Start supervisor in background
  stop       Stop background supervisor and runner
  ensure     Start if stopped, or restart stale runner
  status     Show status, heartbeat, and recent logs
  logs       Tail runner logs
  reset      Reset autonomy state file
EOF
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    run)
      run_foreground
      ;;
    supervise)
      supervise_foreground
      ;;
    start)
      start_background
      ;;
    stop)
      stop_background
      ;;
    ensure)
      ensure_background
      ;;
    status)
      status_background
      ;;
    logs)
      mkdir -p "$AUTONOMY_DIR"
      touch "$LOG_FILE"
      tail -f "$LOG_FILE"
      ;;
    reset)
      reset_state
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
