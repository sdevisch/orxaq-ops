"""CLI entrypoint for reusable Orxaq autonomy package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .context import write_default_skill_protocol
from .gitops import pr_merge, pr_open, pr_wait
from .ide import generate_workspace, open_in_ide
from .manager import (
    ManagerConfig,
    ensure_background,
    health_snapshot,
    install_keepalive,
    keepalive_status,
    preflight,
    reset_state,
    run_foreground,
    start_background,
    status_snapshot,
    stop_background,
    supervise_foreground,
    tail_logs,
    uninstall_keepalive,
)
from .profile import profile_apply
from .providers import run_providers_check
from .stop_report import build_stop_report, file_issue
from .task_queue import validate_task_queue_file


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
    stop_p = sub.add_parser("stop")
    stop_p.add_argument("--report", action="store_true", help="Also write AUTONOMY_STOP_REPORT.md.")
    stop_p.add_argument("--file-issue", action="store_true", help="Also file a GitHub issue with stop report.")
    stop_p.add_argument("--issue-title", default="AUTONOMY STOP: manual follow-up required")
    sub.add_parser("ensure")
    sub.add_parser("status")
    sub.add_parser("health")
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

    providers_p = sub.add_parser("providers-check")
    providers_p.add_argument("--config", default="config/providers.example.yaml")
    providers_p.add_argument("--output", default="artifacts/providers_check.json")
    providers_p.add_argument("--timeout-sec", type=int, default=5)
    providers_p.add_argument("--strict", action="store_true")
    providers_p.add_argument("--profile", default="", help="Optional profile file for required/optional overrides.")

    qv_p = sub.add_parser("task-queue-validate")
    qv_p.add_argument("--tasks-file", default="config/tasks.json")

    profile_p = sub.add_parser("profile-apply")
    profile_p.add_argument("name", choices=["local", "lan", "travel"])

    pr_open_p = sub.add_parser("pr-open")
    pr_open_p.add_argument("--title", required=True)
    pr_open_p.add_argument("--body", required=True)
    pr_open_p.add_argument("--base", default="main")
    pr_open_p.add_argument("--head", default="")

    pr_wait_p = sub.add_parser("pr-wait")
    pr_wait_p.add_argument("--pr", default="")
    pr_wait_p.add_argument("--timeout-sec", type=int, default=3600)
    pr_wait_p.add_argument("--interval-sec", type=int, default=30)

    pr_merge_p = sub.add_parser("pr-merge")
    pr_merge_p.add_argument("--pr", default="")
    pr_merge_p.add_argument("--health-report", default="../orxaq/artifacts/health.json")
    pr_merge_p.add_argument("--min-score", type=int, default=85)
    pr_merge_p.add_argument("--strategy", choices=["squash", "merge", "rebase"], default="squash")

    args = parser.parse_args(argv)
    cfg = _config_from_args(args)

    if args.command == "run":
        return run_foreground(cfg)
    if args.command == "supervise":
        return supervise_foreground(cfg)
    if args.command == "start":
        start_background(cfg)
        return 0
    if args.command == "stop":
        stop_background(cfg)
        if args.report or args.file_issue:
            report_path = build_stop_report(
                root=cfg.root_dir,
                health_path=(cfg.root_dir / "artifacts" / "health.json"),
                heartbeat_path=cfg.heartbeat_file,
                state_path=cfg.state_file,
                output_path=(cfg.root_dir / "artifacts" / "AUTONOMY_STOP_REPORT.md"),
            )
            print(f"wrote stop report: {report_path}")
            if args.file_issue:
                issue = file_issue(root=cfg.root_dir, title=args.issue_title, body_path=report_path)
                print(json.dumps(issue, indent=2, sort_keys=True))
                if not issue.get("ok", False):
                    return 1
        return 0
    if args.command == "ensure":
        ensure_background(cfg)
        return 0
    if args.command == "status":
        print(json.dumps(status_snapshot(cfg), indent=2, sort_keys=True))
        logs = tail_logs(cfg)
        if logs:
            print("--- logs ---")
            print(logs)
        return 0
    if args.command == "health":
        print(json.dumps(health_snapshot(cfg), indent=2, sort_keys=True))
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

    if args.command == "providers-check":
        profile = args.profile.strip() or None
        payload = run_providers_check(
            root=str(cfg.root_dir),
            config_path=args.config,
            output_path=args.output,
            timeout_sec=max(1, int(args.timeout_sec)),
            profile_path=profile,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        if args.strict and not bool(payload.get("summary", {}).get("all_required_up", False)):
            return 1
        return 0

    if args.command == "task-queue-validate":
        task_file = (cfg.root_dir / args.tasks_file).resolve()
        errors = validate_task_queue_file(task_file)
        print(json.dumps({"ok": not errors, "errors": errors, "tasks_file": str(task_file)}, indent=2, sort_keys=True))
        return 0 if not errors else 1

    if args.command == "profile-apply":
        destination = profile_apply(root=cfg.root_dir, name=args.name)
        print(f"applied profile: {args.name} -> {destination}")
        return 0

    if args.command == "pr-open":
        payload = pr_open(
            root=cfg.root_dir,
            title=args.title,
            body=args.body,
            base=args.base,
            head=args.head,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok", False) else 1

    if args.command == "pr-wait":
        payload = pr_wait(
            root=cfg.root_dir,
            pr=args.pr or None,
            timeout_sec=max(1, int(args.timeout_sec)),
            interval_sec=max(1, int(args.interval_sec)),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok", False) else 1

    if args.command == "pr-merge":
        payload = pr_merge(
            root=cfg.root_dir,
            pr=args.pr or None,
            health_report_path=(cfg.root_dir / args.health_report).resolve()
            if not Path(args.health_report).is_absolute()
            else Path(args.health_report),
            min_score=max(0, int(args.min_score)),
            strategy=args.strategy,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok", False) else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
