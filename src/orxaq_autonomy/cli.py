"""CLI entrypoint for reusable Orxaq autonomy package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .context import write_default_skill_protocol
from .dashboard import start_dashboard
from .ide import generate_workspace, open_in_ide
from .manager import (
    ManagerConfig,
    bootstrap_background,
    conversations_snapshot,
    dashboard_status_snapshot,
    ensure_background,
    ensure_dashboard_background,
    health_snapshot,
    install_keepalive,
    lane_status_snapshot,
    keepalive_status,
    load_lane_specs,
    preflight,
    reset_state,
    render_monitor_text,
    run_foreground,
    ensure_lanes_background,
    start_lanes_background,
    start_dashboard_background,
    start_background,
    status_snapshot,
    stop_lanes_background,
    stop_dashboard_background,
    stop_background,
    supervise_foreground,
    tail_logs,
    tail_dashboard_logs,
    monitor_snapshot,
    uninstall_keepalive,
)


def _config_from_args(args: argparse.Namespace) -> ManagerConfig:
    root = Path(args.root).resolve()
    env_file = Path(args.env_file).resolve() if args.env_file else None
    return ManagerConfig.from_root(root, env_file_override=env_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orxaq autonomy manager")
    parser.add_argument("--root", default=".", help="orxaq-ops root directory")
    parser.add_argument("--env-file", default="", help="optional path to .env.autonomy")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("supervise")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("ensure")
    sub.add_parser("status")
    sub.add_parser("health")
    monitor = sub.add_parser("monitor")
    monitor.add_argument("--json", action="store_true")
    pre = sub.add_parser("preflight")
    pre.add_argument("--allow-dirty", action="store_true")
    sub.add_parser("reset")
    sub.add_parser("logs")
    sub.add_parser("install-keepalive")
    sub.add_parser("uninstall-keepalive")
    sub.add_parser("keepalive-status")

    init_skill = sub.add_parser("init-skill-protocol")
    init_skill.add_argument("--output", default="config/skill_protocol.json")

    workspace = sub.add_parser("workspace")
    workspace.add_argument("--output", default="orxaq-dual-agent.code-workspace")

    open_ide = sub.add_parser("open-ide")
    open_ide.add_argument("--ide", choices=["vscode", "cursor", "pycharm"], default="vscode")
    open_ide.add_argument("--workspace", default="orxaq-dual-agent.code-workspace")

    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--workspace", default="orxaq-dual-agent.code-workspace")
    bootstrap.add_argument("--ide", choices=["vscode", "cursor", "pycharm", "none"], default="vscode")
    bootstrap.add_argument("--require-clean", action="store_true")
    bootstrap.add_argument("--skip-keepalive", action="store_true")

    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--refresh-sec", type=int, default=5)
    dashboard.add_argument("--no-browser", action="store_true")
    dashboard.add_argument("--port-scan", type=int, default=20)

    dashboard_start = sub.add_parser("dashboard-start")
    dashboard_start.add_argument("--host", default="127.0.0.1")
    dashboard_start.add_argument("--port", type=int, default=8765)
    dashboard_start.add_argument("--refresh-sec", type=int, default=5)
    dashboard_start.add_argument("--no-browser", action="store_true")

    dashboard_ensure = sub.add_parser("dashboard-ensure")
    dashboard_ensure.add_argument("--host", default="127.0.0.1")
    dashboard_ensure.add_argument("--port", type=int, default=8765)
    dashboard_ensure.add_argument("--refresh-sec", type=int, default=5)
    dashboard_ensure.add_argument("--no-browser", action="store_true")

    sub.add_parser("dashboard-stop")
    sub.add_parser("dashboard-status")
    dashboard_logs = sub.add_parser("dashboard-logs")
    dashboard_logs.add_argument("--lines", type=int, default=120)

    conversations = sub.add_parser("conversations")
    conversations.add_argument("--lines", type=int, default=200)
    conversations.add_argument("--no-lanes", action="store_true")

    lanes_plan = sub.add_parser("lanes-plan")
    lanes_plan.add_argument("--json", action="store_true")

    lanes_status = sub.add_parser("lanes-status")
    lanes_status.add_argument("--json", action="store_true")

    lanes_start = sub.add_parser("lanes-start")
    lanes_start.add_argument("--lane", default="")

    lanes_ensure = sub.add_parser("lanes-ensure")
    lanes_ensure.add_argument("--json", action="store_true")

    lanes_stop = sub.add_parser("lanes-stop")
    lanes_stop.add_argument("--lane", default="")

    args = parser.parse_args(argv)
    cfg = _config_from_args(args)

    try:
        if args.command == "run":
            return run_foreground(cfg)
        if args.command == "supervise":
            return supervise_foreground(cfg)
        if args.command == "start":
            start_background(cfg)
            return 0
        if args.command == "stop":
            stop_background(cfg)
            return 0
        if args.command == "ensure":
            ensure_background(cfg)
            return 0
        if args.command == "status":
            print(json.dumps(status_snapshot(cfg), indent=2, sort_keys=True))
            logs = tail_logs(cfg, latest_run_only=True)
            if logs:
                print("--- logs ---")
                print(logs)
            return 0
        if args.command == "health":
            print(json.dumps(health_snapshot(cfg), indent=2, sort_keys=True))
            return 0
        if args.command == "monitor":
            snapshot = monitor_snapshot(cfg)
            if args.json:
                print(json.dumps(snapshot, indent=2, sort_keys=True))
            else:
                print(render_monitor_text(snapshot))
            return 0
        if args.command == "preflight":
            payload = preflight(cfg, require_clean=not args.allow_dirty)
            print(json.dumps(payload, indent=2, sort_keys=True))
            if payload.get("clean", True):
                return 0
            return 1
        if args.command == "reset":
            reset_state(cfg)
            print(f"cleared state file: {cfg.state_file}")
            return 0
        if args.command == "logs":
            print(tail_logs(cfg, lines=200))
            return 0
        if args.command == "install-keepalive":
            label = install_keepalive(cfg)
            print(f"installed keepalive: {label}")
            return 0
        if args.command == "uninstall-keepalive":
            label = uninstall_keepalive(cfg)
            print(f"removed keepalive: {label}")
            return 0
        if args.command == "keepalive-status":
            print(json.dumps(keepalive_status(cfg), indent=2, sort_keys=True))
            return 0
        if args.command == "init-skill-protocol":
            out = (cfg.root_dir / args.output).resolve()
            write_default_skill_protocol(out)
            print(f"wrote skill protocol: {out}")
            return 0
        if args.command == "workspace":
            out = (cfg.root_dir / args.output).resolve()
            created = generate_workspace(cfg.root_dir, cfg.impl_repo, cfg.test_repo, out)
            print(f"workspace generated: {created}")
            return 0
        if args.command == "open-ide":
            ws = (cfg.root_dir / args.workspace).resolve()
            if not ws.exists() and args.ide in {"vscode", "cursor"}:
                generate_workspace(cfg.root_dir, cfg.impl_repo, cfg.test_repo, ws)
            print(open_in_ide(ide=args.ide, root=cfg.root_dir, workspace_file=ws))
            return 0
        if args.command == "bootstrap":
            ide = None if args.ide == "none" else args.ide
            payload = bootstrap_background(
                cfg,
                allow_dirty=not args.require_clean,
                install_keepalive_job=not args.skip_keepalive,
                ide=ide,
                workspace_filename=args.workspace,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload.get("ok") else 1
        if args.command == "dashboard":
            return start_dashboard(
                cfg,
                host=args.host,
                port=args.port,
                refresh_sec=args.refresh_sec,
                open_browser=not args.no_browser,
                port_scan=args.port_scan,
            )
        if args.command == "dashboard-start":
            snapshot = start_dashboard_background(
                cfg,
                host=args.host,
                port=args.port,
                refresh_sec=args.refresh_sec,
                open_browser=not args.no_browser,
            )
            print(json.dumps(snapshot, indent=2, sort_keys=True))
            return 0 if snapshot.get("running") else 1
        if args.command == "dashboard-ensure":
            snapshot = ensure_dashboard_background(
                cfg,
                host=args.host,
                port=args.port,
                refresh_sec=args.refresh_sec,
                open_browser=not args.no_browser,
            )
            print(json.dumps(snapshot, indent=2, sort_keys=True))
            return 0 if snapshot.get("running") else 1
        if args.command == "dashboard-stop":
            snapshot = stop_dashboard_background(cfg)
            print(json.dumps(snapshot, indent=2, sort_keys=True))
            return 0
        if args.command == "dashboard-status":
            print(json.dumps(dashboard_status_snapshot(cfg), indent=2, sort_keys=True))
            return 0
        if args.command == "dashboard-logs":
            print(tail_dashboard_logs(cfg, lines=args.lines))
            return 0
        if args.command == "conversations":
            payload = conversations_snapshot(
                cfg,
                lines=args.lines,
                include_lanes=not args.no_lanes,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if args.command == "lanes-plan":
            lanes = load_lane_specs(cfg)
            if args.json:
                print(json.dumps({"lanes_file": str(cfg.lanes_file), "lanes": lanes}, indent=2, sort_keys=True))
                return 0
            print(f"lanes_file: {cfg.lanes_file}")
            if not lanes:
                print("No lanes configured.")
                return 0
            for lane in lanes:
                status = "enabled" if lane["enabled"] else "disabled"
                print(f"- {lane['id']} ({lane['owner']}, {status})")
                print(f"  description: {lane['description']}")
                print(f"  impl_repo: {lane['impl_repo']}")
                print(f"  test_repo: {lane['test_repo']}")
                print(f"  tasks_file: {lane['tasks_file']}")
                print(f"  objective_file: {lane['objective_file']}")
                print(f"  exclusive_paths: {', '.join(lane['exclusive_paths']) if lane['exclusive_paths'] else '(none)'}")
            return 0
        if args.command == "lanes-status":
            snapshot = lane_status_snapshot(cfg)
            if args.json:
                print(json.dumps(snapshot, indent=2, sort_keys=True))
                return 0
            print(
                json.dumps(
                    {
                        "lanes_file": snapshot["lanes_file"],
                        "running_count": snapshot["running_count"],
                        "total_count": snapshot["total_count"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            for lane in snapshot["lanes"]:
                state = "running" if lane["running"] else "stopped"
                print(
                    f"- {lane['id']} [{lane['owner']}] {state} pid={lane['pid']} "
                    f"health={lane.get('health', 'unknown')} heartbeat_age={lane.get('heartbeat_age_sec', -1)}s"
                )
                if lane.get("error"):
                    print(f"  error: {lane['error']}")
            return 0
        if args.command == "lanes-start":
            lane_id = args.lane.strip() or None
            payload = start_lanes_background(cfg, lane_id=lane_id)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload.get("ok", False) else 1
        if args.command == "lanes-ensure":
            payload = ensure_lanes_background(cfg)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    json.dumps(
                        {
                            "ensured_count": payload["ensured_count"],
                            "started_count": payload["started_count"],
                            "restarted_count": payload["restarted_count"],
                            "failed_count": payload["failed_count"],
                            "ok": payload["ok"],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            return 0 if payload.get("ok", False) else 1
        if args.command == "lanes-stop":
            lane_id = args.lane.strip() or None
            payload = stop_lanes_background(cfg, lane_id=lane_id)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
    except (FileNotFoundError, RuntimeError) as err:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(err),
                    "command": args.command,
                    "root": str(cfg.root_dir),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
