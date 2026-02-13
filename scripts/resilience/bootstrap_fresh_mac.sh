#!/usr/bin/env bash
# bootstrap_fresh_mac.sh — Full environment setup on a fresh Mac
# Run this after restoring iCloud (automatic) or cloud backup (manual)
# Idempotent: safe to run multiple times
set -euo pipefail

DEV_DIR="${HOME}/dev"
VAULT_DIR="${HOME}/Library/Mobile Documents/com~apple~CloudDocs/orxaq-vault"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Source telemetry if available (may not exist on first bootstrap)
TELEMETRY_SCRIPT="bootstrap_fresh_mac"
if [[ -f "${SCRIPT_DIR}/telemetry.sh" ]]; then
    source "${SCRIPT_DIR}/telemetry.sh"
else
    # Stub out telemetry functions if not yet available
    emit_event() { :; }
    emit_metric() { :; }
    start_timer() { :; }
    end_timer() { :; }
    emit_disk_usage() { :; }
fi

start_timer "bootstrap"

# --- Phase 1: Prerequisites ---
start_timer "prerequisites"

if ! xcode-select -p &>/dev/null; then
    emit_event "xcode_install" "info"
    xcode-select --install
    until xcode-select -p &>/dev/null; do sleep 5; done
fi
emit_event "xcode_ready" "ok"

if ! command -v brew &>/dev/null; then
    emit_event "homebrew_install" "info"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
fi
emit_event "homebrew_ready" "ok"

TOOLS=(python@3.14 gh restic age ruff git node)
TOOLS_INSTALLED=0
for tool in "${TOOLS[@]}"; do
    if ! brew list "${tool}" &>/dev/null; then
        emit_event "tool_install" "info" tool="${tool}"
        brew install "${tool}"
        ((TOOLS_INSTALLED++))
    fi
done
emit_metric "tools_installed" "${TOOLS_INSTALLED}"

if ! command -v claude &>/dev/null; then
    emit_event "claude_cli_install" "info"
    npm install -g @anthropic-ai/claude-code 2>/dev/null || emit_event "claude_cli_manual" "warn"
fi

if ! gh auth status &>/dev/null; then
    emit_event "gh_auth_missing" "error"
    exit 1
fi
emit_event "gh_auth_ready" "ok"

end_timer "prerequisites" "ok" tools_installed="${TOOLS_INSTALLED}"

# --- Phase 2: Clone Repos ---
start_timer "clone_repos"

mkdir -p "${DEV_DIR}"
REPOS=(
    "Orxaq/orxaq"
    "Orxaq/orxaq-ops"
    "Orxaq/orxaq-pay"
    "Orxaq/swarm-orchestrator"
    "Orxaq/odyssey"
)
CLONED=0
PULLED=0

for repo in "${REPOS[@]}"; do
    name=$(basename "${repo}")
    target="${DEV_DIR}/${name}"
    if [[ -d "${target}/.git" ]]; then
        git -C "${target}" pull --ff-only 2>/dev/null || true
        ((PULLED++))
        emit_event "repo_pulled" "ok" repo="${name}"
    else
        gh repo clone "${repo}" "${target}" 2>/dev/null || emit_event "repo_clone_failed" "warn" repo="${name}"
        ((CLONED++))
        emit_event "repo_cloned" "ok" repo="${name}"
    fi
done

emit_metric "repos_cloned" "${CLONED}"
emit_metric "repos_pulled" "${PULLED}"
end_timer "clone_repos" "ok"

# --- Phase 3: Python Environments ---
start_timer "python_envs"

PYTHON="$(brew --prefix python@3.14)/bin/python3.14"
[[ -x "${PYTHON}" ]] || PYTHON="python3"
VENVS_CREATED=0

for repo_dir in orxaq orxaq-ops orxaq-pay swarm-orchestrator odyssey; do
    target="${DEV_DIR}/${repo_dir}"
    [[ -d "${target}" ]] || continue

    venv="${target}/.venv"
    if [[ ! -d "${venv}" ]]; then
        "${PYTHON}" -m venv "${venv}"
        ((VENVS_CREATED++))
        emit_event "venv_created" "ok" repo="${repo_dir}"
    fi

    if [[ -f "${target}/pyproject.toml" ]]; then
        "${venv}/bin/pip" install -e "${target}[dev]" --quiet 2>/dev/null || \
        "${venv}/bin/pip" install -e "${target}" --quiet 2>/dev/null || \
        emit_event "pip_install_failed" "warn" repo="${repo_dir}"
    elif [[ -f "${target}/requirements.txt" ]]; then
        "${venv}/bin/pip" install -r "${target}/requirements.txt" --quiet 2>/dev/null || true
    fi
done

