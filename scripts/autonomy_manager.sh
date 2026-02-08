#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTONOMY_DIR="$ROOT_DIR/artifacts/autonomy"
PID_FILE="$AUTONOMY_DIR/runner.pid"
LOG_FILE="$AUTONOMY_DIR/runner.log"
STATE_FILE="$ROOT_DIR/state/state.json"
ENV_FILE="${ORXAQ_AUTONOMY_ENV_FILE:-$ROOT_DIR/.env.autonomy}"

DEFAULT_IMPL_REPO="$ROOT_DIR/../orxaq"
IMPL_REPO="${ORXAQ_IMPL_REPO:-$DEFAULT_IMPL_REPO}"
DEFAULT_TEST_REPO="$ROOT_DIR/../orxaq_gemini"
TEST_REPO="${ORXAQ_TEST_REPO:-$DEFAULT_TEST_REPO}"
MAX_CYCLES="${ORXAQ_AUTONOMY_MAX_CYCLES:-10000}"
MAX_ATTEMPTS="${ORXAQ_AUTONOMY_MAX_ATTEMPTS:-5}"
AGENT_TIMEOUT="${ORXAQ_AUTONOMY_AGENT_TIMEOUT_SEC:-3600}"
VALIDATE_TIMEOUT="${ORXAQ_AUTONOMY_VALIDATE_TIMEOUT_SEC:-1800}"
AUTONOMY_PYTHON="${ORXAQ_AUTONOMY_PYTHON:-$ROOT_DIR/.venv/bin/python3}"

load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
  fi
}

require_bins() {
  command -v codex >/dev/null 2>&1 || { echo "codex CLI not found in PATH"; exit 1; }
  command -v gemini >/dev/null 2>&1 || { echo "gemini CLI not found in PATH"; exit 1; }
}

check_key() {
  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "OPENAI_API_KEY is not set. Add it to $ENV_FILE or export it in your shell."
    exit 1
  fi
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

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

run_foreground() {
  load_env
  require_bins
  check_key
  require_python
  cd "$ROOT_DIR"
  mkdir -p "$AUTONOMY_DIR"
  exec "$AUTONOMY_PYTHON" scripts/autonomy_runner.py \
    --impl-repo "$IMPL_REPO" \
    --test-repo "$TEST_REPO" \
    --tasks-file "$ROOT_DIR/config/tasks.json" \
    --state-file "$STATE_FILE" \
    --objective-file "$ROOT_DIR/config/objective.md" \
    --codex-schema "$ROOT_DIR/config/codex_result.schema.json" \
    --artifacts-dir "$AUTONOMY_DIR" \
    --max-cycles "$MAX_CYCLES" \
    --max-attempts "$MAX_ATTEMPTS" \
    --agent-timeout-sec "$AGENT_TIMEOUT" \
    --validate-timeout-sec "$VALIDATE_TIMEOUT"
}

start_background() {
  if is_running; then
    echo "autonomy runner already running (pid=$(cat "$PID_FILE"))"
    exit 0
  fi
  mkdir -p "$AUTONOMY_DIR"
  (
    cd "$ROOT_DIR"
    nohup "$ROOT_DIR/scripts/autonomy_manager.sh" run >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
  )
  sleep 2
  if is_running; then
    echo "autonomy runner started (pid=$(cat "$PID_FILE"))"
    echo "log file: $LOG_FILE"
    return
  fi
  echo "autonomy runner failed to stay up. Recent logs:"
  if [[ -f "$LOG_FILE" ]]; then
    tail -n 40 "$LOG_FILE"
  fi
  rm -f "$PID_FILE"
  exit 1
}

stop_background() {
  if ! is_running; then
    echo "autonomy runner is not running"
    rm -f "$PID_FILE"
    exit 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid" >/dev/null 2>&1 || true
  sleep 1
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$PID_FILE"
  echo "autonomy runner stopped"
}

status_background() {
  if is_running; then
    echo "autonomy runner running (pid=$(cat "$PID_FILE"))"
  else
    echo "autonomy runner not running"
  fi
  if [[ -f "$STATE_FILE" ]]; then
    echo "state file: $STATE_FILE"
  fi
  if [[ -f "$LOG_FILE" ]]; then
    echo "last log lines:"
    tail -n 20 "$LOG_FILE"
  fi
}

reset_state() {
  rm -f "$STATE_FILE"
  echo "cleared state file: $STATE_FILE"
}

usage() {
  cat <<EOF
Usage: scripts/autonomy_manager.sh <command>

Commands:
  run      Run autonomy runner in foreground
  start    Start autonomy runner in background
  stop     Stop background runner
  status   Show runner status and recent logs
  logs     Tail runner logs
  reset    Reset autonomy state file
EOF
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    run)
      run_foreground
      ;;
    start)
      start_background
      ;;
    stop)
      stop_background
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
