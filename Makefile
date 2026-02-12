PYTHON ?= python3
ROOT := $(CURDIR)
export PYTHONPATH := $(ROOT)/src:$(PYTHONPATH)
AUTONOMY := $(PYTHON) -m orxaq_autonomy.cli --root $(ROOT)
CODEX_AUTONOMY_WRAPPER ?= /Users/sdevisch/dev/tools/codex-autonomy/codex-autonomy.sh
AUTONOMY_ACTIVE_REF ?= $(shell git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --verify HEAD 2>/dev/null || echo HEAD)
AUTONOMY_DIRTY_TREE := $(strip $(shell git status --porcelain --untracked-files=no 2>/dev/null))
AUTONOMY_ALLOW_INPLACE_DIRTY ?= 0
AUTONOMY_EXEC_ISOLATION_ARGS ?= $(if $(filter 1 true yes,$(AUTONOMY_ALLOW_INPLACE_DIRTY)),$(if $(AUTONOMY_DIRTY_TREE),--no-worktree-isolation,),)
ROUTELLM_FULL_AUTO_TASK ?= routellm-npv-autonomy
ROUTELLM_BASE_REF ?= $(AUTONOMY_ACTIVE_REF)
ROUTELLM_EXEC_ARGS ?= $(AUTONOMY_EXEC_ISOLATION_ARGS)
ROUTELLM_TASKS_FILE ?= $(ROOT)/config/lanes/codex_routellm_npv_tasks.json
ROUTELLM_OBJECTIVE_FILE ?= $(ROOT)/config/objectives/codex_routellm_npv.md
ROUTELLM_CODEX_PROMPT_FILE ?= $(ROOT)/config/prompts/codex_routellm_npv_prompt.md
ROUTELLM_MCP_CONTEXT_FILE ?= $(ROOT)/config/mcp_context.routellm_npv.example.json
ROUTELLM_ENV_OVERRIDES := ORXAQ_AUTONOMY_TASKS_FILE=$(ROUTELLM_TASKS_FILE) ORXAQ_AUTONOMY_OBJECTIVE_FILE=$(ROUTELLM_OBJECTIVE_FILE) ORXAQ_AUTONOMY_CODEX_PROMPT_FILE=$(ROUTELLM_CODEX_PROMPT_FILE) ORXAQ_AUTONOMY_MCP_CONTEXT_FILE=$(ROUTELLM_MCP_CONTEXT_FILE)
ROUTING_SOTA_FULL_AUTO_TASK ?= routing-sota-autonomy
ROUTING_SOTA_PROMPT_FILE ?= $(ROOT)/config/prompts/codex_state_of_the_art_routing_autonomy_prompt.md
ROUTING_SOTA_BASE_REF ?= $(AUTONOMY_ACTIVE_REF)
ROUTING_SOTA_EXEC_ARGS ?= $(AUTONOMY_EXEC_ISOLATION_ARGS)
PROCESS_WATCHDOG_SCRIPT ?= /Users/sdevisch/.codex/skills/autonomous-process-watchdog/scripts/process_watchdog.py
SWARM_TODO_HEALTH_INTERVAL_SEC ?= 3600

