.PHONY: run start stop status logs reset preflight workspace open-vscode

run:
	./scripts/autonomy_manager.sh run

start:
	./scripts/autonomy_manager.sh start

stop:
	./scripts/autonomy_manager.sh stop

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
