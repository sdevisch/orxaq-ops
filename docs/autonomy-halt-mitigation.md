# Autonomy Halt Mitigation

This playbook maps frequent autonomy-stop causes to specific controls in `orxaq-ops`.

## 1) Terminal Commands Blocking on Prompts

Controls:
- Runner forces non-interactive env for all subprocesses (`CI=1`, `GIT_TERMINAL_PROMPT=0`, `PIP_NO_INPUT=1`).
- Subprocess stdin is disconnected (`DEVNULL`) so hidden prompt waits cannot block forever.
- Every command has a timeout and heartbeat progress updates.

## 2) Git Operations Stalling or Failing

Controls:
- Retry classification includes lock/prompt/auth transient signatures.
- Stale lock files (`.git/index.lock`, `HEAD.lock`, `packed-refs.lock`) are auto-healed when old and no git process is active.
- Prompt context warns about in-progress merge/rebase/cherry-pick states so agents resolve safely.

## 3) Test Runs Hanging or Failing Due Tooling Entrypoints

Controls:
- Validation for test commands retries before hard failure.
- `make test` falls back to `pytest` entrypoints when make targets are unavailable.
- Validation remains timeout-bound and emits heartbeat progress.

## 4) New/Unexpected File Types

Controls:
- Prompt includes current repository file-type profile from tracked files.
- Prompt enforces safe handling for unknown/binary types and `.gitattributes` updates where needed.

## 5) Runner/Supervisor Unexpected Exit

Controls:
- Supervisor restarts runner with exponential backoff.
- `ensure` command restarts service if stopped or stale.
- Optional OS keepalive installs a user-space scheduler entry:
  - Windows Task Scheduler task (`schtasks`) under current user.
  - macOS LaunchAgent (`~/Library/LaunchAgents`).

Mitigation Sequence:
1. Detect runner/supervisor exit
2. Log comprehensive diagnostics
3. Check last known good state
4. Attempt graceful restart
5. If restart fails, trigger manual review workflow
6. Preserve conversation and lane state for recovery