.PHONY: run supervise start stop ensure status monitor metrics health process-watchdog full-autonomy logs reset preflight preflight-autonomy bootstrap dashboard dashboard-start dashboard-ensure dashboard-status dashboard-stop dashboard-logs conversations lanes-plan lanes-status lanes-start lanes-ensure lanes-stop mesh-init mesh-status mesh-publish mesh-dispatch mesh-import mesh-export mesh-sync mesh-autonomy-once workspace open-vscode open-cursor open-pycharm install-keepalive uninstall-keepalive keepalive-status routellm-preflight routellm-bootstrap routellm-start routellm-ensure routellm-status routellm-stop routellm-full-auto-discover routellm-full-auto-prepare routellm-full-auto-dry-run routellm-full-auto-run routing-sota-full-auto-discover routing-sota-full-auto-prepare routing-sota-full-auto-dry-run routing-sota-full-auto-run local-model-fleet-probe local-model-fleet-benchmark local-model-fleet-sync local-model-fleet-capability-scan local-model-fleet-full-cycle model-router-connectivity provider-cost-ingest provider-cost-health provider-cost-ingest-check provider-autobootstrap t1-basic-model-policy-check pr-tier-ratio-check privilege-policy-check git-delivery-policy-check git-delivery-baseline-capture git-hygiene-remediate git-hygiene-check backend-upgrade-policy-check api-interop-policy-check backlog-control-once backlog-control-start backlog-control-stop backlog-control-status cleanup-loop-once cleanup-loop-start cleanup-loop-start-fast cleanup-loop-stop cleanup-loop-status remote-heartbeat-start remote-heartbeat-stop remote-heartbeat-status local-idle-guard-once local-idle-guard-start local-idle-guard-stop local-idle-guard-status local-model-watchdog-once local-model-watchdog-start local-model-watchdog-stop local-model-watchdog-status swarm-todo-health-once swarm-todo-health-current-once swarm-todo-health-current-start swarm-todo-health-current-stop swarm-todo-health-current-status swarm-todo-health-start swarm-todo-health-stop swarm-todo-health-status swarm-health-strict swarm-health-operational swarm-health-snapshot swarm-ready-queue swarm-cycle-report lint test version-check repo-hygiene hosted-controls-check readiness-check readiness-check-autonomy bump-patch bump-minor bump-major package setup pre-commit pre-push

run:
	$(AUTONOMY) run

supervise:
	$(AUTONOMY) supervise

start:
	$(AUTONOMY) start

stop:
	$(AUTONOMY) stop

routellm-preflight:
	$(ROUTELLM_ENV_OVERRIDES) $(AUTONOMY) preflight --allow-dirty

routellm-bootstrap:
	$(ROUTELLM_ENV_OVERRIDES) $(AUTONOMY) bootstrap

routellm-start:
	$(ROUTELLM_ENV_OVERRIDES) $(AUTONOMY) start

routellm-ensure:
	$(ROUTELLM_ENV_OVERRIDES) $(AUTONOMY) ensure

routellm-status:
	$(ROUTELLM_ENV_OVERRIDES) $(AUTONOMY) status

routellm-stop:
	$(AUTONOMY) stop

routellm-full-auto-discover:
	$(CODEX_AUTONOMY_WRAPPER) discover --repo-root $(ROOT)

routellm-full-auto-prepare:
	$(CODEX_AUTONOMY_WRAPPER) prepare --repo-root $(ROOT) --task "$(ROUTELLM_FULL_AUTO_TASK)" --base-ref "$(ROUTELLM_BASE_REF)"

routellm-full-auto-dry-run:
	$(CODEX_AUTONOMY_WRAPPER) exec --repo-root $(ROOT) --task "$(ROUTELLM_FULL_AUTO_TASK)" --base-ref "$(ROUTELLM_BASE_REF)" $(ROUTELLM_EXEC_ARGS) --prompt-file $(ROUTELLM_CODEX_PROMPT_FILE) --dry-run

routellm-full-auto-run:
	$(CODEX_AUTONOMY_WRAPPER) exec --repo-root $(ROOT) --task "$(ROUTELLM_FULL_AUTO_TASK)" --base-ref "$(ROUTELLM_BASE_REF)" $(ROUTELLM_EXEC_ARGS) --prompt-file $(ROUTELLM_CODEX_PROMPT_FILE)

routing-sota-full-auto-discover:
	$(CODEX_AUTONOMY_WRAPPER) discover --repo-root $(ROOT)

routing-sota-full-auto-prepare:
	$(CODEX_AUTONOMY_WRAPPER) prepare --repo-root $(ROOT) --task "$(ROUTING_SOTA_FULL_AUTO_TASK)" --base-ref "$(ROUTING_SOTA_BASE_REF)"

routing-sota-full-auto-dry-run:
	$(CODEX_AUTONOMY_WRAPPER) exec --repo-root $(ROOT) --task "$(ROUTING_SOTA_FULL_AUTO_TASK)" --base-ref "$(ROUTING_SOTA_BASE_REF)" $(ROUTING_SOTA_EXEC_ARGS) --prompt-file $(ROUTING_SOTA_PROMPT_FILE) --dry-run

