PYTHON ?= python3
ROOT := $(CURDIR)
export PYTHONPATH := $(ROOT)/src:$(PYTHONPATH)
AUTONOMY := $(PYTHON) -m orxaq_autonomy.cli --root $(ROOT)

.PHONY: run supervise start stop ensure status monitor health logs reset preflight bootstrap dashboard workspace open-vscode open-cursor open-pycharm install-keepalive uninstall-keepalive keepalive-status lint test version-check repo-hygiene hosted-controls-check readiness-check bump-patch bump-minor bump-major package setup pre-commit pre-push

run:
	$(AUTONOMY) run

supervise:
	$(AUTONOMY) supervise

start:
	$(AUTONOMY) start

stop:
	$(AUTONOMY) stop

ensure:
	$(AUTONOMY) ensure

status:
	$(AUTONOMY) status

monitor:
	$(AUTONOMY) monitor

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
	$(AUTONOMY) dashboard

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
