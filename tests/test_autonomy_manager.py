import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import manager


class ManagerTests(unittest.TestCase):
    def _build_root(self, tmp: pathlib.Path) -> pathlib.Path:
        (tmp / "config").mkdir(parents=True, exist_ok=True)
        (tmp / "config" / "prompts").mkdir(parents=True, exist_ok=True)
        (tmp / "state").mkdir(parents=True, exist_ok=True)
        (tmp / "artifacts" / "autonomy").mkdir(parents=True, exist_ok=True)
        (tmp / "config" / "tasks.json").write_text("[]\n", encoding="utf-8")
        (tmp / "config" / "objective.md").write_text("objective\n", encoding="utf-8")
        (tmp / "config" / "codex_result.schema.json").write_text("{}\n", encoding="utf-8")
        (tmp / "config" / "skill_protocol.json").write_text("{}\n", encoding="utf-8")
        (tmp / "config" / "prompts" / "codex_impl_prompt.md").write_text("codex prompt\n", encoding="utf-8")
        (tmp / "config" / "prompts" / "gemini_test_prompt.md").write_text("gemini prompt\n", encoding="utf-8")
        impl = tmp / "impl_repo"
        test = tmp / "test_repo"
        impl.mkdir(parents=True, exist_ok=True)
        test.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(impl), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(test), "init"], check=True, capture_output=True)
        (tmp / ".env.autonomy").write_text(
            f"OPENAI_API_KEY=test\nGEMINI_API_KEY=test\nORXAQ_IMPL_REPO={impl}\nORXAQ_TEST_REPO={test}\n",
            encoding="utf-8",
        )
        return tmp

    def test_manager_config_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            self.assertEqual(cfg.root_dir, root.resolve())
            self.assertTrue(str(cfg.skill_protocol_file).endswith("skill_protocol.json"))

    def test_load_env_file_supports_export_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            env = pathlib.Path(td) / ".env"
            env.write_text("export GEMINI_API_KEY=test\nOPENAI_API_KEY=abc\n", encoding="utf-8")
            parsed = manager._load_env_file(env)
            self.assertEqual(parsed["GEMINI_API_KEY"], "test")
            self.assertEqual(parsed["OPENAI_API_KEY"], "abc")

    def test_runtime_diagnostics_reports_missing_cli(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            env_file.write_text(
                "OPENAI_API_KEY=test\nGEMINI_API_KEY=test\nORXAQ_AUTONOMY_CODEX_CMD=missing_codex_cmd\nORXAQ_AUTONOMY_GEMINI_CMD=missing_gemini_cmd\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            diagnostics = manager.runtime_diagnostics(cfg)
            self.assertFalse(diagnostics["ok"])
            self.assertGreaterEqual(len(diagnostics["errors"]), 2)
            joined = " ".join(diagnostics["errors"])
            self.assertIn("Codex CLI not found", joined)
            self.assertIn("Gemini CLI not found", joined)

    def test_runner_argv_contains_skill_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            argv = manager.runner_argv(cfg)
            self.assertIn("--skill-protocol-file", argv)
            self.assertIn("--validate-command", argv)
            self.assertIn("--codex-startup-prompt-file", argv)
            self.assertIn("--gemini-startup-prompt-file", argv)
            self.assertIn("--claude-startup-prompt-file", argv)
            self.assertIn("--codex-cmd", argv)
            self.assertIn("--gemini-cmd", argv)
            self.assertIn("--claude-cmd", argv)
            self.assertIn("--conversation-log-file", argv)

    def test_ensure_background_starts_if_supervisor_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager._read_pid", return_value=None), mock.patch(
                "orxaq_autonomy.manager._pid_running", return_value=False
            ), mock.patch("orxaq_autonomy.manager.start_background") as start:
                manager.ensure_background(cfg)
                start.assert_called_once_with(cfg)

    def test_status_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.heartbeat_file.write_text(json.dumps({"timestamp": "2000-01-01T00:00:00+00:00"}), encoding="utf-8")
            snap = manager.status_snapshot(cfg)
            self.assertIn("heartbeat_age_sec", snap)
            self.assertIn("supervisor_running", snap)

    def test_install_keepalive_windows_command_is_user_space(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager.os.name", "nt"), mock.patch(
                "orxaq_autonomy.manager.subprocess.run"
            ) as run:
                run.return_value = subprocess_result = mock.Mock(returncode=0, stdout="", stderr="")
                label = manager.install_keepalive(cfg)
                self.assertEqual(label, "OrxaqAutonomyEnsure")
                cmd = run.call_args[0][0]
                self.assertIn("schtasks", cmd[0].lower())
                self.assertNotIn("/RL", " ".join(cmd))
                self.assertEqual(subprocess_result.returncode, 0)
                tr_index = cmd.index("/TR") + 1
                task_command = cmd[tr_index]
                self.assertIn("orxaq_autonomy.cli --root", task_command)
                self.assertIn(" ensure", task_command)

    def test_start_background_places_root_before_subcommand(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager._read_pid", return_value=None), mock.patch(
                "orxaq_autonomy.manager._pid_running", return_value=False
            ), mock.patch("orxaq_autonomy.manager.ensure_runtime"), mock.patch(
                "orxaq_autonomy.manager.subprocess.Popen"
            ) as popen:
                popen.return_value = mock.Mock(pid=1234)
                manager.start_background(cfg)
                argv = popen.call_args[0][0]
                self.assertIn("--root", argv)
                self.assertLess(argv.index("--root"), argv.index("supervise"))

    def test_ensure_background_restarts_stale_runner(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager._read_pid",
                side_effect=[123, 456],
            ), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=[True, True],
            ), mock.patch(
                "orxaq_autonomy.manager._heartbeat_age_sec",
                return_value=9999,
            ), mock.patch(
                "orxaq_autonomy.manager._terminate_pid"
            ) as terminate:
                manager.ensure_background(cfg)
                terminate.assert_called_once_with(456)

    def test_preflight_detects_dirty_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.runtime_diagnostics",
                return_value={"ok": True, "checks": [], "errors": [], "recommendations": []},
            ), mock.patch(
                "orxaq_autonomy.manager._repo_is_clean",
                side_effect=[(False, "dirty"), (True, "ok")],
            ):
                payload = manager.preflight(cfg, require_clean=True)
            self.assertFalse(payload["clean"])
            self.assertEqual(len(payload["checks"]), 2)
            self.assertEqual(payload["runtime"], "ok")

    def test_preflight_allow_dirty_still_requires_repositories(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            env_file.write_text(
                "OPENAI_API_KEY=test\nGEMINI_API_KEY=test\nORXAQ_IMPL_REPO=/tmp/does-not-exist\nORXAQ_TEST_REPO=/tmp/also-missing\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.runtime_diagnostics",
                return_value={"ok": True, "checks": [], "errors": [], "recommendations": []},
            ):
                payload = manager.preflight(cfg, require_clean=False)
            self.assertFalse(payload["clean"])
            self.assertEqual(len(payload["checks"]), 2)
            self.assertTrue(payload["checks"][0]["message"].startswith("missing repository"))

    def test_preflight_reports_runtime_failures_without_exception(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.runtime_diagnostics",
                return_value={
                    "ok": False,
                    "checks": [{"name": "codex_cli", "ok": False, "message": "missing"}],
                    "errors": ["Codex CLI not found"],
                    "recommendations": ["Install codex CLI"],
                },
            ):
                payload = manager.preflight(cfg, require_clean=False)
            self.assertEqual(payload["runtime"], "error")
            self.assertFalse(payload["clean"])
            self.assertIn("Codex CLI not found", payload["runtime_errors"])
            self.assertIn("Install codex CLI", payload["runtime_recommendations"])

    def test_install_keepalive_macos_uses_launch_agent(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            fake_home = pathlib.Path(td) / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            with mock.patch("orxaq_autonomy.manager.os.name", "posix"), mock.patch(
                "orxaq_autonomy.manager.sys.platform", "darwin"
            ), mock.patch("orxaq_autonomy.manager.Path.home", return_value=fake_home), mock.patch(
                "orxaq_autonomy.manager.subprocess.run"
            ) as run:
                run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
                label = manager.install_keepalive(cfg)
                self.assertEqual(label, "com.orxaq.autonomy.ensure")
                plist = fake_home / "Library" / "LaunchAgents" / "com.orxaq.autonomy.ensure.plist"
                self.assertTrue(plist.exists())

    def test_install_keepalive_unsupported_platform_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager.os.name", "posix"), mock.patch(
                "orxaq_autonomy.manager.sys.platform", "linux"
            ):
                with self.assertRaises(RuntimeError):
                    manager.install_keepalive(cfg)

    def test_health_snapshot_writes_health_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "task-a": {"status": "done"},
                        "task-b": {"status": "blocked"},
                        "task-c": {"status": "pending"},
                    }
                ),
                encoding="utf-8",
            )
            cfg.heartbeat_file.write_text(json.dumps({"timestamp": "2000-01-01T00:00:00+00:00"}), encoding="utf-8")
            snapshot = manager.health_snapshot(cfg)
            self.assertIn("health_file", snapshot)
            self.assertEqual(snapshot["state_counts"]["blocked"], 1)
            self.assertTrue(pathlib.Path(snapshot["health_file"]).exists())

    def test_monitor_snapshot_writes_monitor_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "task-a": {"status": "done"},
                        "task-b": {"status": "in_progress"},
                    }
                ),
                encoding="utf-8",
            )
            cfg.heartbeat_file.write_text(json.dumps({"timestamp": "2000-01-01T00:00:00+00:00"}), encoding="utf-8")
            snapshot = manager.monitor_snapshot(cfg)
            self.assertIn("monitor_file", snapshot)
            self.assertTrue(pathlib.Path(snapshot["monitor_file"]).exists())
            self.assertIn("repos", snapshot)

    def test_dashboard_status_snapshot_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            snapshot = manager.dashboard_status_snapshot(cfg)
            self.assertFalse(snapshot["running"])
            self.assertEqual(snapshot["pid"], None)

    def test_start_dashboard_background_writes_pid_and_meta(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager.subprocess.Popen") as popen, mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=lambda pid: bool(pid == 4321),
            ), mock.patch("orxaq_autonomy.manager.time.sleep", return_value=None):
                popen.return_value = mock.Mock(pid=4321)
                snapshot = manager.start_dashboard_background(
                    cfg,
                    host="127.0.0.1",
                    port=8765,
                    refresh_sec=3,
                    open_browser=False,
                )
            self.assertTrue(snapshot["running"])
            self.assertEqual(snapshot["pid"], 4321)
            self.assertTrue(cfg.dashboard_meta_file.exists())

    def test_stop_dashboard_background_terminates_pid(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.dashboard_pid_file.write_text("777\n", encoding="utf-8")
            with mock.patch("orxaq_autonomy.manager._terminate_pid") as terminate:
                snapshot = manager.stop_dashboard_background(cfg)
            terminate.assert_called_once_with(777)
            self.assertFalse(snapshot["running"])

    def test_render_monitor_text_contains_key_fields(self):
        text = manager.render_monitor_text(
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "status": {"supervisor_running": True, "runner_running": False, "heartbeat_age_sec": 5},
                "progress": {
                    "counts": {"done": 1, "in_progress": 2, "pending": 3, "blocked": 0, "unknown": 0},
                    "active_tasks": ["a", "b"],
                },
                "repos": {
                    "implementation": {
                        "ok": True,
                        "branch": "main",
                        "head": "abc123",
                        "dirty": False,
                        "changed_files": 0,
                    },
                    "tests": {"ok": False, "error": "missing repo"},
                },
                "latest_log_line": "running task",
                "monitor_file": "/tmp/monitor.json",
            }
        )
        self.assertIn("supervisor=True", text)
        self.assertIn("done=1", text)
        self.assertIn("impl_repo", text)
        self.assertIn("test_repo", text)

    def test_supervise_foreground_restarts_after_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)

            class _Child:
                def __init__(self, pid: int, rc: int) -> None:
                    self.pid = pid
                    self._rc = rc

                def wait(self) -> int:
                    return self._rc

            popen_calls: list[int] = []

            def fake_popen(*args, **kwargs):
                popen_calls.append(1)
                argv = args[0]
                self.assertIn("--root", argv)
                self.assertLess(argv.index("--root"), argv.index("run"))
                return _Child(pid=100 + len(popen_calls), rc=1 if len(popen_calls) == 1 else 0)

            with mock.patch("orxaq_autonomy.manager.ensure_runtime"), mock.patch(
                "orxaq_autonomy.manager.subprocess.Popen",
                side_effect=fake_popen,
            ), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                return_value=False,
            ), mock.patch(
                "orxaq_autonomy.manager.time.sleep",
                return_value=None,
            ):
                rc = manager.supervise_foreground(cfg)
            self.assertEqual(rc, 0)
            self.assertEqual(len(popen_calls), 2)

    def test_tail_logs_latest_run_only_filters_historical_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.log_file.write_text(
                "\n".join(
                    [
                        "Traceback (most recent call last):",
                        "old failure",
                        "[2026-02-08T20:30:18.086450+00:00] supervisor: launching runner",
                        "[2026-02-08T20:30:18.176992+00:00] Starting autonomy runner",
                        "[2026-02-08T20:30:18.177239+00:00] Running Gemini task",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            logs = manager.tail_logs(cfg, lines=40, latest_run_only=True)
            self.assertIn("supervisor: launching runner", logs)
            self.assertNotIn("old failure", logs)

    def test_bootstrap_background_starts_and_writes_startup_packet(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.preflight",
                return_value={"clean": True, "runtime": "ok", "checks": []},
            ), mock.patch(
                "orxaq_autonomy.manager.start_background"
            ), mock.patch(
                "orxaq_autonomy.manager.install_keepalive",
                return_value="keepalive-label",
            ), mock.patch(
                "orxaq_autonomy.manager.open_in_ide",
                return_value="opened",
            ):
                payload = manager.bootstrap_background(
                    cfg,
                    allow_dirty=True,
                    install_keepalive_job=True,
                    ide="vscode",
                )
            self.assertTrue(payload["ok"])
            self.assertTrue(pathlib.Path(payload["workspace"]).exists())
            self.assertTrue(pathlib.Path(payload["startup_packet"]).exists())
            self.assertFalse(payload["workspace_reused"])
            startup_packet_text = pathlib.Path(payload["startup_packet"]).read_text(encoding="utf-8")
            self.assertIn("codex prompt", startup_packet_text)
            self.assertIn("gemini prompt", startup_packet_text)
            self.assertTrue(payload["keepalive"]["active"])

    def test_bootstrap_background_fails_when_clean_required_and_dirty(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.preflight",
                return_value={"clean": False, "runtime": "error", "checks": []},
            ):
                payload = manager.bootstrap_background(
                    cfg,
                    allow_dirty=False,
                    install_keepalive_job=False,
                    ide=None,
                )
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["reason"], "preflight_failed")
            self.assertTrue(pathlib.Path(payload["startup_packet"]).exists())

    def test_bootstrap_background_reuses_existing_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            existing_workspace = root / "orxaq-dual-agent.code-workspace"
            existing_workspace.write_text("existing\n", encoding="utf-8")
            with mock.patch(
                "orxaq_autonomy.manager.preflight",
                return_value={"clean": True, "runtime": "ok", "checks": []},
            ), mock.patch(
                "orxaq_autonomy.manager.start_background"
            ), mock.patch(
                "orxaq_autonomy.manager.install_keepalive",
                return_value="keepalive-label",
            ), mock.patch(
                "orxaq_autonomy.manager.open_in_ide",
                return_value="opened",
            ), mock.patch(
                "orxaq_autonomy.manager.generate_workspace"
            ) as generate_workspace:
                payload = manager.bootstrap_background(
                    cfg,
                    allow_dirty=True,
                    install_keepalive_job=True,
                    ide="vscode",
                )
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["workspace_reused"])
            generate_workspace.assert_not_called()

    def test_bootstrap_background_start_failure_is_structured(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.preflight",
                return_value={"clean": True, "runtime": "ok", "checks": []},
            ), mock.patch(
                "orxaq_autonomy.manager.start_background",
                side_effect=RuntimeError("codex CLI not found"),
            ):
                payload = manager.bootstrap_background(
                    cfg,
                    allow_dirty=True,
                    install_keepalive_job=False,
                    ide=None,
                )
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["reason"], "start_failed")
            self.assertIn("codex CLI not found", payload["error"])

    def test_conversations_snapshot_reads_main_and_lane_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                                "conversation_log_file": "artifacts/autonomy/lanes/lane-a/conversations.ndjson",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            cfg.conversation_log_file.write_text(
                json.dumps({"timestamp": "2026-01-01T00:00:00+00:00", "owner": "codex", "content": "main"}) + "\n",
                encoding="utf-8",
            )
            lane_conv = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "conversations.ndjson"
            lane_conv.parent.mkdir(parents=True, exist_ok=True)
            lane_conv.write_text(
                json.dumps({"timestamp": "2026-01-01T00:00:01+00:00", "owner": "gemini", "content": "lane"}) + "\n",
                encoding="utf-8",
            )
            snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=True)
            self.assertEqual(snapshot["total_events"], 2)
            self.assertIn("codex", snapshot["owner_counts"])
            self.assertIn("gemini", snapshot["owner_counts"])

    def test_start_lanes_background_starts_selected_lane(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager._resolve_binary", return_value="/usr/bin/codex"), mock.patch(
                "orxaq_autonomy.manager._repo_basic_check",
                return_value=(True, "ok"),
            ), mock.patch(
                "orxaq_autonomy.manager.subprocess.Popen"
            ) as popen, mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=lambda pid: bool(pid == 888),
            ), mock.patch("orxaq_autonomy.manager.time.sleep", return_value=None):
                popen.return_value = mock.Mock(pid=888)
                payload = manager.start_lanes_background(cfg, lane_id="lane-a")
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["started"][0]["id"], "lane-a")
            argv = popen.call_args[0][0]
            self.assertIn("--owner-filter", argv)
            self.assertIn("codex", argv)


if __name__ == "__main__":
    unittest.main()