routing-sota-full-auto-run:
	$(CODEX_AUTONOMY_WRAPPER) exec --repo-root $(ROOT) --task "$(ROUTING_SOTA_FULL_AUTO_TASK)" --base-ref "$(ROUTING_SOTA_BASE_REF)" $(ROUTING_SOTA_EXEC_ARGS) --prompt-file $(ROUTING_SOTA_PROMPT_FILE)

local-model-fleet-probe:
	$(PYTHON) scripts/local_model_fleet.py probe

local-model-fleet-benchmark:
	$(PYTHON) scripts/local_model_fleet.py benchmark

local-model-fleet-sync:
	$(PYTHON) scripts/local_model_fleet.py sync

local-model-fleet-capability-scan:
	$(PYTHON) scripts/local_model_fleet.py capability-scan

local-model-fleet-full-cycle:
	$(PYTHON) scripts/local_model_fleet.py full-cycle

model-router-connectivity:
	$(PYTHON) scripts/model_router_connectivity.py --config $(ROOT)/config/litellm_swarm_router.json --output $(ROOT)/artifacts/model_connectivity.json

provider-cost-ingest:
	$(PYTHON) scripts/provider_cost_ingest.py

provider-cost-health:
	$(PYTHON) scripts/check_provider_cost_health.py --json --output $(ROOT)/artifacts/autonomy/provider_cost_health.json

provider-cost-ingest-check:
	$(PYTHON) scripts/provider_cost_ingest.py
	$(PYTHON) scripts/check_provider_cost_health.py --json --output $(ROOT)/artifacts/autonomy/provider_cost_health.json

t1-basic-model-policy-check:
	$(PYTHON) scripts/check_t1_basic_model_policy.py --root $(ROOT) --policy-file $(ROOT)/config/t1_model_policy.json --metrics-file $(ROOT)/artifacts/autonomy/response_metrics.ndjson --output $(ROOT)/artifacts/autonomy/t1_basic_model_policy.json --json

pr-tier-ratio-check:
	$(PYTHON) scripts/check_pr_t1_ratio.py --root $(ROOT) --policy-file $(ROOT)/config/pr_tier_policy.json --output $(ROOT)/artifacts/autonomy/pr_tier_policy_health.json --json

privilege-policy-check:
	$(PYTHON) scripts/check_privilege_policy.py --root $(ROOT) --policy-file $(ROOT)/config/privilege_policy.json --output $(ROOT)/artifacts/autonomy/privilege_policy_health.json --json

git-delivery-policy-check:
	$(PYTHON) scripts/check_git_delivery_policy.py --root $(ROOT) --repo-root $(ROOT) --policy-file $(ROOT)/config/git_delivery_policy.json --output $(ROOT)/artifacts/autonomy/git_delivery_policy_health.json --json

.PHONY: git-delivery-baseline-capture
git-delivery-baseline-capture:
	$(PYTHON) scripts/check_git_delivery_policy.py --root $(ROOT) --repo-root $(ROOT) --policy-file $(ROOT)/config/git_delivery_policy.json --baseline-file $(ROOT)/artifacts/autonomy/git_delivery_baseline.json --capture-baseline --output $(ROOT)/artifacts/autonomy/git_delivery_policy_health.json --json

.PHONY: pr-approval-remediate
pr-approval-remediate:
	$(PYTHON) scripts/remediate_pr_approvals.py --root $(ROOT) --repo Orxaq/orxaq-ops --repo Orxaq/orxaq --output $(ROOT)/artifacts/autonomy/pr_approval_remediation.json --json

git-hygiene-check:
	$(PYTHON) scripts/check_git_hygiene_health.py --root $(ROOT) --repo-root $(ROOT) --policy-file $(ROOT)/config/git_hygiene_policy.json --output $(ROOT)/artifacts/autonomy/git_hygiene_health.json --json

