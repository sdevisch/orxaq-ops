"""CLI entrypoint for reusable Orxaq autonomy package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .context import write_default_skill_protocol
from .gitops import (
    GitOpsError,
    detect_head_branch,
    detect_repo,
    merge_pr,
    open_pr,
    read_swarm_health_score,
    wait_for_pr,
)
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
    stop_cmd = sub.add_parser("stop")
    stop_cmd.add_argument("--report", action="store_true", help="Also write AUTONOMY_STOP_REPORT.md.")
    stop_cmd.add_argument("--file-issue", action="store_true", help="Also file a GitHub issue with stop report.")
    stop_cmd.add_argument("--issue-title", default="AUTONOMY STOP: manual follow-up required")
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

    providers_check = sub.add_parser("providers-check")
    providers_check.add_argument("--config", default="config/providers.example.yaml")
    providers_check.add_argument("--output", default="artifacts/providers_check.json")
    providers_check.add_argument("--timeout-sec", type=int, default=5)
    providers_check.add_argument("--strict", action="store_true")
    providers_check.add_argument(
        "--profile",
        default="",
        help="Optional profile file for required/optional provider overrides.",
    )

    task_queue_validate = sub.add_parser("task-queue-validate")
    task_queue_validate.add_argument("--tasks-file", default="config/tasks.json")

    profile_cmd = sub.add_parser("profile-apply")
    profile_cmd.add_argument("name", choices=["local", "lan", "travel"])

    pr_open = sub.add_parser("pr-open")
    pr_open.add_argument("--repo", default="", help="GitHub repo slug owner/name")
    pr_open.add_argument("--base", default="main", help="Base branch")
    pr_open.add_argument("--head", default="", help="Head branch (defaults to current branch)")
    pr_open.add_argument("--title", required=True, help="Pull request title")
    pr_open.add_argument("--body", default="", help="Pull request body")
    pr_open.add_argument("--draft", action="store_true", help="Create pull request as draft")

    pr_wait = sub.add_parser("pr-wait")
    pr_wait.add_argument("--repo", default="", help="GitHub repo slug owner/name")
    pr_wait.add_argument("--pr", type=int, required=True, help="Pull request number")
    pr_wait.add_argument("--interval-sec", type=int, default=30, help="Polling interval in seconds")
    pr_wait.add_argument("--max-attempts", type=int, default=120, help="Maximum polling attempts")
    pr_wait.add_argument(
        "--failure-threshold",
        type=int,
        default=3,
        help="Consecutive failing attempts before stopping and optionally closing PR",
    )
    pr_wait.add_argument(
        "--close-on-failure",
        action="store_true",
        help="Close pull request after repeated failures",
    )
    pr_wait.add_argument(
        "--open-issue-on-failure",
        action="store_true",
        help="Open a GitHub issue after repeated failures",
    )

    pr_merge = sub.add_parser("pr-merge")
    pr_merge.add_argument("--repo", default="", help="GitHub repo slug owner/name")
    pr_merge.add_argument("--pr", type=int, required=True, help="Pull request number")
    pr_merge.add_argument(
        "--method",
        choices=["merge", "squash", "rebase"],
        default="squash",
        help="Merge strategy",
    )
    pr_merge.add_argument("--delete-branch", action="store_true", help="Delete branch after merge")
    pr_merge.add_argument(
        "--swarm-health-json",
        default="",
        help="Path to swarm-health JSON for policy enforcement.",
    )
    pr_merge.add_argument(
        "--swarm-health-score",
        type=float,
        default=-1.0,
        help="Explicit swarm-health score override.",
    )
    pr_merge.add_argument(
        "--min-swarm-health",
        type=float,
        default=85.0,
        help="Minimum required swarm-health score for merge.",
    )
    pr_merge.add_argument(
        "--allow-ci-yellow",
        action="store_true",
        help="Allow merge even when checks are not fully green.",
    )

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
        payload = run_providers_check(
            root=str(cfg.root_dir),
            config_path=args.config,
            output_path=args.output,
            timeout_sec=max(1, int(args.timeout_sec)),
            profile_path=args.profile.strip() or None,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        if args.strict and not bool(payload.get("summary", {}).get("all_required_up", False)):
            return 1
        return 0
    if args.command == "task-queue-validate":
        task_file = (cfg.root_dir / args.tasks_file).resolve()
        errors = validate_task_queue_file(task_file)
        print(
            json.dumps(
                {"ok": not errors, "errors": errors, "tasks_file": str(task_file)},
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if not errors else 1
    if args.command == "profile-apply":
        destination = profile_apply(root=cfg.root_dir, name=args.name)
        print(f"applied profile: {args.name} -> {destination}")
        return 0
    if args.command == "pr-open":
        try:
            repo = args.repo.strip() or detect_repo(cfg.root_dir)
            head = args.head.strip() or detect_head_branch(cfg.root_dir)
            payload = open_pr(
                repo=repo,
                base=args.base.strip(),
                head=head,
                title=args.title,
                body=args.body,
                draft=bool(args.draft),
            )
        except GitOpsError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
            return 1
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "pr-wait":
        try:
            repo = args.repo.strip() or detect_repo(cfg.root_dir)
            payload = wait_for_pr(
                repo=repo,
                pr_number=int(args.pr),
                interval_sec=max(1, int(args.interval_sec)),
                max_attempts=max(1, int(args.max_attempts)),
                failure_threshold=max(1, int(args.failure_threshold)),
                close_on_failure=bool(args.close_on_failure),
                open_issue_on_failure=bool(args.open_issue_on_failure),
            )
        except GitOpsError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
            return 1
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 1
    if args.command == "pr-merge":
        try:
            repo = args.repo.strip() or detect_repo(cfg.root_dir)
            swarm_health_score: float | None
            if args.swarm_health_score >= 0:
                swarm_health_score = float(args.swarm_health_score)
            elif args.swarm_health_json.strip():
                swarm_health_score = read_swarm_health_score(Path(args.swarm_health_json).resolve())
            else:
                swarm_health_score = None
            payload = merge_pr(
                repo=repo,
                pr_number=int(args.pr),
                method=args.method,
                delete_branch=bool(args.delete_branch),
                min_swarm_health=float(args.min_swarm_health),
                swarm_health_score=swarm_health_score,
                require_ci_green=not bool(args.allow_ci_yellow),
            )
        except GitOpsError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
            return 1
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
