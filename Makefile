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

.PHONY: run supervise start stop ensure status monitor metrics health process-watchdog full-autonomy logs reset preflight preflight-autonomy bootstrap dashboard dashboard-start dashboard-ensure dashboard-status dashboard-stop dashboard-logs conversations lanes-plan lanes-status lanes-start lanes-ensure lanes-stop mesh-init mesh-status mesh-publish mesh-dispatch mesh-import mesh-export mesh-sync mesh-autonomy-once workspace open-vscode open-cursor open-pycharm install-keepalive uninstall-keepalive keepalive-status routellm-preflight routellm-bootstrap routellm-start routellm-ensure routellm-status routellm-stop routellm-full-auto-discover routellm-full-auto-prepare routellm-full-auto-dry-run routellm-full-auto-run routing-sota-full-auto-discover routing-sota-full-auto-prepare routing-sota-full-auto-dry-run routing-sota-full-auto-run local-model-fleet-probe local-model-fleet-benchmark local-model-fleet-sync local-model-fleet-capability-scan local-model-fleet-full-cycle model-router-connectivity provider-cost-ingest provider-cost-health provider-cost-ingest-check remote-heartbeat-start remote-heartbeat-stop remote-heartbeat-status local-idle-guard-once local-idle-guard-start local-idle-guard-stop local-idle-guard-status local-model-watchdog-once local-model-watchdog-start local-model-watchdog-stop local-model-watchdog-status swarm-todo-health-once swarm-todo-health-start swarm-todo-health-stop swarm-todo-health-status lint test version-check repo-hygiene hosted-controls-check readiness-check readiness-check-autonomy bump-patch bump-minor bump-major package setup pre-commit pre-push

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
	$(PYTHON) scripts/check_provider_cost_health.py

provider-cost-ingest-check:
	$(PYTHON) scripts/provider_cost_ingest.py
	$(PYTHON) scripts/check_provider_cost_health.py

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