git-hygiene-remediate:
	$(PYTHON) scripts/remediate_git_hygiene.py --root $(ROOT) --repo $(ROOT) --repo ../orxaq --stale-days 1 --max-remote-deletes 250 --max-local-deletes 250 --archive-unmerged-branches --archive-unmerged-min-age-days 1 --archive-tag-namespace archive/branch-debt --no-archive-push-remote-tags --apply --output $(ROOT)/artifacts/autonomy/git_hygiene_remediation.json --json

backend-upgrade-policy-check:
	$(PYTHON) scripts/check_backend_upgrade_policy.py --root $(ROOT) --backend-policy-file $(ROOT)/config/backend_portfolio_policy.json --upgrade-policy-file $(ROOT)/config/upgrade_lifecycle_policy.json --backlog-file ../orxaq/ops/backlog/distributed_todo.yaml --output $(ROOT)/artifacts/autonomy/backend_upgrade_policy_health.json --json

api-interop-policy-check:
	$(PYTHON) scripts/check_api_interop_policy.py --root $(ROOT) --policy-file $(ROOT)/config/api_interop_policy.json --backlog-file ../orxaq/ops/backlog/distributed_todo.yaml --output $(ROOT)/artifacts/autonomy/api_interop_policy_health.json --json

backlog-control-once:
	-$(PYTHON) scripts/deterministic_backlog_control.py --root $(ROOT) --policy-file $(ROOT)/config/deterministic_backlog_policy.json --output-file $(ROOT)/artifacts/autonomy/deterministic_backlog_health.json --history-file $(ROOT)/artifacts/autonomy/deterministic_backlog_history.ndjson --apply --json

backlog-control-start:
	@mkdir -p $(ROOT)/artifacts/autonomy
	@if [ -f $(ROOT)/artifacts/autonomy/deterministic_backlog.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/deterministic_backlog.pid) 2>/dev/null; then \
		echo "deterministic backlog loop already running pid=$$(cat $(ROOT)/artifacts/autonomy/deterministic_backlog.pid)"; \
	else \
		$(PYTHON) scripts/deterministic_backlog_control.py --daemon --root $(ROOT) --policy-file $(ROOT)/config/deterministic_backlog_policy.json --output-file $(ROOT)/artifacts/autonomy/deterministic_backlog_health.json --history-file $(ROOT)/artifacts/autonomy/deterministic_backlog_history.ndjson --pid-file $(ROOT)/artifacts/autonomy/deterministic_backlog.pid --log-file $(ROOT)/artifacts/autonomy/deterministic_backlog.log --apply >/dev/null; \
		echo "deterministic backlog loop started pid=$$(cat $(ROOT)/artifacts/autonomy/deterministic_backlog.pid)"; \
	fi

backlog-control-stop:
	@if [ -f $(ROOT)/artifacts/autonomy/deterministic_backlog.pid ]; then \
		PID=$$(cat $(ROOT)/artifacts/autonomy/deterministic_backlog.pid); \
		if kill -0 $$PID 2>/dev/null; then kill $$PID; fi; \
		rm -f $(ROOT)/artifacts/autonomy/deterministic_backlog.pid; \
		echo "deterministic backlog loop stopped"; \
	else \
		echo "deterministic backlog loop not running"; \
	fi

backlog-control-status:
	@if [ -f $(ROOT)/artifacts/autonomy/deterministic_backlog.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/deterministic_backlog.pid) 2>/dev/null; then \
		echo "running pid=$$(cat $(ROOT)/artifacts/autonomy/deterministic_backlog.pid)"; \
		tail -n 8 $(ROOT)/artifacts/autonomy/deterministic_backlog.log; \
	else \
		echo "stopped"; \
		[ -f $(ROOT)/artifacts/autonomy/deterministic_backlog.log ] && tail -n 8 $(ROOT)/artifacts/autonomy/deterministic_backlog.log || true; \
	fi

provider-autobootstrap:
	bash scripts/provider_autobootstrap.sh

cleanup-loop-once:
	$(PYTHON) scripts/health_green_loop.py --root $(ROOT) --interval-sec 3600 --low-codex-model gpt-5-mini

