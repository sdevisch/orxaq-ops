.PHONY: run start stop status logs reset

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
