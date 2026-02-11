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
    autonomy_stop,
    ensure_background,
    health_snapshot,
    install_keepalive,
    keepalive_status,
    preflight,
    reset_state,
    run_foreground,
    start_background,
    status_snapshot,
    supervise_foreground,
    tail_logs,
    uninstall_keepalive,
)
from .router import apply_router_profile, run_router_check


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
    stop = sub.add_parser("stop")
    stop.add_argument(
        "--reason",
        default="manual stop requested",
        help="Reason included in AUTONOMY_STOP_REPORT.md and issue payload.",
    )
    stop.add_argument(
        "--file-issue",
        action="store_true",
        help="Create a GitHub issue after writing the stop report.",
    )
    stop.add_argument(
        "--issue-repo",
        default="",
        help="Optional owner/repo override for issue filing (default: current repo).",
    )
    stop.add_argument(
        "--issue-label",
        action="append",
        default=[],
        help="Issue label(s) to include when --file-issue is enabled.",
    )
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
    router = sub.add_parser("router-check")
    router.add_argument("--config", default="./config/router.example.yaml")
    router.add_argument("--output", default="./artifacts/router_check.json")
    router.add_argument("--profile", default="")
    router.add_argument("--profiles-dir", default="./profiles")
    router.add_argument("--active-config", default="./config/router.active.yaml")
    router.add_argument("--lane", default="")
    router.add_argument("--timeout-sec", type=int, default=5)
    router.add_argument("--strict", action="store_true")
    profile_apply = sub.add_parser("profile-apply")
    profile_apply.add_argument("name")
    profile_apply.add_argument("--config", default="./config/router.example.yaml")
    profile_apply.add_argument("--profiles-dir", default="./profiles")
    profile_apply.add_argument("--output", default="./config/router.active.yaml")

    init_skill = sub.add_parser("init-skill-protocol")
    init_skill.add_argument("--output", default="config/skill_protocol.json")

    workspace = sub.add_parser("workspace")
    workspace.add_argument("--output", default="orxaq-dual-agent.code-workspace")

    open_ide = sub.add_parser("open-ide")
    open_ide.add_argument("--ide", choices=["vscode", "cursor", "pycharm"], default="vscode")
    open_ide.add_argument("--workspace", default="orxaq-dual-agent.code-workspace")

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
        payload = autonomy_stop(
            cfg,
            reason=args.reason,
            file_issue=bool(args.file_issue),
            issue_repo=args.issue_repo,
            labels=list(args.issue_label or []),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
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
    if args.command == "router-check":
        report = run_router_check(
            root=str(cfg.root_dir),
            config_path=args.config,
            output_path=args.output,
            profile=args.profile,
            profiles_dir=args.profiles_dir,
            active_config_output=args.active_config,
            lane=args.lane,
            timeout_sec=max(1, int(args.timeout_sec)),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.strict and not bool(report.get("summary", {}).get("overall_ok", False)):
            return 1
        return 0
    if args.command == "profile-apply":
        try:
            payload = apply_router_profile(
                root=str(cfg.root_dir),
                profile_name=args.name,
                base_config_path=args.config,
                profiles_dir=args.profiles_dir,
                output_path=args.output,
            )
        except Exception as err:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(err)}, sort_keys=True))
            return 1
        print(json.dumps(payload, indent=2, sort_keys=True))
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