cleanup-loop-start:
	@mkdir -p $(ROOT)/artifacts/autonomy/health_green_loop
	@if [ -f $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid) 2>/dev/null; then \
		echo "health-green loop already running pid=$$(cat $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid)"; \
	else \
		$(PYTHON) scripts/health_green_loop.py --daemon --root $(ROOT) --interval-sec 3600 --low-codex-model gpt-5-mini --pid-file $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid --log-file $(ROOT)/artifacts/autonomy/health_green_loop/loop.log --output-file $(ROOT)/artifacts/autonomy/health_green_loop/latest.json --history-file $(ROOT)/artifacts/autonomy/health_green_loop/history.ndjson --lock-file $(ROOT)/artifacts/autonomy/health_green_loop/loop.lock >/dev/null; \
		echo "health-green loop started pid=$$(cat $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid)"; \
	fi

cleanup-loop-start-fast: cleanup-loop-start

cleanup-loop-stop:
	@if [ -f $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid ]; then \
		PID=$$(cat $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid); \
		if kill -0 $$PID 2>/dev/null; then kill $$PID; fi; \
		rm -f $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid; \
		echo "health-green loop stopped"; \
	else \
		echo "health-green loop not running"; \
	fi

cleanup-loop-status:
	@if [ -f $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid) 2>/dev/null; then \
		echo "running pid=$$(cat $(ROOT)/artifacts/autonomy/health_green_loop/loop.pid)"; \
		tail -n 8 $(ROOT)/artifacts/autonomy/health_green_loop/loop.log; \
	else \
		echo "stopped"; \
		[ -f $(ROOT)/artifacts/autonomy/health_green_loop/loop.log ] && tail -n 8 $(ROOT)/artifacts/autonomy/health_green_loop/loop.log || true; \
	fi

remote-heartbeat-start:
	@mkdir -p $(ROOT)/artifacts/autonomy/local_models
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid) 2>/dev/null; then \
		echo "remote heartbeat already running pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid)"; \
	else \
		$(PYTHON) scripts/lmstudio_remote_heartbeat.py --daemon --interval-sec 10 --pid-file $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid --log-file $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.log >/dev/null; \
		echo "remote heartbeat started pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid)"; \
	fi

remote-heartbeat-stop:
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid ]; then \
		PID=$$(cat $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid); \
		if kill -0 $$PID 2>/dev/null; then kill $$PID; fi; \
		rm -f $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid; \
		echo "remote heartbeat stopped"; \
	else \
		echo "remote heartbeat not running"; \
	fi

remote-heartbeat-status:
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid) 2>/dev/null; then \
		echo "running pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.pid)"; \
		tail -n 6 $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.log; \
	else \
		echo "stopped"; \
		[ -f $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.log ] && tail -n 6 $(ROOT)/artifacts/autonomy/local_models/remote_heartbeat.log || true; \
	fi

local-idle-guard-once:
	$(PYTHON) scripts/local_model_idle_guard.py --root $(ROOT) --config $(ROOT)/config/local_model_idle_guard.json --once --json

local-idle-guard-start:
	@mkdir -p $(ROOT)/artifacts/autonomy/local_models
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid) 2>/dev/null; then \
		echo "local idle guard already running pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid)"; \
	else \
		$(PYTHON) scripts/local_model_idle_guard.py --daemon --root $(ROOT) --config $(ROOT)/config/local_model_idle_guard.json --json --pid-file $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid --log-file $(ROOT)/artifacts/autonomy/local_models/idle_guard.log >/dev/null; \
		echo "local idle guard started pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid)"; \
	fi

local-idle-guard-stop:
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid ]; then \
		PID=$$(cat $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid); \
		if kill -0 $$PID 2>/dev/null; then kill $$PID; fi; \
		rm -f $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid; \
		echo "local idle guard stopped"; \
	else \
		echo "local idle guard not running"; \
	fi

local-idle-guard-status:
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid) 2>/dev/null; then \
		echo "running pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/idle_guard.pid)"; \
		tail -n 8 $(ROOT)/artifacts/autonomy/local_models/idle_guard.log; \
	else \
		echo "stopped"; \
		[ -f $(ROOT)/artifacts/autonomy/local_models/idle_guard.log ] && tail -n 8 $(ROOT)/artifacts/autonomy/local_models/idle_guard.log || true; \
	fi