emit_metric "venvs_created" "${VENVS_CREATED}"
end_timer "python_envs" "ok"

# --- Phase 4: Restore Secrets ---
start_timer "restore_secrets"
if [[ -d "${VAULT_DIR}" && -x "${SCRIPT_DIR}/decrypt_secrets.sh" ]]; then
    bash "${SCRIPT_DIR}/decrypt_secrets.sh"
    emit_event "secrets_restored" "ok"
else
    emit_event "secrets_deferred" "info" reason="vault_not_synced"
fi
end_timer "restore_secrets" "ok"

# --- Phase 5: Claude Config ---
start_timer "claude_config"
CLAUDE_MEM="${HOME}/.claude/projects/-Users-sdevisch-dev/memory"
mkdir -p "${CLAUDE_MEM}"
MEMORY_RESTORED=0

if [[ -d "${VAULT_DIR}/claude-memory" ]]; then
    for f in "${VAULT_DIR}/claude-memory"/*.md; do
        dest="${CLAUDE_MEM}/$(basename "$f")"
        if [[ ! -f "${dest}" && -f "$f" ]]; then
            cp "$f" "${dest}"
            ((MEMORY_RESTORED++))
        fi
    done
fi

if [[ ! -f "${DEV_DIR}/.claude/CLAUDE.md" && -f "${VAULT_DIR}/CLAUDE.md" ]]; then
    mkdir -p "${DEV_DIR}/.claude"
    cp "${VAULT_DIR}/CLAUDE.md" "${DEV_DIR}/.claude/CLAUDE.md"
fi

if [[ -d "${VAULT_DIR}/agents" && ! -d "${DEV_DIR}/.claude/agents" ]]; then
    mkdir -p "${DEV_DIR}/.claude/agents"
    cp "${VAULT_DIR}/agents"/*.md "${DEV_DIR}/.claude/agents/" 2>/dev/null || true
fi

emit_metric "memory_files_restored" "${MEMORY_RESTORED}"
end_timer "claude_config" "ok"

# --- Phase 6: LaunchAgents ---
start_timer "launchagents"
LAUNCHD_DIR="${SCRIPT_DIR}/launchd"
AGENTS_INSTALLED=0
if [[ -d "${LAUNCHD_DIR}" ]]; then
    for plist in "${LAUNCHD_DIR}"/*.plist; do
        if [[ -f "${plist}" ]]; then
            name=$(basename "${plist}")
            dest="${HOME}/Library/LaunchAgents/${name}"
            cp "${plist}" "${dest}"
            launchctl bootout "gui/$(id -u)/${name%.plist}" 2>/dev/null || true
            launchctl bootstrap "gui/$(id -u)" "${dest}" 2>/dev/null || true
            ((AGENTS_INSTALLED++))
            emit_event "launchagent_installed" "ok" name="${name}"
        fi
    done
fi
emit_metric "launchagents_installed" "${AGENTS_INSTALLED}"
end_timer "launchagents" "ok"

# --- Phase 7: Verification ---
start_timer "verification"
PASS=0
FAIL=0

check() {
    local desc="$1"
    local cmd="$2"
    if eval "${cmd}" &>/dev/null; then
        ((PASS++))
        emit_event "check_pass" "ok" check="${desc}"
    else
        ((FAIL++))
        emit_event "check_fail" "error" check="${desc}"
    fi
}

check "Python 3.14" "python3 --version | grep -q 3.14"
check "GitHub CLI" "gh auth status"
check "restic" "command -v restic"
check "age" "command -v age"
check "orxaq repo" "test -d ${DEV_DIR}/orxaq/.git"
check "orxaq-pay repo" "test -d ${DEV_DIR}/orxaq-pay/.git"
check "swarm-orchestrator repo" "test -d ${DEV_DIR}/swarm-orchestrator/.git"
check "orxaq venv" "test -d ${DEV_DIR}/orxaq/.venv"
check "Claude memory" "test -f ${CLAUDE_MEM}/MEMORY.md"
check "CLAUDE.md" "test -f ${DEV_DIR}/.claude/CLAUDE.md"
check "Telemetry" "test -f ${SCRIPT_DIR}/telemetry.sh"

emit_metric "checks_passed" "${PASS}"
emit_metric "checks_failed" "${FAIL}"
end_timer "verification" "$([ ${FAIL} -eq 0 ] && echo ok || echo error)"

end_timer "bootstrap" "$([ ${FAIL} -eq 0 ] && echo ok || echo error)" \
    passed="${PASS}" failed="${FAIL}"

echo ""
echo "Bootstrap Complete: ${PASS} passed, ${FAIL} failed"

if [[ ${FAIL} -gt 0 ]]; then
    exit 1
fi
