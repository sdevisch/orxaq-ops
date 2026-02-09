PYTHON ?= python3
ROOT := $(CURDIR)
export PYTHONPATH := $(ROOT)/src:$(PYTHONPATH)
AUTONOMY := $(PYTHON) -m orxaq_autonomy.cli --root $(ROOT)
CODEX_AUTONOMY_WRAPPER ?= /Users/sdevisch/dev/tools/codex-autonomy/codex-autonomy.sh
AUTONOMY_ACTIVE_REF ?= $(shell git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --verify HEAD 2>/dev/null || echo HEAD)
AUTONOMY_DIRTY_TREE := $(strip $(shell git status --porcelain --untracked-files=no 2>/dev/null))
AUTONOMY_EXEC_ISOLATION_ARGS ?= $(if $(AUTONOMY_DIRTY_TREE),--no-worktree-isolation,)
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

.PHONY: run supervise start stop ensure status monitor metrics health logs reset preflight bootstrap dashboard dashboard-start dashboard-ensure dashboard-status dashboard-stop dashboard-logs conversations lanes-plan lanes-status lanes-start lanes-ensure lanes-stop workspace open-vscode open-cursor open-pycharm install-keepalive uninstall-keepalive keepalive-status routellm-preflight routellm-bootstrap routellm-start routellm-ensure routellm-status routellm-stop routellm-full-auto-discover routellm-full-auto-prepare routellm-full-auto-dry-run routellm-full-auto-run routing-sota-full-auto-discover routing-sota-full-auto-prepare routing-sota-full-auto-dry-run routing-sota-full-auto-run lint test version-check repo-hygiene hosted-controls-check readiness-check bump-patch bump-minor bump-major package setup pre-commit pre-push

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

logs:
	$(AUTONOMY) logs

reset:
	$(AUTONOMY) reset

preflight:
	$(AUTONOMY) preflight

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