local-model-watchdog-once:
	$(PYTHON) $(PROCESS_WATCHDOG_SCRIPT) --config $(ROOT)/config/local_model_process_watchdog.json --state-file $(ROOT)/artifacts/autonomy/local_models/process_watchdog_state.json --history-file $(ROOT)/artifacts/autonomy/local_models/process_watchdog_history.ndjson --json

local-model-watchdog-start:
	@mkdir -p $(ROOT)/artifacts/autonomy/local_models
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid) 2>/dev/null; then \
		echo "local model watchdog already running pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid)"; \
	else \
		$(PYTHON) scripts/local_model_watchdog_daemon.py --daemon --root $(ROOT) --watchdog-script $(PROCESS_WATCHDOG_SCRIPT) --config $(ROOT)/config/local_model_process_watchdog.json --state-file $(ROOT)/artifacts/autonomy/local_models/process_watchdog_state.json --history-file $(ROOT)/artifacts/autonomy/local_models/process_watchdog_history.ndjson --interval-sec 20 --pid-file $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid --log-file $(ROOT)/artifacts/autonomy/local_models/process_watchdog.log >/dev/null; \
		echo "local model watchdog started pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid)"; \
	fi

local-model-watchdog-stop:
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid ]; then \
		PID=$$(cat $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid); \
		if kill -0 $$PID 2>/dev/null; then kill $$PID; fi; \
		rm -f $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid; \
		echo "local model watchdog stopped"; \
	else \
		echo "local model watchdog not running"; \
	fi

local-model-watchdog-status:
	@if [ -f $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid) 2>/dev/null; then \
		echo "running pid=$$(cat $(ROOT)/artifacts/autonomy/local_models/process_watchdog.pid)"; \
		tail -n 8 $(ROOT)/artifacts/autonomy/local_models/process_watchdog.log; \
	else \
		echo "stopped"; \
		[ -f $(ROOT)/artifacts/autonomy/local_models/process_watchdog.log ] && tail -n 8 $(ROOT)/artifacts/autonomy/local_models/process_watchdog.log || true; \
	fi

swarm-todo-health-once:
	$(PYTHON) scripts/swarm_distributed_todo_health.py --root $(ROOT) --json

swarm-todo-health-current-once:
	$(PYTHON) scripts/swarm_todo_health_current.py --root $(ROOT) --json

swarm-todo-health-current-start:
	@mkdir -p $(ROOT)/artifacts/autonomy/swarm_todo_health
	@if [ -f $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid) 2>/dev/null; then \
		echo "swarm/todo current-scope health daemon already running pid=$$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid)"; \
	else \
		$(PYTHON) scripts/swarm_todo_health_current.py --daemon --root $(ROOT) --interval-sec $(SWARM_TODO_HEALTH_INTERVAL_SEC) --json --pid-file $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid --log-file $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.log --output-file $(ROOT)/artifacts/autonomy/swarm_todo_health/current_latest.json --history-file $(ROOT)/artifacts/autonomy/swarm_todo_health/current_history.ndjson >/dev/null; \
		echo "swarm/todo current-scope health daemon started pid=$$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid)"; \
	fi

swarm-todo-health-current-stop:
	@if [ -f $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid ]; then \
		PID=$$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid); \
		if kill -0 $$PID 2>/dev/null; then kill $$PID; fi; \
		rm -f $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid; \
		echo "swarm/todo current-scope health daemon stopped"; \
	else \
		echo "swarm/todo current-scope health daemon not running"; \
	fi

swarm-todo-health-current-status:
	@if [ -f $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid) 2>/dev/null; then \
		echo "running pid=$$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.pid)"; \
		tail -n 8 $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.log; \
	else \
		echo "stopped"; \
		[ -f $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.log ] && tail -n 8 $(ROOT)/artifacts/autonomy/swarm_todo_health/current_health.log || true; \
	fi

