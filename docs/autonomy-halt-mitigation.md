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

## 6) Laptop Offline / Closed -- No Cloud Failover

Current limitation (issue #62): When the laptop loses power, network, or
the lid is closed during travel, all local autonomous work stops. There is no
deployed cloud worker to pick up queued tasks.

Design note: The infrastructure for cloud failover already exists at two
levels:

1. Autopilot cloud worker (`~/.claude/autopilot/cloud/cloud_worker.py`) --
   a Flask + Anthropic API service designed for Google Cloud Run. It monitors
   a heartbeat from the local autopilot; when the local machine goes silent
   for > 10 minutes, the cloud worker begins processing queued prompts. This
   is not yet deployed.

2. Swarm orchestrator async executor (`src/orxaq_autonomy/swarm_orchestrator.py`) --
   routes tasks across L0-L3 tiers. Cloud tiers (L2/L3) already handle task
   execution via Anthropic, OpenRouter, and other providers when the network
   is online. This covers the routing/execution layer but not the supervisory
   heartbeat-based takeover.

TODO -- to enable full cloud failover:

- Deploy the cloud worker to Google Cloud Run (`~/.claude/autopilot/cloud/`).
- Set `cloud_url` in autopilot config: `python3 autopilot.py set-cloud-url <url>`.
- Verify heartbeat sync: `python3 autopilot.py cloud-sync`.
- Consider adding a GitHub Actions workflow as a tertiary fallback that
  checks the heartbeat via Firestore and triggers task processing.

This is tracked as an enhancement rather than a bug because the swarm
orchestrator cloud providers already handle cloud execution when the
laptop is online. The gap is only for the fully-offline/asleep scenario.
