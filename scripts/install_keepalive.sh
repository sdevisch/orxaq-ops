#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.orxaq.autonomy.ensure"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$ROOT_DIR/artifacts/autonomy"
LOG_FILE="$LOG_DIR/ensure.log"

usage() {
  cat <<EOF
Usage: scripts/install_keepalive.sh [install|uninstall|status]

Commands:
  install    Install and load a user LaunchAgent that runs ensure every minute
  uninstall  Unload and remove the LaunchAgent
  status     Show LaunchAgent status
EOF
}

require_launchd() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "keepalive installer currently supports macOS launchd only"
    exit 1
  fi
  command -v launchctl >/dev/null 2>&1 || {
    echo "launchctl not found"
    exit 1
  }
}

install_agent() {
  require_launchd
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

  cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd ${ROOT_DIR} && ./scripts/autonomy_manager.sh ensure &gt;&gt; ${LOG_FILE} 2&gt;&amp;1</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_FILE}</string>
</dict>
</plist>
EOF

  launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
  launchctl enable "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true

  echo "installed keepalive LaunchAgent: $PLIST_PATH"
  echo "label: $LABEL"
}

uninstall_agent() {
  require_launchd
  launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
  rm -f "$PLIST_PATH"
  echo "removed keepalive LaunchAgent: $PLIST_PATH"
}

status_agent() {
  require_launchd
  if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    echo "keepalive active: $LABEL"
  else
    echo "keepalive not active: $LABEL"
  fi
  if [[ -f "$PLIST_PATH" ]]; then
    echo "plist: $PLIST_PATH"
  fi
}

main() {
  local cmd="${1:-install}"
  case "$cmd" in
    install)
      install_agent
      ;;
    uninstall)
      uninstall_agent
      ;;
    status)
      status_agent
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