swarm-todo-health-start:
	@mkdir -p $(ROOT)/artifacts/autonomy/swarm_todo_health
	@if [ -f $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid) 2>/dev/null; then \
		echo "swarm/todo health daemon already running pid=$$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid)"; \
	else \
		$(PYTHON) scripts/swarm_distributed_todo_health.py --daemon --root $(ROOT) --interval-sec $(SWARM_TODO_HEALTH_INTERVAL_SEC) --json --pid-file $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid --log-file $(ROOT)/artifacts/autonomy/swarm_todo_health/health.log --output-file $(ROOT)/artifacts/autonomy/swarm_todo_health/latest.json --history-file $(ROOT)/artifacts/autonomy/swarm_todo_health/history.ndjson >/dev/null; \
		echo "swarm/todo health daemon started pid=$$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid)"; \
	fi

swarm-todo-health-stop:
	@if [ -f $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid ]; then \
		PID=$$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid); \
		if kill -0 $$PID 2>/dev/null; then kill $$PID; fi; \
		rm -f $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid; \
		echo "swarm/todo health daemon stopped"; \
	else \
		echo "swarm/todo health daemon not running"; \
	fi

swarm-todo-health-status:
	@if [ -f $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid ] && kill -0 $$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid) 2>/dev/null; then \
		echo "running pid=$$(cat $(ROOT)/artifacts/autonomy/swarm_todo_health/health.pid)"; \
		tail -n 8 $(ROOT)/artifacts/autonomy/swarm_todo_health/health.log; \
	else \
		echo "stopped"; \
		[ -f $(ROOT)/artifacts/autonomy/swarm_todo_health/health.log ] && tail -n 8 $(ROOT)/artifacts/autonomy/swarm_todo_health/health.log || true; \
	fi

swarm-health-strict: model-router-connectivity
	$(PYTHON) ../orxaq/orxaq_cli.py swarm-health --root ../orxaq --output ../orxaq/artifacts/health.json --strict --connectivity-report ../orxaq-ops/artifacts/model_connectivity.json

swarm-health-operational: model-router-connectivity
	$(PYTHON) ../orxaq/orxaq_cli.py swarm-health --root ../orxaq --output ../orxaq/artifacts/health_operational.json --connectivity-report ../orxaq-ops/artifacts/model_connectivity.json --skip-quality-gates --skip-security-gates

swarm-health-snapshot: model-router-connectivity
	$(PYTHON) scripts/run_swarm_health_snapshot.py --ops-root $(ROOT) --source-root ../orxaq --connectivity-report artifacts/model_connectivity.json --strict-output artifacts/autonomy/health_snapshot/strict.json --operational-output artifacts/autonomy/health_snapshot/operational.json

swarm-ready-queue:
	$(PYTHON) scripts/swarm_ready_queue.py --root $(ROOT) --output $(ROOT)/artifacts/autonomy/ready_queue_week.json --max-items 21

swarm-cycle-report:
	-$(MAKE) swarm-health-snapshot
	-$(MAKE) t1-basic-model-policy-check
	-$(MAKE) pr-tier-ratio-check
	-$(MAKE) privilege-policy-check
	-$(MAKE) pr-approval-remediate
	-$(MAKE) git-delivery-policy-check
	-$(MAKE) git-hygiene-remediate
	-$(MAKE) git-hygiene-check
	-$(MAKE) backend-upgrade-policy-check
	-$(MAKE) api-interop-policy-check
	-$(MAKE) backlog-control-once
	-$(PYTHON) scripts/swarm_todo_health_current.py --root $(ROOT) --json
	-$(PYTHON) scripts/cleanup_loop.py --root $(ROOT)
	$(PYTHON) scripts/swarm_ready_queue.py --root $(ROOT) --output $(ROOT)/artifacts/autonomy/ready_queue_week.json --max-items 21
	$(PYTHON) scripts/swarm_cycle_report.py --root $(ROOT) --output $(ROOT)/artifacts/autonomy/swarm_cycle_report.json --markdown $(ROOT)/artifacts/autonomy/swarm_cycle_report.md --blocked-output $(ROOT)/artifacts/autonomy/blocked_cycle_escalations.json

ensure:
	$(AUTONOMY) ensure

status:
	$(AUTONOMY) status

monitor:
	$(AUTONOMY) monitor

metrics:
	$(AUTONOMY) metrics

