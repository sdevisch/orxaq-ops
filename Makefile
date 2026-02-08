.PHONY: run supervise start stop ensure status logs reset preflight workspace open-vscode lint test

run:
	./scripts/autonomy_manager.sh run

supervise:
	./scripts/autonomy_manager.sh supervise

start:
	./scripts/autonomy_manager.sh start

stop:
	./scripts/autonomy_manager.sh stop

ensure:
	./scripts/autonomy_manager.sh ensure

status:
	./scripts/autonomy_manager.sh status

logs:
	./scripts/autonomy_manager.sh logs

reset:
	./scripts/autonomy_manager.sh reset

preflight:
	./scripts/preflight.sh

workspace:
	./scripts/generate_workspace.sh

open-vscode: workspace
	./scripts/open_vscode.sh

lint:
	python3 -m py_compile scripts/autonomy_runner.py
	bash -n scripts/autonomy_manager.sh scripts/preflight.sh scripts/generate_workspace.sh scripts/open_vscode.sh

test:
	python3 -m unittest discover -s tests -p 'test_*.py'
