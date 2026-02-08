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
        (tmp / "config" / "pricing.json").write_text('{"version":1,"currency":"USD","models":{}}\n', encoding="utf-8")
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
            self.assertIn("--metrics-file", argv)
            self.assertIn("--metrics-summary-file", argv)
            self.assertIn("--pricing-file", argv)
            self.assertIn("--gemini-fallback-model", argv)
            self.assertIn("--auto-push-guard", argv)
            self.assertIn("--auto-push-interval-sec", argv)

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
            self.assertIn("diagnostics", snapshot)
            self.assertIn("handoffs", snapshot)
            self.assertIn("runtime", snapshot)
            self.assertIn("effective_agents_running", snapshot["runtime"])
            self.assertIn("source", snapshot["progress"])

    def test_monitor_snapshot_reports_handoff_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            handoff_dir = root / "artifacts" / "autonomy" / "handoffs"
            handoff_dir.mkdir(parents=True, exist_ok=True)
            (handoff_dir / "to_codex.ndjson").write_text(
                "\n".join(
                    [
                        json.dumps({"timestamp": "2026-01-01T00:00:00+00:00", "task_id": "a"}),
                        json.dumps({"timestamp": "2026-01-01T00:00:01+00:00", "task_id": "b"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (handoff_dir / "to_gemini.ndjson").write_text(
                json.dumps({"timestamp": "2026-01-01T00:00:02+00:00", "task_id": "c"}) + "\n",
                encoding="utf-8",
            )
            snapshot = manager.monitor_snapshot(cfg)
            self.assertEqual(snapshot["handoffs"]["to_codex_events"], 2)
            self.assertEqual(snapshot["handoffs"]["to_gemini_events"], 1)
            self.assertEqual(snapshot["handoffs"]["latest_to_codex"]["task_id"], "b")
            self.assertEqual(snapshot["handoffs"]["latest_to_gemini"]["task_id"], "c")

    def test_monitor_snapshot_reports_response_metrics_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.metrics_summary_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.metrics_summary_file.write_text(
                json.dumps(
                    {
                        "responses_total": 4,
                        "quality_score_sum": 3.0,
                        "latency_sec_sum": 20.0,
                        "prompt_difficulty_score_sum": 120.0,
                        "tokens_total": 5000,
                        "tokens_input_total": 3500,
                        "tokens_output_total": 1500,
                        "token_exact_count": 3,
                        "first_time_pass_count": 3,
                        "acceptance_pass_count": 3,
                        "exact_cost_count": 2,
                        "cost_usd_total": 1.25,
                        "by_owner": {
                            "codex": {
                                "responses": 4,
                                "first_time_pass": 3,
                                "validation_passed": 3,
                                "cost_usd_total": 1.25,
                                "tokens_total": 5000,
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager.monitor_snapshot(cfg)
            metrics = snapshot["response_metrics"]
            self.assertEqual(metrics["responses_total"], 4)
            self.assertAlmostEqual(metrics["quality_score_avg"], 0.75, places=6)
            self.assertAlmostEqual(metrics["latency_sec_avg"], 5.0, places=6)
            self.assertAlmostEqual(metrics["prompt_difficulty_score_avg"], 30.0, places=6)
            self.assertAlmostEqual(metrics["cost_usd_total"], 1.25, places=8)
            self.assertEqual(metrics["tokens_total"], 5000)
            self.assertAlmostEqual(metrics["token_rate_per_minute"], 15000.0, places=6)
            self.assertEqual(metrics["exciting_stat"]["label"], "Token Flow")
            self.assertIn("codex", metrics["by_owner"])

    def test_monitor_snapshot_retains_output_when_lane_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                side_effect=RuntimeError("lane source unavailable"),
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 0,
                    "events": [],
                    "owner_counts": {},
                    "partial": False,
                    "ok": True,
                    "errors": [],
                    "sources": [],
                },
            ), mock.patch(
                "orxaq_autonomy.manager._repo_monitor_snapshot",
                return_value={
                    "ok": True,
                    "error": "",
                    "path": "/tmp/repo",
                    "branch": "main",
                    "head": "abc123",
                    "upstream": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "sync_state": "synced",
                    "dirty": False,
                    "changed_files": 0,
                },
            ), mock.patch("orxaq_autonomy.manager.tail_logs", return_value=""):
                snapshot = manager.monitor_snapshot(cfg)
            self.assertFalse(snapshot["diagnostics"]["ok"])
            self.assertFalse(snapshot["diagnostics"]["sources"]["lanes"]["ok"])
            self.assertFalse(snapshot["lanes"]["ok"])
            self.assertIn("lane source unavailable", snapshot["lanes"]["errors"][0])

    def test_monitor_snapshot_retains_output_when_response_metrics_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "ok": True,
                    "errors": [],
                    "running_count": 1,
                    "total_count": 1,
                    "lanes": [
                        {
                            "id": "lane-a",
                            "owner": "codex",
                            "running": True,
                            "health": "ok",
                            "state_counts": {"pending": 0, "in_progress": 1, "done": 0, "blocked": 0, "unknown": 0},
                            "task_total": 1,
                        }
                    ],
                    "health_counts": {"ok": 1},
                    "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                },
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 0,
                    "events": [],
                    "owner_counts": {},
                    "partial": False,
                    "ok": True,
                    "errors": [],
                    "sources": [],
                },
            ), mock.patch(
                "orxaq_autonomy.manager._response_metrics_snapshot",
                side_effect=RuntimeError("metrics source unavailable"),
            ), mock.patch(
                "orxaq_autonomy.manager._repo_monitor_snapshot",
                return_value={
                    "ok": True,
                    "error": "",
                    "path": "/tmp/repo",
                    "branch": "main",
                    "head": "abc123",
                    "upstream": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "sync_state": "synced",
                    "dirty": False,
                    "changed_files": 0,
                },
            ), mock.patch("orxaq_autonomy.manager.tail_logs", return_value=""):
                snapshot = manager.monitor_snapshot(cfg)
            self.assertFalse(snapshot["diagnostics"]["sources"]["response_metrics"]["ok"])
            self.assertFalse(snapshot["response_metrics"]["ok"])
            self.assertIn("metrics source unavailable", snapshot["response_metrics"]["errors"][0])
            self.assertEqual(snapshot["runtime"]["lane_owner_health"]["codex"]["running"], 1)

    def test_monitor_snapshot_retains_output_when_handoff_source_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"ok": True, "errors": [], "running_count": 0, "total_count": 0, "lanes": []},
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 0,
                    "events": [],
                    "owner_counts": {},
                    "partial": False,
                    "ok": True,
                    "errors": [],
                    "sources": [],
                },
            ), mock.patch(
                "orxaq_autonomy.manager._repo_monitor_snapshot",
                return_value={
                    "ok": True,
                    "error": "",
                    "path": "/tmp/repo",
                    "branch": "main",
                    "head": "abc123",
                    "upstream": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "sync_state": "synced",
                    "dirty": False,
                    "changed_files": 0,
                },
            ), mock.patch("orxaq_autonomy.manager.tail_logs", return_value=""), mock.patch(
                "orxaq_autonomy.manager._tail_ndjson",
                side_effect=OSError("handoff read denied"),
            ):
                snapshot = manager.monitor_snapshot(cfg)
            self.assertFalse(snapshot["diagnostics"]["sources"]["handoffs"]["ok"])
            self.assertEqual(snapshot["handoffs"]["to_codex_events"], 0)
            self.assertEqual(snapshot["handoffs"]["to_gemini_events"], 0)

    def test_monitor_snapshot_merges_primary_and_lane_progress_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.state_file.write_text(
                json.dumps(
                    {
                        "task-a": {"status": "done"},
                        "task-b": {"status": "done"},
                    }
                ),
                encoding="utf-8",
            )
            lane_payload = {
                "ok": True,
                "errors": [],
                "running_count": 1,
                "total_count": 1,
                "lanes": [
                    {
                        "id": "lane-a",
                        "owner": "codex",
                        "running": True,
                        "health": "ok",
                        "state_counts": {"pending": 1, "in_progress": 1, "done": 0, "blocked": 0, "unknown": 0},
                        "task_total": 2,
                    }
                ],
            }
            with mock.patch("orxaq_autonomy.manager.lane_status_snapshot", return_value=lane_payload):
                snapshot = manager.monitor_snapshot(cfg)
            self.assertEqual(snapshot["progress"]["source"], "merged_states")
            self.assertEqual(snapshot["progress"]["counts"]["pending"], 1)
            self.assertEqual(snapshot["progress"]["counts"]["in_progress"], 1)
            self.assertEqual(snapshot["progress"]["counts"]["done"], 2)
            self.assertEqual(snapshot["progress"]["active_tasks"], ["lane:lane-a"])

    def test_monitor_snapshot_includes_recent_conversation_events(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"ok": True, "errors": [], "running_count": 0, "total_count": 0, "lanes": []},
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 3,
                    "events": [
                        {"timestamp": "2026-01-01T00:00:00+00:00", "owner": "codex", "content": "a"},
                        {"timestamp": "2026-01-01T00:00:01+00:00", "owner": "gemini", "content": "b"},
                        {"timestamp": "2026-01-01T00:00:02+00:00", "owner": "claude", "content": "c"},
                    ],
                    "owner_counts": {"codex": 1, "gemini": 1, "claude": 1},
                    "partial": False,
                    "ok": True,
                    "errors": [],
                    "sources": [],
                },
            ), mock.patch(
                "orxaq_autonomy.manager._repo_monitor_snapshot",
                return_value={
                    "ok": True,
                    "error": "",
                    "path": "/tmp/repo",
                    "branch": "main",
                    "head": "abc123",
                    "upstream": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "sync_state": "synced",
                    "dirty": False,
                    "changed_files": 0,
                },
            ), mock.patch("orxaq_autonomy.manager.tail_logs", return_value=""):
                snapshot = manager.monitor_snapshot(cfg)
            self.assertEqual(len(snapshot["conversations"]["recent_events"]), 3)
            self.assertEqual(snapshot["conversations"]["latest"]["content"], "c")

    def test_monitor_snapshot_counts_idle_lane_as_operational(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "ok": True,
                    "errors": [],
                    "running_count": 0,
                    "total_count": 1,
                    "lanes": [
                        {
                            "id": "lane-a",
                            "owner": "codex",
                            "running": False,
                            "health": "idle",
                            "state_counts": {"pending": 0, "in_progress": 0, "done": 1, "blocked": 0, "unknown": 0},
                            "task_total": 1,
                        }
                    ],
                    "health_counts": {"idle": 1},
                    "owner_counts": {"codex": {"total": 1, "running": 0, "healthy": 1, "degraded": 0}},
                },
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 0,
                    "events": [],
                    "owner_counts": {},
                    "partial": False,
                    "ok": True,
                    "errors": [],
                    "sources": [],
                },
            ), mock.patch(
                "orxaq_autonomy.manager._repo_monitor_snapshot",
                return_value={
                    "ok": True,
                    "error": "",
                    "path": "/tmp/repo",
                    "branch": "main",
                    "head": "abc123",
                    "upstream": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "sync_state": "synced",
                    "dirty": False,
                    "changed_files": 0,
                },
            ), mock.patch("orxaq_autonomy.manager.tail_logs", return_value=""):
                snapshot = manager.monitor_snapshot(cfg)
            self.assertEqual(snapshot["runtime"]["lane_operational_count"], 1)
            self.assertEqual(snapshot["runtime"]["lane_degraded_count"], 0)

    def test_dashboard_status_snapshot_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            snapshot = manager.dashboard_status_snapshot(cfg)
            self.assertFalse(snapshot["running"])
            self.assertEqual(snapshot["pid"], None)
            self.assertIn("build_current", snapshot)

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
            meta = json.loads(cfg.dashboard_meta_file.read_text(encoding="utf-8"))
            self.assertIn("build_id", meta)

    def test_stop_dashboard_background_terminates_pid(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.dashboard_pid_file.write_text("777\n", encoding="utf-8")
            with mock.patch("orxaq_autonomy.manager._terminate_pid") as terminate:
                snapshot = manager.stop_dashboard_background(cfg)
            terminate.assert_called_once_with(777)
            self.assertFalse(snapshot["running"])

    def test_ensure_dashboard_background_restarts_when_build_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            stale_snapshot = {
                "running": True,
                "build_current": False,
                "pid": 222,
                "url": "http://127.0.0.1:8765/",
            }
            fresh_snapshot = {"running": True, "pid": 333, "url": "http://127.0.0.1:8765/", "build_current": True}
            with mock.patch(
                "orxaq_autonomy.manager.dashboard_status_snapshot",
                return_value=stale_snapshot,
            ), mock.patch(
                "orxaq_autonomy.manager.stop_dashboard_background",
                return_value={"running": False},
            ) as stop_dashboard, mock.patch(
                "orxaq_autonomy.manager.start_dashboard_background",
                return_value=fresh_snapshot,
            ) as start_dashboard:
                snapshot = manager.ensure_dashboard_background(cfg, open_browser=False)
            stop_dashboard.assert_called_once()
            start_dashboard.assert_called_once()
            self.assertTrue(snapshot["running"])

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
                        "upstream": "origin/main",
                        "ahead": 0,
                        "behind": 0,
                        "sync_state": "synced",
                        "dirty": False,
                        "changed_files": 0,
                    },
                    "tests": {"ok": False, "error": "missing repo"},
                },
                "lanes": {
                    "running_count": 1,
                    "total_count": 1,
                    "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                },
                "handoffs": {"to_codex_events": 3, "to_gemini_events": 1},
                "latest_log_line": "running task",
                "monitor_file": "/tmp/monitor.json",
            }
        )
        self.assertIn("supervisor=True", text)
        self.assertIn("done=1", text)
        self.assertIn("impl_repo", text)
        self.assertIn("test_repo", text)
        self.assertIn("sync=synced", text)
        self.assertIn("handoffs: to_codex=3 to_gemini=1", text)
        self.assertIn("lane_owners: codex(total=1,running=1,healthy=1,degraded=0)", text)
        self.assertIn("exciting_stat:", text)

    def test_repo_monitor_snapshot_flags_behind_branch(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            with mock.patch("orxaq_autonomy.manager._repo_basic_check", return_value=(True, "ok")), mock.patch(
                "orxaq_autonomy.manager._git_command",
                side_effect=[
                    (True, "main"),
                    (True, "abc123"),
                    (True, ""),
                    (True, "origin/main"),
                    (True, "0 2"),
                ],
            ):
                payload = manager._repo_monitor_snapshot(repo)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["sync_state"], "behind")
        self.assertEqual(payload["ahead"], 0)
        self.assertEqual(payload["behind"], 2)
        self.assertIn("sync_behind", payload["error"])

    def test_repo_monitor_snapshot_allows_ahead_branch(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            with mock.patch("orxaq_autonomy.manager._repo_basic_check", return_value=(True, "ok")), mock.patch(
                "orxaq_autonomy.manager._git_command",
                side_effect=[
                    (True, "main"),
                    (True, "abc123"),
                    (True, ""),
                    (True, "origin/main"),
                    (True, "2 0"),
                ],
            ):
                payload = manager._repo_monitor_snapshot(repo)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["sync_state"], "ahead")
        self.assertEqual(payload["ahead"], 2)
        self.assertEqual(payload["behind"], 0)

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
            self.assertTrue(snapshot["ok"])
            self.assertFalse(snapshot["partial"])

    def test_conversations_snapshot_degrades_if_lane_specs_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.conversation_log_file.write_text(
                json.dumps({"timestamp": "2026-01-01T00:00:00+00:00", "owner": "codex", "content": "main"}) + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "orxaq_autonomy.manager._load_lane_specs_resilient",
                return_value=([], ["bad lanes config"]),
            ):
                snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=True)
            self.assertEqual(snapshot["total_events"], 1)
            self.assertFalse(snapshot["ok"])
            self.assertTrue(snapshot["partial"])
            self.assertIn("lane_specs: bad lanes config", snapshot["errors"][0])

    def test_conversations_snapshot_keeps_valid_sources_with_invalid_lane_config(self):
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
                            },
                            {
                                "id": "lane-b",
                                "owner": "bad-owner",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
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
            self.assertFalse(snapshot["ok"])
            self.assertTrue(snapshot["partial"])
            self.assertIn("lane_specs: lane-b", snapshot["errors"][0])

    def test_conversations_snapshot_falls_back_to_lane_events_when_conversation_missing(self):
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
            lane_events = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "events.ndjson"
            lane_events.parent.mkdir(parents=True, exist_ok=True)
            lane_events.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01+00:00",
                        "lane_id": "lane-a",
                        "event_type": "task_done",
                        "payload": {"task_id": "t1", "status": "done"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=True)
            self.assertEqual(snapshot["total_events"], 2)
            lane_events_seen = [item for item in snapshot["events"] if item.get("source_kind") == "lane_events"]
            self.assertEqual(len(lane_events_seen), 1)
            self.assertIn("task_done", lane_events_seen[0].get("content", ""))

    def test_conversations_snapshot_treats_missing_lane_sources_as_recoverable(self):
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

            snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=True)
            self.assertTrue(snapshot["ok"])
            self.assertFalse(snapshot["partial"])
            self.assertEqual(snapshot["total_events"], 1)
            lane_source = next(item for item in snapshot["sources"] if item.get("lane_id") == "lane-a")
            self.assertTrue(lane_source["missing"])
            self.assertTrue(lane_source["ok"])
            self.assertTrue(lane_source["recoverable_missing"])
            self.assertEqual(lane_source["event_count"], 0)

    def test_conversations_snapshot_normalizes_missing_lane_owner_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "owner": "gemini",
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
            lane_conv = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "conversations.ndjson"
            lane_conv.parent.mkdir(parents=True, exist_ok=True)
            lane_conv.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01+00:00",
                        "owner": "",
                        "lane_id": "",
                        "event_type": "agent_output",
                        "content": "lane update",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=True)
            lane_events = [item for item in snapshot["events"] if item.get("source_kind") == "lane"]
            self.assertEqual(len(lane_events), 1)
            self.assertEqual(lane_events[0]["owner"], "gemini")
            self.assertEqual(lane_events[0]["lane_id"], "lane-a")

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
            self.assertIn("--metrics-file", argv)
            self.assertIn("--metrics-summary-file", argv)
            self.assertIn("--pricing-file", argv)

    def test_start_lanes_background_keeps_valid_lane_when_another_lane_is_invalid(self):
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
                            },
                            {
                                "id": "lane-b",
                                "enabled": True,
                                "owner": "unsupported-owner",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager.start_lane_background", return_value={"id": "lane-a", "pid": 91}) as start:
                payload = manager.start_lanes_background(cfg)
            self.assertEqual(payload["requested_lane"], "all_enabled")
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["config_error_count"], 1)
            self.assertEqual(payload["failed_count"], 1)
            self.assertFalse(payload["ok"])
            self.assertIn("lane-b", payload["config_errors"][0])
            self.assertEqual(payload["failed"][0]["source"], "lane_config")
            start.assert_called_once_with(cfg, "lane-a")

    def test_build_lane_runner_cmd_includes_dependency_state_and_handoff_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                                "dependency_state_file": "state/state.json",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            lane = manager.load_lane_specs(cfg)[0]
            argv = manager._build_lane_runner_cmd(cfg, lane)
            self.assertIn("--dependency-state-file", argv)
            self.assertIn(str(lane["dependency_state_file"]), argv)
            self.assertIn("--handoff-dir", argv)
            handoff_value = argv[argv.index("--handoff-dir") + 1]
            self.assertEqual(handoff_value, str(lane["handoff_dir"]))
            self.assertIn("--continuous", argv)
            self.assertIn("--continuous-recycle-delay-sec", argv)
            self.assertIn("--gemini-fallback-model", argv)
            self.assertIn("--auto-push-guard", argv)
            self.assertIn("--auto-push-interval-sec", argv)

    def test_ensure_lanes_background_starts_unexpectedly_stopped_lane(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            lanes_file = root / "config" / "lanes.json"
            lanes_file.write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
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
            pause = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "paused.flag"
            pause.parent.mkdir(parents=True, exist_ok=True)
            pause.write_text("manual\n", encoding="utf-8")
            with mock.patch("orxaq_autonomy.manager.start_lane_background", return_value={"id": "lane-a", "pid": 99}) as start, mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {
                            "id": "lane-a",
                            "running": False,
                            "heartbeat_stale": False,
                            "state_counts": {"done": 0, "pending": 1, "in_progress": 0, "blocked": 0},
                            "task_total": 1,
                        }
                    ]
                },
            ):
                payload = manager.ensure_lanes_background(cfg)
            # paused lanes are skipped
            self.assertEqual(payload["started_count"], 0)
            self.assertEqual(payload["skipped_count"], 1)
            start.assert_not_called()
            pause.unlink()
            with mock.patch("orxaq_autonomy.manager.start_lane_background", return_value={"id": "lane-a", "pid": 99}) as start, mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {
                            "id": "lane-a",
                            "running": False,
                            "heartbeat_stale": False,
                            "state_counts": {"done": 0, "pending": 1, "in_progress": 0, "blocked": 0},
                            "task_total": 1,
                        }
                    ]
                },
            ):
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["started_count"], 1)
            start.assert_called_once_with(cfg, "lane-a")

    def test_ensure_lanes_background_targets_single_lane(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            lanes_file = root / "config" / "lanes.json"
            lanes_file.write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-b",
                                "enabled": True,
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                side_effect=lambda config, lane_id: {"id": lane_id, "pid": 99},
            ) as start, mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {"id": "lane-a", "running": False, "heartbeat_stale": False, "build_current": True},
                        {"id": "lane-b", "running": False, "heartbeat_stale": False, "build_current": True},
                    ]
                },
            ):
                payload = manager.ensure_lanes_background(cfg, lane_id="lane-b")
            self.assertEqual(payload["requested_lane"], "lane-b")
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["started"][0]["id"], "lane-b")
            start.assert_called_once_with(cfg, "lane-b")

    def test_ensure_lanes_background_keeps_valid_lane_when_another_lane_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-b",
                                "enabled": True,
                                "owner": "unsupported-owner",
                                "impl_repo": str(root / "test_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                return_value={"id": "lane-a", "pid": 99},
            ) as start, mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {
                            "id": "lane-a",
                            "running": False,
                            "heartbeat_stale": False,
                            "build_current": True,
                            "state_counts": {"done": 0, "pending": 1, "in_progress": 0, "blocked": 0},
                            "task_total": 1,
                        }
                    ]
                },
            ):
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["requested_lane"], "all_enabled")
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["config_error_count"], 1)
            self.assertEqual(payload["failed_count"], 1)
            self.assertFalse(payload["ok"])
            self.assertIn("lane-b", payload["config_errors"][0])
            self.assertEqual(payload["failed"][0]["source"], "lane_config")
            start.assert_called_once_with(cfg, "lane-a")

    def test_ensure_lanes_background_raises_for_unknown_lane(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
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
            with self.assertRaises(RuntimeError):
                manager.ensure_lanes_background(cfg, lane_id="missing-lane")

    def test_start_lanes_background_reports_failure_when_lane_exits_immediately(self):
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
                return_value=False,
            ), mock.patch("orxaq_autonomy.manager.time.sleep", return_value=None):
                popen.return_value = mock.Mock(pid=777)
                payload = manager.start_lanes_background(cfg, lane_id="lane-a")
            self.assertEqual(payload["started_count"], 0)
            self.assertEqual(payload["failed_count"], 1)
            self.assertIn("exited immediately", payload["failed"][0]["error"])

    def test_ensure_lanes_background_restarts_completed_lane_for_continuous_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            lanes_file = root / "config" / "lanes.json"
            lanes_file.write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
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
            with mock.patch("orxaq_autonomy.manager.start_lane_background") as start, mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {
                            "id": "lane-a",
                            "running": False,
                            "heartbeat_stale": False,
                            "state_counts": {"done": 1, "pending": 0, "in_progress": 0, "blocked": 0},
                            "task_total": 1,
                        }
                    ]
                },
            ):
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["skipped_count"], 0)
            start.assert_called_once_with(cfg, "lane-a")

    def test_ensure_lanes_background_restarts_running_lane_on_build_update(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            lanes_file = root / "config" / "lanes.json"
            lanes_file.write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
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
            with mock.patch("orxaq_autonomy.manager.stop_lane_background", return_value={"id": "lane-a", "running": False}) as stop, mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                return_value={"id": "lane-a", "pid": 555},
            ) as start, mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {
                            "id": "lane-a",
                            "running": True,
                            "heartbeat_stale": False,
                            "build_current": False,
                            "state_counts": {"done": 0, "pending": 1, "in_progress": 0, "blocked": 0},
                            "task_total": 1,
                        }
                    ]
                },
            ):
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["restarted_count"], 1)
            stop.assert_called_once_with(cfg, "lane-a", reason="build_update")
            start.assert_called_once_with(cfg, "lane-a")

    def test_stop_lane_background_marks_pause_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            lane_id = "lane-x"
            pid_file = root / "artifacts" / "autonomy" / "lanes" / lane_id / "lane.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text("123\n", encoding="utf-8")
            with mock.patch("orxaq_autonomy.manager._terminate_pid"), mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"lanes": [{"id": lane_id, "running": False}]},
            ):
                manager.stop_lane_background(cfg, lane_id, reason="manual")
            pause_file = root / "artifacts" / "autonomy" / "lanes" / lane_id / "paused.flag"
            self.assertTrue(pause_file.exists())

    def test_stop_lane_background_terminates_pid_and_lock_pid(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
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
            lane_runtime = root / "artifacts" / "autonomy" / "lanes" / "lane-a"
            lane_runtime.mkdir(parents=True, exist_ok=True)
            (lane_runtime / "lane.pid").write_text("111\n", encoding="utf-8")
            (lane_runtime / "runner.lock").write_text(json.dumps({"pid": 222}) + "\n", encoding="utf-8")

            with mock.patch("orxaq_autonomy.manager._terminate_pid") as terminate, mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"lanes": [{"id": "lane-a", "running": False}]},
            ):
                manager.stop_lane_background(cfg, "lane-a", reason="manual")

            called_pids = [args[0][0] for args in terminate.call_args_list]
            self.assertEqual(called_pids, [111, 222])

    def test_lane_status_snapshot_adopts_running_lock_pid(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
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
            lane_runtime = root / "artifacts" / "autonomy" / "lanes" / "lane-a"
            lane_runtime.mkdir(parents=True, exist_ok=True)
            (lane_runtime / "runner.lock").write_text(json.dumps({"pid": 4242}) + "\n", encoding="utf-8")
            with mock.patch("orxaq_autonomy.manager._pid_running", side_effect=lambda pid: pid == 4242):
                snapshot = manager.lane_status_snapshot(cfg)
            lane = snapshot["lanes"][0]
            self.assertTrue(lane["running"])
            self.assertEqual(lane["pid"], 4242)
            pid_file = lane_runtime / "lane.pid"
            self.assertTrue(pid_file.exists())
            self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), "4242")

    def test_start_lane_background_skips_spawn_when_lock_pid_running(self):
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
            lane_runtime = root / "artifacts" / "autonomy" / "lanes" / "lane-a"
            lane_runtime.mkdir(parents=True, exist_ok=True)
            (lane_runtime / "runner.lock").write_text(json.dumps({"pid": 555}) + "\n", encoding="utf-8")

            with mock.patch("orxaq_autonomy.manager._resolve_binary", return_value="/usr/bin/codex"), mock.patch(
                "orxaq_autonomy.manager._repo_basic_check",
                return_value=(True, "ok"),
            ), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=lambda pid: pid == 555,
            ), mock.patch("orxaq_autonomy.manager.subprocess.Popen") as popen:
                payload = manager.start_lanes_background(cfg, lane_id="lane-a")
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["started"][0]["pid"], 555)
            popen.assert_not_called()

    def test_start_lane_background_recovers_when_pid_stale_and_lock_rotated(self):
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
            lane_runtime = root / "artifacts" / "autonomy" / "lanes" / "lane-a"
            lane_runtime.mkdir(parents=True, exist_ok=True)
            (lane_runtime / "lane.pid").write_text("111\n", encoding="utf-8")
            (lane_runtime / "runner.lock").write_text(json.dumps({"pid": 333}) + "\n", encoding="utf-8")

            with mock.patch("orxaq_autonomy.manager._resolve_binary", return_value="/usr/bin/codex"), mock.patch(
                "orxaq_autonomy.manager._repo_basic_check",
                return_value=(True, "ok"),
            ), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=lambda pid: pid == 333,
            ), mock.patch("orxaq_autonomy.manager.subprocess.Popen") as popen:
                payload = manager.start_lanes_background(cfg, lane_id="lane-a")
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["started"][0]["pid"], 333)
            popen.assert_not_called()

    def test_lane_status_snapshot_includes_lane_health_metadata(self):
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
            lane_runtime = root / "artifacts" / "autonomy" / "lanes" / "lane-a"
            lane_runtime.mkdir(parents=True, exist_ok=True)
            (lane_runtime / "lane.pid").write_text("891\n", encoding="utf-8")
            (lane_runtime / "heartbeat.json").write_text(
                json.dumps({"timestamp": "2000-01-01T00:00:00+00:00"}),
                encoding="utf-8",
            )
            with mock.patch("orxaq_autonomy.manager._pid_running", return_value=True):
                snapshot = manager.lane_status_snapshot(cfg)
            self.assertTrue(snapshot["ok"])
            lane = snapshot["lanes"][0]
            self.assertEqual(lane["owner"], "codex")
            self.assertEqual(lane["health"], "stale")
            self.assertTrue(lane["heartbeat_stale"])
            self.assertGreaterEqual(lane["heartbeat_age_sec"], 1)
            self.assertEqual(snapshot["health_counts"]["stale"], 1)
            self.assertEqual(snapshot["owner_counts"]["codex"]["total"], 1)
            self.assertEqual(snapshot["owner_counts"]["codex"]["degraded"], 1)

    def test_lane_status_snapshot_keeps_valid_lane_when_another_lane_is_invalid(self):
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
                            },
                            {
                                "id": "lane-b",
                                "enabled": True,
                                "owner": "unsupported-owner",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            snapshot = manager.lane_status_snapshot(cfg)
            self.assertEqual(snapshot["total_count"], 1)
            self.assertEqual(snapshot["lanes"][0]["id"], "lane-a")
            self.assertFalse(snapshot["ok"])
            self.assertIn("lane-b", snapshot["errors"][0])

    def test_lane_status_snapshot_treats_missing_state_entries_as_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "tasks.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "new-task",
                            "owner": "gemini",
                            "priority": 1,
                            "title": "New Task",
                            "description": "Desc",
                            "depends_on": [],
                            "acceptance": [],
                        }
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": True,
                                "owner": "gemini",
                                "impl_repo": str(root / "test_repo"),
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
            lane_runtime = root / "artifacts" / "autonomy" / "lanes" / "lane-a"
            lane_runtime.mkdir(parents=True, exist_ok=True)
            (lane_runtime / "state.json").write_text(
                json.dumps({"old-done-task": {"status": "done"}}) + "\n",
                encoding="utf-8",
            )
            snapshot = manager.lane_status_snapshot(cfg)
            lane = snapshot["lanes"][0]
            self.assertEqual(lane["task_total"], 1)
            self.assertEqual(lane["state_counts"]["pending"], 1)
            self.assertEqual(lane["missing_state_entries"], 1)
            self.assertEqual(lane["extra_state_entries"], 1)


if __name__ == "__main__":
    unittest.main()