health:
	$(AUTONOMY) health

process-watchdog:
	$(AUTONOMY) process-watchdog --strict

full-autonomy:
	$(AUTONOMY) full-autonomy --strict

logs:
	$(AUTONOMY) logs

reset:
	$(AUTONOMY) reset

preflight:
	$(AUTONOMY) preflight

preflight-autonomy:
	$(AUTONOMY) preflight --allow-dirty

bootstrap:
	$(AUTONOMY) bootstrap

dashboard:
	$(AUTONOMY) dashboard-start

dashboard-start:
	$(AUTONOMY) dashboard-start

dashboard-ensure:
	$(AUTONOMY) dashboard-ensure

dashboard-status:
	$(AUTONOMY) dashboard-status

dashboard-stop:
	$(AUTONOMY) dashboard-stop

dashboard-logs:
	$(AUTONOMY) dashboard-logs

conversations:
	$(AUTONOMY) conversations

lanes-plan:
	$(AUTONOMY) lanes-plan

lanes-status:
	$(AUTONOMY) lanes-status

lanes-start:
	$(AUTONOMY) lanes-start

lanes-ensure:
	$(AUTONOMY) lanes-ensure

lanes-stop:
	$(AUTONOMY) lanes-stop

mesh-init:
	$(AUTONOMY) mesh-init

mesh-status:
	$(AUTONOMY) mesh-status

mesh-publish:
	$(AUTONOMY) mesh-publish --topic scheduling --event-type task.enqueued --payload-json '{"task_id":"example"}'

mesh-dispatch:
	$(AUTONOMY) mesh-dispatch

mesh-import:
	$(AUTONOMY) mesh-import

mesh-export:
	$(AUTONOMY) mesh-export

mesh-sync:
	$(AUTONOMY) mesh-sync

mesh-autonomy-once:
	$(AUTONOMY) mesh-autonomy-once

workspace:
	$(AUTONOMY) workspace

open-vscode: workspace
	$(AUTONOMY) open-ide --ide vscode

open-cursor: workspace
	$(AUTONOMY) open-ide --ide cursor

open-pycharm:
	$(AUTONOMY) open-ide --ide pycharm

install-keepalive:
	$(AUTONOMY) install-keepalive

uninstall-keepalive:
	$(AUTONOMY) uninstall-keepalive

keepalive-status:
	$(AUTONOMY) keepalive-status

setup:
	$(PYTHON) -m pip install -e .
	@if command -v pre-commit >/dev/null 2>&1; then \
		pre-commit install; \
		pre-commit install --hook-type pre-push; \
		pre-commit install --hook-type commit-msg; \
	else \
		echo "pre-commit not installed; install it to enable git hooks."; \
	fi

pre-commit:
	pre-commit run --all-files

pre-push:
	pre-commit run --all-files --hook-stage push

lint:
	$(PYTHON) -m py_compile src/orxaq_autonomy/*.py scripts/autonomy_runner.py
	bash -n scripts/autonomy_manager.sh scripts/preflight.sh scripts/generate_workspace.sh scripts/open_vscode.sh scripts/install_keepalive.sh

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

version-check:
	$(PYTHON) scripts/check_version_policy.py

repo-hygiene:
	$(PYTHON) scripts/check_repo_hygiene.py --root .

hosted-controls-check:
	$(PYTHON) scripts/check_hosted_controls.py --root .

readiness-check: version-check repo-hygiene hosted-controls-check preflight

readiness-check-autonomy: version-check repo-hygiene hosted-controls-check preflight-autonomy

bump-patch:
	$(PYTHON) scripts/bump_version.py --part patch --apply

bump-minor:
	$(PYTHON) scripts/bump_version.py --part minor --apply

bump-major:
	$(PYTHON) scripts/bump_version.py --part major --apply

package:
	@if $(PYTHON) -m build --version >/dev/null 2>&1; then \
		$(PYTHON) -m build; \
	else \
		$(PYTHON) -m venv .pkg-venv; \
		./.pkg-venv/bin/python -m pip install --upgrade pip build; \
		./.pkg-venv/bin/python -m build; \
	fi
