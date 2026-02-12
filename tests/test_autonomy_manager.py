import json
import os
import pathlib
import subprocess
import sys
import tempfile
import datetime as dt
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
        (tmp / "config" / "routellm_policy.json").write_text('{"version":1,"enabled":false}\n', encoding="utf-8")
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
            self.assertTrue(str(cfg.routellm_policy_file).endswith("routellm_policy.json"))
            self.assertTrue(str(cfg.supervisor_lock_file).endswith("supervisor.lock"))
            self.assertFalse(cfg.routellm_enabled)
            self.assertEqual(cfg.routellm_timeout_sec, 5)
            self.assertAlmostEqual(cfg.swarm_daily_budget_usd, 100.0, places=4)
            self.assertAlmostEqual(cfg.swarm_budget_warning_ratio, 0.8, places=6)
            self.assertTrue(cfg.swarm_budget_enforce_hard_stop)

    def test_manager_config_execution_profile_normalizes_alias(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            with mock.patch.dict(os.environ, {"ORXAQ_AUTONOMY_EXECUTION_PROFILE": "extra-high"}, clear=False):
                cfg = manager.ManagerConfig.from_root(root)
            self.assertEqual(cfg.execution_profile, "extra_high")

    def test_manager_config_execution_profile_invalid_falls_back_to_high(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            with mock.patch.dict(os.environ, {"ORXAQ_AUTONOMY_EXECUTION_PROFILE": "ultra"}, clear=False):
                cfg = manager.ManagerConfig.from_root(root)
            self.assertEqual(cfg.execution_profile, "high")

    def test_emit_mesh_event_best_effort_writes_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            manager._emit_mesh_event(
                cfg,
                topic="monitoring",
                event_type="unit.test",
                payload={"ok": True},
            )
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            self.assertTrue(events_file.exists())
            lines = [line for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["event_type"], "unit.test")

    def test_latest_mesh_scaling_decision_prefers_recent_matching_lane(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            events_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = manager._now_utc()
            recent = (now_utc - dt.timedelta(seconds=30)).isoformat()
            stale = (now_utc - dt.timedelta(minutes=20)).isoformat()
            events_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event_id": "evt_old",
                                "timestamp": stale,
                                "topic": "scaling",
                                "event_type": "decision.made",
                                "payload": {"requested_lane": "lane-a", "action": "scale_down", "target_delta": -1},
                            }
                        ),
                        json.dumps(
                            {
                                "event_id": "evt_new",
                                "timestamp": recent,
                                "topic": "scaling",
                                "event_type": "decision.made",
                                "payload": {"requested_lane": "lane-a", "action": "scale_up", "target_delta": 1},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            decision = manager._latest_mesh_scaling_decision(cfg, "lane-a")
            self.assertEqual(decision["action"], "scale_up")
            self.assertEqual(decision["event_id"], "evt_new")

    def test_consume_mesh_scaling_commands_stops_target_lane(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            events_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = manager._now_utc().isoformat()
            events_file.write_text(
                json.dumps(
                    {
                        "event_id": "evt_cmd_stop",
                        "timestamp": now_utc,
                        "topic": "scaling",
                        "event_type": "command.requested",
                        "payload": {"action": "scale_down", "target_lane": "lane-a", "requested_lane": "lane-a"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_map = {"lane-a": {"id": "lane-a", "enabled": True}}
            status_by_id = {"lane-a": {"running": True, "pid": 111}}
            with mock.patch("orxaq_autonomy.manager.stop_lane_background") as stop:
                effects = manager._consume_mesh_scaling_commands(
                    cfg,
                    lane_map=lane_map,
                    status_by_id=status_by_id,
                    requested_lane="lane-a",
                )
            self.assertTrue(effects["ok"])
            self.assertEqual(effects["stopped_count"], 1)
            stop.assert_called_once()

    def test_consume_mesh_scaling_commands_starts_target_lane(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            events_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = manager._now_utc().isoformat()
            events_file.write_text(
                json.dumps(
                    {
                        "event_id": "evt_cmd_start",
                        "timestamp": now_utc,
                        "topic": "scaling",
                        "event_type": "command.requested",
                        "payload": {"action": "scale_up", "target_lane": "lane-a", "requested_lane": "lane-a"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_map = {"lane-a": {"id": "lane-a", "enabled": True}}
            status_by_id = {"lane-a": {"running": False}}
            with mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                return_value={"id": "lane-a", "pid": 222},
            ) as start:
                effects = manager._consume_mesh_scaling_commands(
                    cfg,
                    lane_map=lane_map,
                    status_by_id=status_by_id,
                    requested_lane="lane-a",
                )
            self.assertTrue(effects["ok"])
            self.assertEqual(effects["started_count"], 1)
            start.assert_called_once()

    def test_consume_mesh_scaling_commands_rejects_stale_leader_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            events_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = manager._now_utc().isoformat()
            events_file.write_text(
                json.dumps(
                    {
                        "event_id": "evt_cmd_stale_epoch",
                        "timestamp": now_utc,
                        "topic": "scaling",
                        "event_type": "command.requested",
                        "payload": {
                            "command_id": "cmd_stale",
                            "action": "scale_down",
                            "target_lane": "lane-a",
                            "requested_lane": "lane-a",
                            "leader_epoch": 2,
                            "decision_table_version": "scaling_v2",
                            "execution_dag_id": "dag-lane-a",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            seen_state_file = cfg.state_file.parent / "mesh_scaling_command_seen.json"
            seen_state_file.parent.mkdir(parents=True, exist_ok=True)
            seen_state_file.write_text(
                json.dumps({"event_ids": [], "last_leader_epoch": 5}, indent=2) + "\n",
                encoding="utf-8",
            )
            lane_map = {"lane-a": {"id": "lane-a", "enabled": True}}
            status_by_id = {"lane-a": {"running": True, "pid": 111}}
            with mock.patch("orxaq_autonomy.manager.stop_lane_background") as stop:
                effects = manager._consume_mesh_scaling_commands(
                    cfg,
                    lane_map=lane_map,
                    status_by_id=status_by_id,
                    requested_lane="lane-a",
                )
            self.assertTrue(effects["ok"])
            self.assertEqual(effects["stopped_count"], 0)
            stop.assert_not_called()
            self.assertTrue(any(item.get("reason") == "stale_epoch" for item in effects.get("skipped", [])))
            state_payload = json.loads(seen_state_file.read_text(encoding="utf-8"))
            self.assertEqual(int(state_payload.get("last_leader_epoch", -1)), 5)
            command_log_file = root / "artifacts" / "autonomy" / "event_mesh" / "commands.ndjson"
            self.assertTrue(command_log_file.exists())
            log_rows = [
                json.loads(line)
                for line in command_log_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertGreaterEqual(len(log_rows), 1)
            self.assertEqual(log_rows[-1]["outcome"], "rejected_stale_epoch")

    def test_consume_mesh_scaling_commands_policy_selects_running_lane_for_scale_down(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            events_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = manager._now_utc().isoformat()
            events_file.write_text(
                json.dumps(
                    {
                        "event_id": "evt_cmd_policy_down",
                        "timestamp": now_utc,
                        "topic": "scaling",
                        "event_type": "command.requested",
                        "payload": {"action": "scale_down", "requested_lane": "all_enabled"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_map = {
                "lane-a": {"id": "lane-a", "enabled": True, "scaling_rank": 1},
                "lane-b": {"id": "lane-b", "enabled": True, "scaling_rank": 3},
            }
            status_by_id = {"lane-a": {"running": True}, "lane-b": {"running": True}}
            with mock.patch("orxaq_autonomy.manager.stop_lane_background") as stop:
                effects = manager._consume_mesh_scaling_commands(
                    cfg,
                    lane_map=lane_map,
                    status_by_id=status_by_id,
                    requested_lane="",
                )
            self.assertTrue(effects["ok"])
            self.assertEqual(effects["stopped_count"], 1)
            self.assertEqual(stop.call_args.kwargs["reason"], "mesh_scale_down")
            self.assertEqual(stop.call_args.args[1], "lane-b")

    def test_consume_mesh_scaling_commands_policy_selects_stopped_lane_for_scale_up(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            events_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = manager._now_utc().isoformat()
            events_file.write_text(
                json.dumps(
                    {
                        "event_id": "evt_cmd_policy_up",
                        "timestamp": now_utc,
                        "topic": "scaling",
                        "event_type": "command.requested",
                        "payload": {"action": "scale_up", "requested_lane": "all_enabled"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_map = {
                "lane-a": {"id": "lane-a", "enabled": True, "scaling_rank": 1},
                "lane-b": {"id": "lane-b", "enabled": True, "scaling_rank": 3},
            }
            status_by_id = {"lane-a": {"running": False}, "lane-b": {"running": False}}
            with mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                return_value={"id": "lane-a", "pid": 333},
            ) as start:
                effects = manager._consume_mesh_scaling_commands(
                    cfg,
                    lane_map=lane_map,
                    status_by_id=status_by_id,
                    requested_lane="",
                )
            self.assertTrue(effects["ok"])
            self.assertEqual(effects["started_count"], 1)
            self.assertEqual(start.call_args.args[1], "lane-a")

    def test_consume_mesh_scaling_commands_rejects_missing_causal_metadata_when_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            events_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = manager._now_utc().isoformat()
            events_file.write_text(
                json.dumps(
                    {
                        "event_id": "evt_cmd_causal_reject",
                        "timestamp": now_utc,
                        "topic": "scaling",
                        "event_type": "command.requested",
                        "payload": {
                            "command_id": "cmd_causal_reject",
                            "action": "scale_down",
                            "requested_lane": "all_enabled",
                            "leader_epoch": 6,
                            "decision_table_version": "scaling_v2",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_map = {"lane-a": {"id": "lane-a", "enabled": True, "scaling_rank": 1}}
            status_by_id = {"lane-a": {"running": True}}
            with mock.patch.dict("os.environ", {"ORXAQ_AUTONOMY_CAUSAL_GATE_MODE": "enforced"}, clear=False):
                with mock.patch("orxaq_autonomy.manager.stop_lane_background") as stop:
                    effects = manager._consume_mesh_scaling_commands(
                        cfg,
                        lane_map=lane_map,
                        status_by_id=status_by_id,
                        requested_lane="",
                    )
            self.assertTrue(effects["ok"])
            stop.assert_not_called()
            self.assertTrue(any(item.get("reason", "").startswith("missing_hypothesis") for item in effects.get("skipped", [])))

    def test_consume_mesh_scaling_commands_rejects_when_local_node_is_not_leader(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            lease_file = root / "state" / "event_mesh" / "leader_lease.json"
            lease_file.parent.mkdir(parents=True, exist_ok=True)
            lease_file.write_text(
                json.dumps(
                    {
                        "leader_id": "node-a",
                        "epoch": 8,
                        "lease_expires_at": (manager._now_utc() + dt.timedelta(minutes=5)).isoformat(),
                        "ttl_sec": 45,
                        "updated_at": manager._now_iso(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            events_file = root / "artifacts" / "autonomy" / "event_mesh" / "events.ndjson"
            events_file.parent.mkdir(parents=True, exist_ok=True)
            events_file.write_text(
                json.dumps(
                    {
                        "event_id": "evt_cmd_non_leader",
                        "timestamp": manager._now_utc().isoformat(),
                        "topic": "scaling",
                        "event_type": "command.requested",
                        "payload": {
                            "command_id": "cmd_non_leader",
                            "action": "scale_down",
                            "target_lane": "lane-a",
                            "requested_lane": "lane-a",
                            "leader_id": "node-a",
                            "leader_epoch": 8,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_map = {"lane-a": {"id": "lane-a", "enabled": True}}
            status_by_id = {"lane-a": {"running": True}}
            with mock.patch.dict(
                "os.environ",
                {"ORXAQ_AUTONOMY_NODE_ID": "node-b", "ORXAQ_AUTONOMY_ENFORCE_MESH_LEADER_FENCE": "1"},
                clear=False,
            ):
                with mock.patch("orxaq_autonomy.manager.stop_lane_background") as stop:
                    effects = manager._consume_mesh_scaling_commands(
                        cfg,
                        lane_map=lane_map,
                        status_by_id=status_by_id,
                        requested_lane="lane-a",
                    )
            self.assertTrue(effects["ok"])
            stop.assert_not_called()
            self.assertTrue(any(item.get("reason") == "leader_fence_local_follower" for item in effects.get("skipped", [])))

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
            with mock.patch(
                "orxaq_autonomy.manager._resolve_binary",
                return_value=None,
            ), mock.patch.dict(
                "os.environ",
                {
                    "ORXAQ_AUTONOMY_CODEX_CMD": "missing_codex_cmd",
                    "ORXAQ_AUTONOMY_GEMINI_CMD": "missing_gemini_cmd",
                },
                clear=False,
            ):
                cfg = manager.ManagerConfig.from_root(root)
                diagnostics = manager.runtime_diagnostics(cfg)
            self.assertFalse(diagnostics["ok"])
            self.assertGreaterEqual(len(diagnostics["errors"]), 2)
            joined = " ".join(diagnostics["errors"])
            self.assertIn("Codex CLI not found", joined)
            self.assertIn("Gemini CLI not found", joined)

    def test_runtime_env_prefers_known_paths_and_root_src(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src_dir = root / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            env = manager._runtime_env(
                {"PATH": "/usr/bin", "PYTHONPATH": "/tmp/custom"},
                root_dir=root,
            )
            path_entries = str(env.get("PATH", "")).split(os.pathsep)
            self.assertGreaterEqual(len(path_entries), 1)
            self.assertEqual(path_entries[0], "/opt/homebrew/bin")
            self.assertIn("/usr/bin", path_entries)
            py_entries = str(env.get("PYTHONPATH", "")).split(os.pathsep)
            self.assertGreaterEqual(len(py_entries), 1)
            self.assertEqual(py_entries[0], str(src_dir.resolve()))
            self.assertIn("/tmp/custom", py_entries)

    def test_manager_config_resolves_cli_commands_to_absolute_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            with mock.patch(
                "orxaq_autonomy.manager._resolve_binary",
                side_effect=lambda cmd: {
                    "codex": "/opt/homebrew/bin/codex",
                    "gemini": "/opt/homebrew/bin/gemini",
                    "claude": None,
                }.get(cmd),
            ):
                cfg = manager.ManagerConfig.from_root(root)
            self.assertEqual(cfg.codex_cmd, "/opt/homebrew/bin/codex")
            self.assertEqual(cfg.gemini_cmd, "/opt/homebrew/bin/gemini")
            self.assertEqual(cfg.claude_cmd, "claude")

    def test_pid_running_treats_zombie_as_not_running(self):
        fake_pid = 12345
        fake_ps = subprocess.CompletedProcess(["ps"], returncode=0, stdout="Z\n", stderr="")
        with mock.patch.object(manager.os, "kill", return_value=None):
            with mock.patch.object(manager.subprocess, "run", return_value=fake_ps):
                self.assertFalse(manager._pid_running(fake_pid))

    def test_pid_running_accepts_non_zombie_process(self):
        fake_pid = 12345
        fake_ps = subprocess.CompletedProcess(["ps"], returncode=0, stdout="Ss\n", stderr="")
        with mock.patch.object(manager.os, "kill", return_value=None):
            with mock.patch.object(manager.subprocess, "run", return_value=fake_ps):
                self.assertTrue(manager._pid_running(fake_pid))

    def test_runner_argv_contains_skill_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            argv = manager.runner_argv(cfg)
            self.assertIn("--task-queue-file", argv)
            self.assertIn("--task-queue-state-file", argv)
            self.assertIn("--execution-profile", argv)
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
            self.assertIn("--routellm-policy-file", argv)
            self.assertIn("--routellm-timeout-sec", argv)
            self.assertIn("--no-routellm-enabled", argv)
            self.assertIn("--gemini-fallback-model", argv)
            self.assertIn("--auto-push-guard", argv)
            self.assertIn("--auto-push-interval-sec", argv)

    def test_manager_config_reads_routellm_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            env_file.write_text(
                (
                    "OPENAI_API_KEY=test\n"
                    "GEMINI_API_KEY=test\n"
                    f"ORXAQ_IMPL_REPO={root / 'impl_repo'}\n"
                    f"ORXAQ_TEST_REPO={root / 'test_repo'}\n"
                    "ORXAQ_AUTONOMY_ROUTELLM_ENABLED=1\n"
                    "ORXAQ_AUTONOMY_ROUTELLM_URL=http://127.0.0.1:9000/route\n"
                    "ORXAQ_AUTONOMY_ROUTELLM_TIMEOUT_SEC=13\n"
                ),
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            self.assertTrue(cfg.routellm_enabled)
            self.assertEqual(cfg.routellm_url, "http://127.0.0.1:9000/route")
            self.assertEqual(cfg.routellm_timeout_sec, 13)
            argv = manager.runner_argv(cfg)
            self.assertIn("--routellm-enabled", argv)
            self.assertIn("--routellm-url", argv)

    def test_manager_config_reads_scaling_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            decision_file = root / "artifacts" / "autonomy" / "scaling.json"
            env_file.write_text(
                (
                    "OPENAI_API_KEY=test\n"
                    "GEMINI_API_KEY=test\n"
                    f"ORXAQ_IMPL_REPO={root / 'impl_repo'}\n"
                    f"ORXAQ_TEST_REPO={root / 'test_repo'}\n"
                    "ORXAQ_AUTONOMY_SCALING_ENABLED=1\n"
                    f"ORXAQ_AUTONOMY_SCALING_DECISION_FILE={decision_file}\n"
                    "ORXAQ_AUTONOMY_SCALING_MIN_NPV_USD=1.25\n"
                    "ORXAQ_AUTONOMY_SCALING_DAILY_BUDGET_USD=45\n"
                    "ORXAQ_AUTONOMY_SCALING_MAX_PARALLEL_AGENTS=4\n"
                    "ORXAQ_AUTONOMY_SCALING_MAX_SUBAGENTS_PER_AGENT=3\n"
                ),
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            self.assertTrue(cfg.scaling_enabled)
            self.assertEqual(cfg.scaling_decision_file, decision_file.resolve())
            self.assertAlmostEqual(cfg.scaling_min_marginal_npv_usd, 1.25, places=4)
            self.assertAlmostEqual(cfg.scaling_daily_budget_usd, 45.0, places=4)
            self.assertEqual(cfg.scaling_max_parallel_agents, 4)
            self.assertEqual(cfg.scaling_max_subagents_per_agent, 3)

    def test_manager_config_reads_swarm_budget_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            env_file.write_text(
                (
                    "OPENAI_API_KEY=test\n"
                    "GEMINI_API_KEY=test\n"
                    f"ORXAQ_IMPL_REPO={root / 'impl_repo'}\n"
                    f"ORXAQ_TEST_REPO={root / 'test_repo'}\n"
                    "ORXAQ_AUTONOMY_SWARM_DAILY_BUDGET_USD=125\n"
                    "ORXAQ_AUTONOMY_SWARM_BUDGET_WARNING_RATIO=0.65\n"
                    "ORXAQ_AUTONOMY_SWARM_BUDGET_ENFORCE_HARD_STOP=0\n"
                ),
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            self.assertAlmostEqual(cfg.swarm_daily_budget_usd, 125.0, places=4)
            self.assertAlmostEqual(cfg.swarm_budget_warning_ratio, 0.65, places=6)
            self.assertFalse(cfg.swarm_budget_enforce_hard_stop)

    def test_manager_config_reads_parallel_capacity_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            state_file = root / "artifacts" / "autonomy" / "parallel-state.json"
            log_file = root / "artifacts" / "autonomy" / "parallel-log.ndjson"
            env_file.write_text(
                (
                    "OPENAI_API_KEY=test\n"
                    "GEMINI_API_KEY=test\n"
                    f"ORXAQ_IMPL_REPO={root / 'impl_repo'}\n"
                    f"ORXAQ_TEST_REPO={root / 'test_repo'}\n"
                    "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_DEFAULT_LIMIT=3\n"
                    "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_RECOVERY_CYCLES=5\n"
                    "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_MAX_LIMIT=11\n"
                    f"ORXAQ_AUTONOMY_PARALLEL_CAPACITY_STATE_FILE={state_file}\n"
                    f"ORXAQ_AUTONOMY_PARALLEL_CAPACITY_LOG_FILE={log_file}\n"
                ),
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            self.assertEqual(cfg.parallel_capacity_default_limit, 3)
            self.assertEqual(cfg.parallel_capacity_recovery_cycles, 5)
            self.assertEqual(cfg.parallel_capacity_max_limit, 11)
            self.assertEqual(cfg.parallel_capacity_state_file, state_file.resolve())
            self.assertEqual(cfg.parallel_capacity_log_file, log_file.resolve())

    def test_manager_config_reads_provider_cost_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            records_file = root / "artifacts" / "autonomy" / "provider_costs" / "records.ndjson"
            summary_file = root / "artifacts" / "autonomy" / "provider_costs" / "summary.json"
            env_file.write_text(
                (
                    "OPENAI_API_KEY=test\n"
                    "GEMINI_API_KEY=test\n"
                    f"ORXAQ_IMPL_REPO={root / 'impl_repo'}\n"
                    f"ORXAQ_TEST_REPO={root / 'test_repo'}\n"
                    f"ORXAQ_AUTONOMY_PROVIDER_COST_RECORDS_FILE={records_file}\n"
                    f"ORXAQ_AUTONOMY_PROVIDER_COST_SUMMARY_FILE={summary_file}\n"
                    "ORXAQ_AUTONOMY_PROVIDER_COST_STALE_SEC=321\n"
                ),
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            self.assertEqual(cfg.provider_cost_records_file, records_file.resolve())
            self.assertEqual(cfg.provider_cost_summary_file, summary_file.resolve())
            self.assertEqual(cfg.provider_cost_stale_sec, 321)

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

    def test_local_model_fleet_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            fleet = manager._local_model_fleet_snapshot(cfg)
            self.assertFalse(fleet["ok"])
            self.assertIn("fleet_status_missing", fleet["error"])

            status_file = cfg.artifacts_dir / "local_models" / "fleet_status.json"
            status_file.parent.mkdir(parents=True, exist_ok=True)
            status_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-02-09T18:00:00Z",
                        "probe": {"summary": {"endpoint_total": 2, "endpoint_healthy": 2}},
                        "benchmark": {"summary": {"requests": 4, "ok": 4, "errors": 0}},
                        "sync": {"message": "dry_run_no_command_template"},
                    }
                ),
                encoding="utf-8",
            )
            fleet = manager._local_model_fleet_snapshot(cfg)
            self.assertTrue(fleet["ok"])
            self.assertEqual(fleet["summary"]["endpoint_total"], 2)
            self.assertEqual(fleet["benchmark"]["requests"], 4)

    def test_monitor_snapshot_includes_routing_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.metrics_summary_file.write_text(
                json.dumps(
                    {
                        "responses_total": 2,
                        "quality_score_avg": 0.9,
                        "latency_sec_avg": 1.2,
                        "prompt_difficulty_score_avg": 33.0,
                        "first_time_pass_count": 2,
                        "first_time_pass_rate": 1.0,
                        "acceptance_pass_count": 2,
                        "acceptance_pass_rate": 1.0,
                        "exact_cost_count": 2,
                        "exact_cost_coverage": 1.0,
                        "cost_usd_total": 0.03,
                        "cost_usd_avg": 0.015,
                        "tokens_total": 300,
                        "tokens_input_total": 210,
                        "tokens_output_total": 90,
                        "tokens_avg": 150.0,
                        "token_exact_count": 2,
                        "token_exact_coverage": 1.0,
                        "token_rate_per_minute": 25.0,
                        "routing_decisions_total": 2,
                        "routing_routellm_count": 1,
                        "routing_routellm_rate": 0.5,
                        "routing_fallback_count": 1,
                        "routing_fallback_rate": 0.5,
                        "routing_router_error_count": 1,
                        "routing_router_error_rate": 0.5,
                        "routing_router_latency_sum": 0.4,
                        "routing_router_latency_avg": 0.2,
                        "routing_by_provider": {
                            "codex": {
                                "responses": 2,
                                "routellm_count": 1,
                                "fallback_count": 1,
                                "router_error_count": 1,
                            }
                        },
                        "currency": "USD",
                        "by_owner": {},
                        "latest_metric": {},
                        "optimization_recommendations": [],
                    }
                ),
                encoding="utf-8",
            )
            snapshot = manager.monitor_snapshot(cfg)
            metrics = snapshot["response_metrics"]
            self.assertEqual(metrics["routing_decisions_total"], 2)
            self.assertEqual(metrics["routing_routellm_count"], 1)
            self.assertEqual(metrics["routing_fallback_count"], 1)
            self.assertEqual(metrics["routing_router_error_count"], 1)

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

    def test_start_background_emits_mesh_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager._read_pid", return_value=None), mock.patch(
                "orxaq_autonomy.manager._pid_running", return_value=False
            ), mock.patch("orxaq_autonomy.manager.ensure_runtime"), mock.patch(
                "orxaq_autonomy.manager.subprocess.Popen"
            ) as popen, mock.patch("orxaq_autonomy.manager._emit_mesh_event") as emit:
                popen.return_value = mock.Mock(pid=2222)
                manager.start_background(cfg)
            self.assertTrue(any(call.kwargs.get("event_type") == "supervisor.started" for call in emit.mock_calls))

    def test_stop_background_emits_mesh_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager._read_pid",
                side_effect=[123, 456],
            ), mock.patch("orxaq_autonomy.manager._terminate_pid"), mock.patch(
                "orxaq_autonomy.manager._emit_mesh_event"
            ) as emit:
                manager.stop_background(cfg)
            self.assertTrue(any(call.kwargs.get("event_type") == "supervisor.stopped" for call in emit.mock_calls))

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

    def test_ensure_background_emits_lane_summary_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.lanes_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.lanes_file.write_text("[]\n", encoding="utf-8")
            with mock.patch(
                "orxaq_autonomy.manager._read_pid",
                side_effect=[123, 456],
            ), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=[True, False],
            ), mock.patch(
                "orxaq_autonomy.manager._heartbeat_age_sec",
                return_value=5,
            ), mock.patch(
                "orxaq_autonomy.manager.ensure_lanes_background",
                return_value={
                    "requested_lane": "all_enabled",
                    "ensured_count": 0,
                    "started_count": 1,
                    "restarted_count": 0,
                    "scaled_up_count": 0,
                    "scaled_down_count": 0,
                    "failed_count": 0,
                    "ok": True,
                    "parallel_capacity": {"groups": {}},
                },
            ), mock.patch("orxaq_autonomy.manager._emit_mesh_event") as emit, mock.patch(
                "orxaq_autonomy.manager._dispatch_mesh_events"
            ) as dispatch:
                manager.ensure_background(cfg)
            self.assertTrue(any(call.kwargs.get("event_type") == "lanes.ensure.completed" for call in emit.mock_calls))
            dispatch.assert_called()

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

    def test_process_watchdog_pass_orders_processes_and_writes_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.supervisor_pid_file.write_text("111\n", encoding="utf-8")
            cfg.runner_pid_file.write_text("222\n", encoding="utf-8")
            with mock.patch("orxaq_autonomy.manager._pid_running", return_value=True), mock.patch(
                "orxaq_autonomy.manager._heartbeat_age_sec",
                return_value=0,
            ):
                payload = manager.process_watchdog_pass(cfg)
            self.assertEqual([item["id"] for item in payload["processes"]], ["supervisor", "runner"])
            self.assertTrue(payload["ok"])
            self.assertTrue(pathlib.Path(payload["state_file"]).exists())
            self.assertTrue(pathlib.Path(payload["history_file"]).exists())

    def test_process_watchdog_pass_restarts_missing_supervisor(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)

            def fake_start(_cfg: manager.ManagerConfig) -> None:
                _cfg.supervisor_pid_file.write_text("321\n", encoding="utf-8")
                _cfg.runner_pid_file.write_text("654\n", encoding="utf-8")

            with mock.patch("orxaq_autonomy.manager.start_background", side_effect=fake_start), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=lambda pid: isinstance(pid, int) and pid in {321, 654},
            ), mock.patch(
                "orxaq_autonomy.manager._heartbeat_age_sec",
                return_value=-1,
            ):
                payload = manager.process_watchdog_pass(cfg)
            self.assertEqual(payload["processes"][0]["id"], "supervisor")
            self.assertEqual(payload["processes"][0]["status"], "restarted")
            self.assertTrue(payload["ok"])

    def test_process_watchdog_pass_uses_spawned_supervisor_pid_when_pid_file_lags(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)

            def fake_pid_running(pid: int | None) -> bool:
                return pid == 777

            with mock.patch("orxaq_autonomy.manager.start_background", return_value=777), mock.patch(
                "orxaq_autonomy.manager._read_pid",
                return_value=None,
            ), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=fake_pid_running,
            ), mock.patch(
                "orxaq_autonomy.manager._wait_for_running_pid",
                side_effect=[None, None],
            ), mock.patch(
                "orxaq_autonomy.manager._heartbeat_age_sec",
                return_value=-1,
            ), mock.patch(
                "orxaq_autonomy.manager._restart_supervisor_for_watchdog"
            ) as restart_supervisor:
                payload = manager.process_watchdog_pass(cfg)

            self.assertEqual(payload["processes"][0]["status"], "restarted")
            self.assertIn("waiting for pid file", payload["processes"][0]["detail"])
            self.assertEqual(payload["processes"][1]["status"], "down_no_restart")
            restart_supervisor.assert_not_called()

    def test_process_watchdog_pass_uses_safe_default_startup_grace(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.supervisor_pid_file.write_text("111\n", encoding="utf-8")
            cfg.runner_pid_file.write_text("222\n", encoding="utf-8")
            with mock.patch("orxaq_autonomy.manager._pid_running", return_value=True), mock.patch(
                "orxaq_autonomy.manager._heartbeat_age_sec",
                return_value=0,
            ):
                payload = manager.process_watchdog_pass(cfg)
            self.assertEqual(payload["startup_grace_sec"], 8)

    def test_process_watchdog_pass_treats_completed_runner_lifecycle_as_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.supervisor_pid_file.write_text("111\n", encoding="utf-8")
            cfg.heartbeat_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-02-12T01:06:18.235933+00:00",
                        "phase": "completed",
                        "message": "all tasks completed",
                    }
                ),
                encoding="utf-8",
            )
            cfg.state_file.write_text(
                json.dumps({"task-a": {"status": "done"}, "task-b": {"status": "done"}}),
                encoding="utf-8",
            )

            with mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=lambda pid: pid == 111,
            ), mock.patch(
                "orxaq_autonomy.manager._heartbeat_age_sec",
                return_value=10,
            ), mock.patch(
                "orxaq_autonomy.manager._restart_supervisor_for_watchdog"
            ) as restart_supervisor:
                payload = manager.process_watchdog_pass(cfg)

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["processes"][1]["id"], "runner")
            self.assertEqual(payload["processes"][1]["status"], "healthy")
            self.assertIn("runner idle", payload["processes"][1]["detail"])
            restart_supervisor.assert_not_called()

    def test_full_autonomy_snapshot_writes_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager.preflight", return_value={"clean": True}), mock.patch(
                "orxaq_autonomy.manager.process_watchdog_pass",
                return_value={"ok": True},
            ), mock.patch(
                "orxaq_autonomy.manager.status_snapshot",
                return_value={"supervisor_running": True, "runner_running": True},
            ), mock.patch(
                "orxaq_autonomy.manager.health_snapshot",
                return_value={"heartbeat_stale": False},
            ):
                payload = manager.full_autonomy_snapshot(cfg)
            self.assertTrue(payload["ok"])
            self.assertTrue(pathlib.Path(payload["full_autonomy_report_file"]).exists())

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

    def test_monitor_snapshot_reports_completed_last_24h_from_task_done_events(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            conv_file = root / "artifacts" / "autonomy" / "custom_conversations.ndjson"
            conv_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = manager._now_utc()
            recent_ts = (now_utc - dt.timedelta(hours=2)).isoformat()
            old_ts = (now_utc - dt.timedelta(hours=30)).isoformat()
            conv_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": recent_ts,
                                "event_type": "task_done",
                                "owner": "codex",
                                "task_id": "task-a",
                                "cycle": 1,
                            }
                        ),
                        # Duplicate event should be deduplicated by key.
                        json.dumps(
                            {
                                "timestamp": recent_ts,
                                "event_type": "task_done",
                                "owner": "codex",
                                "task_id": "task-a",
                                "cycle": 1,
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": recent_ts,
                                "event_type": "task_done",
                                "owner": "gemini",
                                "task_id": "task-b",
                                "cycle": 2,
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": old_ts,
                                "event_type": "task_done",
                                "owner": "codex",
                                "task_id": "old-task",
                                "cycle": 3,
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": recent_ts,
                                "event_type": "task_blocked",
                                "owner": "codex",
                                "task_id": "blocked-task",
                                "cycle": 4,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
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
                    "sources": [
                        {
                            "path": str(conv_file),
                            "resolved_path": str(conv_file),
                            "kind": "primary",
                            "resolved_kind": "primary",
                            "lane_id": "",
                            "owner": "",
                            "ok": True,
                            "missing": False,
                            "recoverable_missing": False,
                            "fallback_used": False,
                            "error": "",
                            "event_count": 5,
                        }
                    ],
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
            progress = snapshot["progress"]
            self.assertEqual(progress["completed_last_24h"], 2)
            self.assertEqual(progress["completed_last_24h_unique_tasks"], 2)
            self.assertEqual(progress["completed_last_24h_by_owner"]["codex"], 1)
            self.assertEqual(progress["completed_last_24h_by_owner"]["gemini"], 1)
            self.assertEqual(progress["completed_last_24h_sources_scanned"], 1)
            self.assertEqual(progress["completed_last_24h_errors"], [])
            self.assertTrue(snapshot["diagnostics"]["sources"]["completed_last_24h"]["ok"])

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

    def test_response_metrics_snapshot_aggregates_estimated_tokens_and_provider_costs(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.metrics_summary_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.metrics_summary_file.write_text(
                json.dumps(
                    {
                        "responses_total": 1,
                        "quality_score_sum": 0.9,
                        "latency_sec_sum": 8.0,
                        "prompt_difficulty_score_sum": 30.0,
                        "tokens_total": 90,
                        "estimated_tokens_total": 100,
                        "tokens_input_total": 60,
                        "tokens_output_total": 30,
                        "token_exact_count": 1,
                        "first_time_pass_count": 1,
                        "acceptance_pass_count": 1,
                        "exact_cost_count": 1,
                        "cost_usd_total": 1.0,
                        "routing_decisions_total": 1,
                        "routing_routellm_count": 1,
                        "routing_fallback_count": 0,
                        "routing_router_error_count": 0,
                        "routing_by_provider": {
                            "codex": {
                                "responses": 1,
                                "routellm_count": 1,
                                "fallback_count": 0,
                                "router_error_count": 0,
                                "cost_usd_total": 1.0,
                                "tokens_total": 90,
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_metrics = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "response_metrics_summary.json"
            lane_metrics.parent.mkdir(parents=True, exist_ok=True)
            lane_metrics.write_text(
                json.dumps(
                    {
                        "responses_total": 1,
                        "quality_score_sum": 0.7,
                        "latency_sec_sum": 4.0,
                        "prompt_difficulty_score_sum": 15.0,
                        "tokens_total": 60,
                        "estimated_tokens_total": 70,
                        "tokens_input_total": 40,
                        "tokens_output_total": 20,
                        "token_exact_count": 1,
                        "first_time_pass_count": 1,
                        "acceptance_pass_count": 1,
                        "exact_cost_count": 1,
                        "cost_usd_total": 0.5,
                        "routing_decisions_total": 1,
                        "routing_routellm_count": 1,
                        "routing_fallback_count": 0,
                        "routing_router_error_count": 0,
                        "routing_by_provider": {
                            "codex": {
                                "responses": 1,
                                "routellm_count": 1,
                                "fallback_count": 0,
                                "router_error_count": 0,
                                "cost_usd_total": 0.5,
                                "tokens_total": 60,
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager._response_metrics_snapshot(
                cfg,
                lane_items=[{"metrics_summary_file": str(lane_metrics)}],
            )
            self.assertEqual(snapshot["tokens_total"], 150)
            self.assertEqual(snapshot["estimated_tokens_total"], 170)
            self.assertAlmostEqual(snapshot["estimated_cost_per_million_tokens"], 8823.529412, places=6)
            self.assertIn("codex", snapshot["routing_by_provider"])
            self.assertEqual(snapshot["routing_by_provider"]["codex"]["tokens_total"], 150)
            self.assertAlmostEqual(snapshot["routing_by_provider"]["codex"]["cost_usd_total"], 1.5, places=6)
            self.assertAlmostEqual(snapshot["routing_by_provider"]["codex"]["cost_per_million_tokens"], 10000.0, places=6)

    def test_response_metrics_snapshot_builds_windowed_costs_and_splits_from_metrics_events(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)

            primary_metrics = cfg.metrics_file
            primary_metrics.parent.mkdir(parents=True, exist_ok=True)
            lane_metrics = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "response_metrics.ndjson"
            lane_metrics.parent.mkdir(parents=True, exist_ok=True)

            now_utc = dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
            primary_events = [
                {
                    "timestamp": (now_utc - dt.timedelta(minutes=10)).isoformat(),
                    "cost_usd": 1.0,
                    "total_tokens": 100,
                    "routing_provider": "codex",
                    "model": "gpt-4.1-mini",
                },
                {
                    "timestamp": (now_utc - dt.timedelta(days=2)).isoformat(),
                    "cost_usd": 3.0,
                    "total_tokens": 300,
                    "routing_provider": "claude",
                    "model": "claude-3-7-sonnet-latest",
                },
            ]
            lane_events = [
                {
                    "timestamp": (now_utc - dt.timedelta(hours=2)).isoformat(),
                    "cost_usd": 2.0,
                    "total_tokens": 200,
                    "routing_provider": "gemini",
                    "model": "gemini-2.5-flash",
                },
                {
                    "timestamp": (now_utc - dt.timedelta(days=10)).isoformat(),
                    "cost_usd": 4.0,
                    "total_tokens": 400,
                    "routing_provider": "codex",
                    "model": "gpt-4.1",
                },
                {
                    "timestamp": (now_utc - dt.timedelta(days=40)).isoformat(),
                    "cost_usd": 5.0,
                    "total_tokens": 500,
                    "routing_provider": "legacy",
                    "model": "old-model",
                },
            ]
            primary_metrics.write_text(
                "\n".join(json.dumps(item, sort_keys=True) for item in primary_events) + "\n",
                encoding="utf-8",
            )
            lane_metrics.write_text(
                "\n".join(json.dumps(item, sort_keys=True) for item in lane_events) + "\n",
                encoding="utf-8",
            )

            with mock.patch("orxaq_autonomy.manager._now_utc", return_value=now_utc):
                snapshot = manager._response_metrics_snapshot(
                    cfg,
                    lane_items=[{"metrics_file": str(lane_metrics)}],
                )

            windows = snapshot["cost_windows_usd"]
            self.assertAlmostEqual(windows["last_hour"], 1.0, places=6)
            self.assertAlmostEqual(windows["today"], 3.0, places=6)
            self.assertAlmostEqual(windows["last_7d"], 6.0, places=6)
            self.assertAlmostEqual(windows["last_30d"], 10.0, places=6)

            provider_split = snapshot["provider_cost_30d"]
            self.assertIn("codex", provider_split)
            self.assertIn("gemini", provider_split)
            self.assertIn("claude", provider_split)
            self.assertAlmostEqual(provider_split["codex"]["cost_usd_total"], 5.0, places=6)
            self.assertEqual(provider_split["codex"]["responses"], 2)

            model_split = snapshot["model_cost_30d"]
            self.assertIn("gpt-4.1-mini", model_split)
            self.assertIn("gemini-2.5-flash", model_split)
            self.assertIn("claude-3-7-sonnet-latest", model_split)
            self.assertIn("gpt-4.1", model_split)
            self.assertNotIn("old-model", model_split)

            freshness = snapshot["data_freshness"]
            self.assertEqual(
                freshness["latest_event_timestamp"],
                (now_utc - dt.timedelta(minutes=10)).isoformat(),
            )
            self.assertEqual(freshness["age_sec"], 600)
            self.assertFalse(freshness["stale"])
            self.assertEqual(freshness["files_scanned"], 2)
            self.assertEqual(freshness["events_scanned"], 5)

            series = snapshot["cost_series_hourly_24h"]
            self.assertEqual(len(series), 24)
            total_24h_cost = sum(float(item.get("cost_usd_total", 0.0) or 0.0) for item in series)
            self.assertAlmostEqual(total_24h_cost, 3.0, places=6)

    def test_response_metrics_snapshot_prefers_authoritative_provider_cost_records(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.metrics_summary_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.metrics_summary_file.write_text(
                json.dumps(
                    {
                        "responses_total": 2,
                        "quality_score_sum": 1.5,
                        "latency_sec_sum": 10.0,
                        "prompt_difficulty_score_sum": 20.0,
                        "tokens_total": 1000,
                        "tokens_input_total": 700,
                        "tokens_output_total": 300,
                        "token_exact_count": 2,
                        "first_time_pass_count": 1,
                        "acceptance_pass_count": 1,
                        "exact_cost_count": 1,
                        "cost_usd_total": 0.5,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            provider_records = [
                {
                    "record_id": "auth-1",
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "window_start": "2026-01-02T11:00:00+00:00",
                    "window_end": "2026-01-02T11:10:00+00:00",
                    "timestamp": "2026-01-02T11:10:00+00:00",
                    "total_tokens": 120,
                    "total_cost_usd": 1.2,
                    "currency": "USD",
                    "source_of_truth": "authoritative_provider_api",
                },
                {
                    "record_id": "auth-2",
                    "provider": "anthropic",
                    "model": "claude-3-7-sonnet-latest",
                    "window_start": "2026-01-01T11:00:00+00:00",
                    "window_end": "2026-01-01T11:10:00+00:00",
                    "timestamp": "2026-01-01T11:10:00+00:00",
                    "total_tokens": 200,
                    "total_cost_usd": 2.5,
                    "currency": "USD",
                    "source_of_truth": "authoritative_provider_api",
                },
            ]
            cfg.provider_cost_records_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.provider_cost_records_file.write_text(
                "\n".join(json.dumps(item, sort_keys=True) for item in provider_records) + "\n",
                encoding="utf-8",
            )

            now_utc = dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
            with mock.patch("orxaq_autonomy.manager._now_utc", return_value=now_utc):
                snapshot = manager._response_metrics_snapshot(cfg, lane_items=[])

            self.assertTrue(snapshot["authoritative_cost_available"])
            self.assertEqual(snapshot["source_of_truth"], "authoritative_provider_api")
            self.assertEqual(snapshot["authoritative_cost_records_total"], 2)
            self.assertAlmostEqual(snapshot["cost_windows_usd"]["last_hour"], 1.2, places=6)
            self.assertIn("openai", snapshot["provider_cost_30d"])
            self.assertIn("gpt-4.1-mini", snapshot["model_cost_30d"])
            self.assertEqual(snapshot["data_freshness"]["age_sec"], 3000)

    def test_response_metrics_snapshot_reports_swarm_budget_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            with mock.patch.dict(
                os.environ,
                {
                    "ORXAQ_AUTONOMY_SWARM_DAILY_BUDGET_USD": "100",
                    "ORXAQ_AUTONOMY_SWARM_BUDGET_WARNING_RATIO": "0.8",
                    "ORXAQ_AUTONOMY_SWARM_BUDGET_ENFORCE_HARD_STOP": "1",
                },
                clear=False,
            ):
                cfg = manager.ManagerConfig.from_root(root)

            cfg.metrics_file.parent.mkdir(parents=True, exist_ok=True)
            now_utc = dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
            cfg.metrics_file.write_text(
                json.dumps(
                    {
                        "timestamp": (now_utc - dt.timedelta(minutes=5)).isoformat(),
                        "cost_usd": 120.0,
                        "total_tokens": 1000,
                        "routing_provider": "codex",
                        "model": "gpt-4.1-mini",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch("orxaq_autonomy.manager._now_utc", return_value=now_utc):
                snapshot = manager._response_metrics_snapshot(cfg, lane_items=[])
            budget = snapshot["swarm_daily_budget"]
            self.assertTrue(budget["enabled"])
            self.assertEqual(budget["state"], "exceeded")
            self.assertTrue(budget["hard_stop"])
            self.assertAlmostEqual(budget["daily_budget_usd"], 100.0, places=6)
            self.assertAlmostEqual(budget["daily_spend_usd"], 120.0, places=6)

    def test_response_metrics_snapshot_prefers_latest_metric_by_utc_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.metrics_summary_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.metrics_summary_file.write_text(
                json.dumps(
                    {
                        "responses_total": 1,
                        "quality_score_sum": 1.0,
                        "latency_sec_sum": 2.0,
                        "prompt_difficulty_score_sum": 3.0,
                        "tokens_total": 100,
                        "tokens_input_total": 60,
                        "tokens_output_total": 40,
                        "token_exact_count": 1,
                        "first_time_pass_count": 1,
                        "acceptance_pass_count": 1,
                        "exact_cost_count": 1,
                        "cost_usd_total": 0.1,
                        "latest_metric": {
                            "timestamp": "2026-01-01T01:30:00+01:00",
                            "task_id": "earlier-utc",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_metrics = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "response_metrics_summary.json"
            lane_metrics.parent.mkdir(parents=True, exist_ok=True)
            lane_metrics.write_text(
                json.dumps(
                    {
                        "responses_total": 1,
                        "quality_score_sum": 0.5,
                        "latency_sec_sum": 1.0,
                        "prompt_difficulty_score_sum": 2.0,
                        "tokens_total": 80,
                        "tokens_input_total": 50,
                        "tokens_output_total": 30,
                        "token_exact_count": 1,
                        "first_time_pass_count": 1,
                        "acceptance_pass_count": 1,
                        "exact_cost_count": 1,
                        "cost_usd_total": 0.05,
                        "latest_metric": {
                            "timestamp": "2026-01-01T00:45:00+00:00",
                            "task_id": "later-utc",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager._response_metrics_snapshot(
                cfg,
                lane_items=[{"metrics_summary_file": str(lane_metrics)}],
            )
            self.assertEqual(snapshot["latest_metric"]["task_id"], "later-utc")
            self.assertEqual(snapshot["latest_metric"]["timestamp"], "2026-01-01T00:45:00+00:00")

    def test_response_metrics_snapshot_uses_source_sequence_for_invalid_latest_metric_timestamps(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.metrics_summary_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.metrics_summary_file.write_text(
                json.dumps(
                    {
                        "responses_total": 1,
                        "quality_score_sum": 1.0,
                        "latency_sec_sum": 2.0,
                        "prompt_difficulty_score_sum": 3.0,
                        "tokens_total": 100,
                        "tokens_input_total": 60,
                        "tokens_output_total": 40,
                        "token_exact_count": 1,
                        "first_time_pass_count": 1,
                        "acceptance_pass_count": 1,
                        "exact_cost_count": 1,
                        "cost_usd_total": 0.1,
                        "latest_metric": {
                            "timestamp": "z-invalid",
                            "task_id": "first-invalid",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lane_metrics = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "response_metrics_summary.json"
            lane_metrics.parent.mkdir(parents=True, exist_ok=True)
            lane_metrics.write_text(
                json.dumps(
                    {
                        "responses_total": 1,
                        "quality_score_sum": 0.5,
                        "latency_sec_sum": 1.0,
                        "prompt_difficulty_score_sum": 2.0,
                        "tokens_total": 80,
                        "tokens_input_total": 50,
                        "tokens_output_total": 30,
                        "token_exact_count": 1,
                        "first_time_pass_count": 1,
                        "acceptance_pass_count": 1,
                        "exact_cost_count": 1,
                        "cost_usd_total": 0.05,
                        "latest_metric": {
                            "timestamp": "a-invalid",
                            "task_id": "second-invalid",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager._response_metrics_snapshot(
                cfg,
                lane_items=[{"metrics_summary_file": str(lane_metrics)}],
            )
            self.assertEqual(snapshot["latest_metric"]["task_id"], "second-invalid")
            self.assertEqual(snapshot["latest_metric"]["timestamp"], "a-invalid")

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
            self.assertTrue(snapshot["lanes"]["partial"])
            self.assertIn("lane source unavailable", snapshot["lanes"]["errors"][0])

    def test_monitor_snapshot_uses_lane_plan_fallback_when_lane_status_fails(self):
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
            self.assertEqual(snapshot["lanes"]["total_count"], 1)
            self.assertEqual(snapshot["lanes"]["lanes"][0]["id"], "lane-a")
            self.assertEqual(snapshot["lanes"]["lanes"][0]["owner"], "codex")
            self.assertEqual(snapshot["lanes"]["lanes"][0]["health"], "unknown")
            self.assertEqual(snapshot["lanes"]["owner_counts"]["codex"]["total"], 1)
            self.assertFalse(snapshot["lanes"]["ok"])
            self.assertTrue(snapshot["lanes"]["partial"])

    def test_monitor_snapshot_recovers_lane_from_conversations_when_lane_source_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "ok": False,
                    "errors": ["lane status source unavailable"],
                    "running_count": 0,
                    "total_count": 0,
                    "lanes": [],
                    "health_counts": {},
                    "owner_counts": {},
                    "partial": True,
                },
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 1,
                    "events": [
                        {
                            "timestamp": "2026-01-01T00:45:00+00:00",
                            "owner": "gemini",
                            "lane_id": "lane-b",
                            "event_type": "status",
                            "content": "lane recovered from conversation stream",
                        }
                    ],
                    "owner_counts": {"gemini": 1},
                    "partial": True,
                    "ok": False,
                    "errors": ["primary conversation stream degraded"],
                    "sources": [
                        {
                            "kind": "lane",
                            "resolved_kind": "lane_events",
                            "lane_id": "lane-b",
                            "owner": "",
                            "ok": True,
                            "missing": True,
                            "recoverable_missing": True,
                            "fallback_used": True,
                            "error": "",
                            "event_count": 1,
                        }
                    ],
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
            self.assertEqual(snapshot["lanes"]["recovered_lane_count"], 1)
            self.assertEqual(snapshot["lanes"]["lanes"][0]["id"], "lane-b")
            self.assertEqual(snapshot["lanes"]["lanes"][0]["owner"], "gemini")
            self.assertTrue(snapshot["lanes"]["lanes"][0]["conversation_lane_fallback"])
            self.assertEqual(snapshot["lanes"]["owner_counts"]["gemini"]["total"], 1)
            self.assertEqual(snapshot["runtime"]["lane_owner_health"]["gemini"]["total"], 1)
            self.assertIn(
                "Lane status missing for 'lane-b'; using conversation-derived fallback.",
                snapshot["lanes"]["errors"],
            )

    def test_monitor_snapshot_recovery_suppresses_requested_lane_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "ok": False,
                    "errors": [
                        "lane status source unavailable",
                        "Unknown lane id 'lane-b'. Update config/lanes.json.",
                        "Requested lane 'lane-b' is unavailable because lane status sources failed.",
                    ],
                    "running_count": 0,
                    "total_count": 0,
                    "lanes": [],
                    "health_counts": {},
                    "owner_counts": {},
                    "partial": True,
                },
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 1,
                    "events": [
                        {
                            "timestamp": "2026-01-01T00:45:00+00:00",
                            "owner": "gemini",
                            "lane_id": "lane-b",
                            "event_type": "status",
                            "content": "lane recovered from conversation stream",
                        }
                    ],
                    "owner_counts": {"gemini": 1},
                    "partial": True,
                    "ok": False,
                    "errors": ["primary conversation stream degraded"],
                    "sources": [
                        {
                            "kind": "lane",
                            "resolved_kind": "lane_events",
                            "lane_id": "lane-b",
                            "owner": "",
                            "ok": True,
                            "missing": True,
                            "recoverable_missing": True,
                            "fallback_used": True,
                            "error": "",
                            "event_count": 1,
                        }
                    ],
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
            lane_errors = snapshot["lanes"]["errors"]
            self.assertEqual(snapshot["lanes"]["recovered_lane_count"], 1)
            self.assertIn("lane status source unavailable", lane_errors)
            self.assertIn(
                "Lane status missing for 'lane-b'; using conversation-derived fallback.",
                lane_errors,
            )
            self.assertNotIn("Unknown lane id 'lane-b'. Update config/lanes.json.", lane_errors)
            self.assertNotIn(
                "Requested lane 'lane-b' is unavailable because lane status sources failed.",
                lane_errors,
            )

    def test_monitor_snapshot_does_not_recover_lanes_when_lane_source_healthy(self):
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
                    "partial": False,
                },
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 1,
                    "events": [
                        {
                            "timestamp": "2026-01-01T00:45:00+00:00",
                            "owner": "gemini",
                            "lane_id": "lane-b",
                            "event_type": "status",
                            "content": "out-of-band lane event",
                        }
                    ],
                    "owner_counts": {"gemini": 1},
                    "partial": False,
                    "ok": True,
                    "errors": [],
                    "sources": [
                        {
                            "kind": "lane",
                            "resolved_kind": "lane_events",
                            "lane_id": "lane-b",
                            "owner": "gemini",
                            "ok": True,
                            "missing": False,
                            "recoverable_missing": False,
                            "fallback_used": False,
                            "error": "",
                            "event_count": 1,
                        }
                    ],
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
            self.assertEqual(snapshot["lanes"]["total_count"], 1)
            self.assertEqual(snapshot["lanes"]["recovered_lane_count"], 0)
            self.assertEqual(snapshot["lanes"]["lanes"][0]["id"], "lane-a")
            self.assertEqual(snapshot["runtime"]["lane_owner_health"]["codex"]["total"], 1)

    def test_monitor_snapshot_retains_output_when_conversation_snapshot_fails(self):
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
                    "lanes": [{"id": "lane-a", "owner": "codex", "running": True, "health": "ok"}],
                    "health_counts": {"ok": 1},
                    "owner_counts": {"codex": {"total": 1, "running": 1, "healthy": 1, "degraded": 0}},
                },
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                side_effect=RuntimeError("conversation source unavailable"),
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
            self.assertFalse(snapshot["diagnostics"]["sources"]["conversations"]["ok"])
            self.assertFalse(snapshot["conversations"]["ok"])
            self.assertTrue(snapshot["conversations"]["partial"])
            self.assertEqual(snapshot["conversations"]["total_events"], 0)
            self.assertEqual(len(snapshot["conversations"]["sources"]), 1)
            self.assertEqual(snapshot["conversations"]["sources"][0]["kind"], "primary")
            self.assertEqual(snapshot["conversations"]["sources"][0]["path"], str(cfg.conversation_log_file))
            self.assertIn("conversation source unavailable", snapshot["conversations"]["errors"][0])

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

    def test_monitor_snapshot_orders_recent_conversations_by_utc_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"ok": True, "errors": [], "running_count": 0, "total_count": 0, "lanes": []},
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 2,
                    "events": [
                        {"timestamp": "2026-01-01T01:30:00+01:00", "owner": "codex", "content": "earlier"},
                        {"timestamp": "2026-01-01T00:45:00+00:00", "owner": "gemini", "content": "later"},
                    ],
                    "owner_counts": {"codex": 1, "gemini": 1},
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
            self.assertEqual(snapshot["conversations"]["recent_events"][-1]["content"], "later")
            self.assertEqual(snapshot["conversations"]["latest"]["content"], "later")

    def test_monitor_snapshot_handles_invalid_timestamps_and_conversation_source_counters(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"ok": True, "errors": [], "running_count": 0, "total_count": 0, "lanes": []},
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": 4,
                    "events": [
                        {
                            "timestamp": "definitely-invalid-z",
                            "owner": "codex",
                            "lane_id": "lane-a",
                            "content": "invalid-first",
                        },
                        {
                            "timestamp": "2026-01-01T01:30:00+01:00",
                            "owner": "codex",
                            "lane_id": "lane-a",
                            "content": "earlier",
                        },
                        {
                            "timestamp": "2026-01-01T00:45:00+00:00",
                            "owner": "gemini",
                            "lane_id": "lane-a",
                            "content": "later",
                        },
                        {
                            "timestamp": "abc-invalid-a",
                            "owner": "claude",
                            "lane_id": "lane-a",
                            "content": "invalid-last",
                        },
                    ],
                    "owner_counts": {"codex": 2, "gemini": 1, "claude": 1},
                    "partial": True,
                    "ok": False,
                    "errors": ["primary conversation stream degraded"],
                    "sources": [
                        {
                            "kind": "primary",
                            "resolved_kind": "primary",
                            "lane_id": "",
                            "owner": "",
                            "ok": False,
                            "missing": False,
                            "recoverable_missing": False,
                            "fallback_used": False,
                            "error": "primary stream unavailable",
                            "event_count": 0,
                        },
                        {
                            "kind": "lane",
                            "resolved_kind": "lane_events",
                            "lane_id": "lane-a",
                            "owner": "codex",
                            "ok": True,
                            "missing": True,
                            "recoverable_missing": True,
                            "fallback_used": True,
                            "error": "",
                            "event_count": 3,
                        },
                    ],
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
            self.assertEqual(snapshot["conversations"]["recent_events"][-1]["content"], "later")
            self.assertEqual(snapshot["conversations"]["latest"]["content"], "later")
            self.assertEqual(snapshot["conversations"]["source_error_count"], 1)
            self.assertEqual(snapshot["conversations"]["source_missing_count"], 1)
            self.assertEqual(snapshot["conversations"]["source_recoverable_missing_count"], 1)
            self.assertEqual(snapshot["conversations"]["source_fallback_count"], 1)

    def test_monitor_snapshot_normalizes_scalar_errors_and_malformed_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "ok": False,
                    "errors": "lane status source unavailable",
                    "running_count": "not-a-number",
                    "total_count": 0,
                    "lanes": [],
                    "health_counts": {},
                    "owner_counts": {},
                    "partial": True,
                },
            ), mock.patch(
                "orxaq_autonomy.manager.conversations_snapshot",
                return_value={
                    "total_events": "2",
                    "events": [
                        {"timestamp": "2026-01-01T00:00:00+00:00", "owner": "codex", "content": "a"},
                        {"timestamp": "2026-01-01T00:00:01+00:00", "owner": "gemini", "content": "b"},
                    ],
                    "owner_counts": "invalid-owner-counts",
                    "partial": False,
                    "ok": False,
                    "errors": "conversation source unavailable",
                    "sources": "invalid-sources",
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
            self.assertFalse(snapshot["runtime"]["lane_agents_running"])
            self.assertEqual(snapshot["diagnostics"]["sources"]["lanes"]["error"], "lane status source unavailable")
            self.assertEqual(
                snapshot["diagnostics"]["sources"]["conversations"]["error"],
                "conversation source unavailable",
            )
            self.assertEqual(snapshot["conversations"]["errors"], ["conversation source unavailable"])
            self.assertEqual(snapshot["conversations"]["total_events"], 2)
            self.assertEqual(snapshot["conversations"]["owner_counts"], {})
            self.assertEqual(snapshot["conversations"]["sources"], [])
            self.assertEqual(snapshot["conversations"]["source_error_count"], 0)
            self.assertEqual(snapshot["conversations"]["source_missing_count"], 0)
            self.assertEqual(snapshot["conversations"]["source_recoverable_missing_count"], 0)
            self.assertEqual(snapshot["conversations"]["source_fallback_count"], 0)

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

    def test_dashboard_status_snapshot_reconciles_live_http_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.dashboard_meta_file.write_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": 8765,
                        "refresh_sec": 5,
                        "url": "http://127.0.0.1:8765/",
                        "build_id": "stale",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            cfg.dashboard_pid_file.write_text("999\n", encoding="utf-8")

            version_payload = {
                "ok": True,
                "build_id": "test-build",
                "pid": 4321,
                "root_dir": str(cfg.root_dir),
                "started_at": "2026-02-12T00:00:00Z",
                "requested_port": 8765,
                "bound_port": 8765,
                "refresh_sec": 5,
                "signature": "unit-test",
            }
            version_body = (json.dumps(version_payload, sort_keys=True) + "\n").encode("utf-8")

            class FakeResponse:
                def __init__(self, body: bytes, status: int = 200):
                    self.status = status
                    self._body = body

                def read(self, _size: int = -1) -> bytes:
                    return self._body

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            with mock.patch("orxaq_autonomy.manager._dashboard_build_id", return_value="test-build"), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=lambda pid: bool(pid == 4321),
            ), mock.patch(
                "orxaq_autonomy.manager.urlopen",
                return_value=FakeResponse(version_body, status=200),
            ):
                snapshot = manager.dashboard_status_snapshot(cfg)

            self.assertTrue(snapshot["running"])
            self.assertTrue(snapshot.get("running_http", False))
            self.assertEqual(snapshot["pid"], 4321)
            self.assertEqual(cfg.dashboard_pid_file.read_text(encoding="utf-8").strip(), "4321")
            meta = json.loads(cfg.dashboard_meta_file.read_text(encoding="utf-8"))
            self.assertEqual(meta["build_id"], "test-build")
            self.assertEqual(meta["port"], 8765)

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
            popen_args = popen.call_args.args[0]
            self.assertIn("--port-scan", popen_args)
            self.assertIn("0", popen_args)

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
                "orxaq_autonomy.manager._acquire_process_lock",
                return_value=object(),
            ), mock.patch(
                "orxaq_autonomy.manager._release_process_lock"
            ) as release_lock, mock.patch(
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
            release_lock.assert_called_once()

    def test_supervise_foreground_exits_when_lock_held(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch("orxaq_autonomy.manager.ensure_runtime"), mock.patch(
                "orxaq_autonomy.manager._acquire_process_lock",
                return_value=None,
            ), mock.patch(
                "orxaq_autonomy.manager._read_pid",
                return_value=12345,
            ), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                return_value=True,
            ), mock.patch(
                "orxaq_autonomy.manager.subprocess.Popen"
            ) as popen:
                rc = manager.supervise_foreground(cfg)
            self.assertEqual(rc, 0)
            popen.assert_not_called()

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
            self.assertIn("Codex prompt source", startup_packet_text)
            self.assertTrue(
                "codex prompt" in startup_packet_text
                or "technical owner for RouteLLM" in startup_packet_text
            )
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

    def test_conversations_snapshot_orders_events_by_utc_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            cfg.conversation_log_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-01-01T01:30:00+01:00",
                                "owner": "codex",
                                "content": "earlier",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-01-01T00:45:00+00:00",
                                "owner": "codex",
                                "content": "later",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=False)
            self.assertEqual(snapshot["total_events"], 2)
            self.assertEqual(snapshot["events"][-1]["content"], "later")

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
            lane_config_source = next(item for item in snapshot["sources"] if item.get("kind") == "lane_config")
            self.assertFalse(lane_config_source["ok"])
            self.assertEqual(lane_config_source["lane_id"], "lane_config")

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
            lane_config_source = next(
                item
                for item in snapshot["sources"]
                if item.get("kind") == "lane_config" and item.get("lane_id") == "lane-b"
            )
            self.assertEqual(lane_config_source["owner"], "bad-owner")

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
            lane_source = next(item for item in snapshot["sources"] if item.get("lane_id") == "lane-a")
            self.assertTrue(lane_source["ok"])
            self.assertTrue(lane_source["missing"])
            self.assertTrue(lane_source["recoverable_missing"])
            self.assertTrue(lane_source["fallback_used"])
            self.assertEqual(lane_source["resolved_kind"], "lane_events")

    def test_conversations_snapshot_falls_back_when_lane_conversation_read_fails(self):
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
            lane_events = root / "artifacts" / "autonomy" / "lanes" / "lane-a" / "events.ndjson"
            lane_events.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:02+00:00",
                        "lane_id": "lane-a",
                        "event_type": "task_done",
                        "payload": {"task_id": "t1", "status": "done"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            original_tail = manager._tail_ndjson

            def flaky_tail(path: pathlib.Path, limit: int) -> list[dict[str, object]]:
                if pathlib.Path(path).resolve() == lane_conv.resolve():
                    raise OSError("lane file locked")
                return original_tail(path, limit)

            with mock.patch("orxaq_autonomy.manager._tail_ndjson", side_effect=flaky_tail):
                snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=True)
            self.assertEqual(snapshot["total_events"], 2)
            self.assertFalse(snapshot["ok"])
            self.assertTrue(snapshot["partial"])
            self.assertTrue(any("lane file locked" in item for item in snapshot["errors"]))
            lane_source = next(item for item in snapshot["sources"] if item.get("lane_id") == "lane-a")
            self.assertFalse(lane_source["ok"])
            self.assertTrue(lane_source["fallback_used"])
            self.assertEqual(lane_source["resolved_kind"], "lane_events")
            self.assertIn("fallback lane events used", lane_source["error"])
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

    def test_conversations_snapshot_infers_owner_from_lane_id_when_primary_owner_missing(self):
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
            cfg.conversation_log_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01+00:00",
                        "owner": "",
                        "lane_id": "lane-a",
                        "event_type": "agent_output",
                        "content": "main feed update",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=True)
            self.assertEqual(snapshot["total_events"], 1)
            self.assertTrue(snapshot["ok"])
            self.assertFalse(snapshot["partial"])
            self.assertEqual(snapshot["events"][0]["owner"], "gemini")
            self.assertEqual(snapshot["owner_counts"], {"gemini": 1})

    def test_conversations_snapshot_preserves_explicit_owner_when_lane_id_present(self):
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
            cfg.conversation_log_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01+00:00",
                        "owner": "claude",
                        "lane_id": "lane-a",
                        "event_type": "agent_output",
                        "content": "owner preserved",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager.conversations_snapshot(cfg, lines=20, include_lanes=True)
            self.assertEqual(snapshot["total_events"], 1)
            self.assertEqual(snapshot["events"][0]["owner"], "claude")
            self.assertEqual(snapshot["owner_counts"], {"claude": 1})

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
                "orxaq_autonomy.manager._prepare_lane_runtime_repos",
                return_value={
                    "runtime_impl_repo": (root / "impl_repo").resolve(),
                    "runtime_test_repo": (root / "test_repo").resolve(),
                    "worktree_branches": {},
                },
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

    def test_start_lanes_background_applies_npv_scaling_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            env_file.write_text(
                env_file.read_text(encoding="utf-8")
                + "ORXAQ_AUTONOMY_SCALING_ENABLED=1\n",
                encoding="utf-8",
            )
            decision_file = root / "artifacts" / "autonomy" / "scaling_decision.json"
            decision_file.parent.mkdir(parents=True, exist_ok=True)
            decision_file.write_text(
                json.dumps(
                    {
                        "decision": "scale_up",
                        "should_scale": True,
                        "marginal_npv_usd": 2.6,
                        "capacity": {
                            "requested_parallel_agents": 2,
                            "requested_subagents_per_agent": 1,
                        },
                        "constraints": {
                            "projected_daily_spend_usd": 3.1,
                            "daily_budget_usd": 12.0,
                            "max_parallel_agents": 2,
                            "max_subagents_per_agent": 2,
                        },
                    }
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
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                                "scaling_mode": "npv",
                                "scaling_group": "codex-core",
                                "scaling_rank": 1,
                                "scaling_decision_file": str(decision_file),
                                "scaling_max_parallel_agents": 2,
                                "scaling_max_subagents_per_agent": 2,
                            },
                            {
                                "id": "lane-b",
                                "enabled": True,
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                                "scaling_mode": "npv",
                                "scaling_group": "codex-core",
                                "scaling_rank": 2,
                                "scaling_decision_file": str(decision_file),
                                "scaling_max_parallel_agents": 2,
                                "scaling_max_subagents_per_agent": 2,
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
                side_effect=lambda _cfg, lane_id: {"id": lane_id, "pid": 99},
            ) as start:
                payload = manager.start_lanes_background(cfg)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["started_count"], 2)
            self.assertEqual(payload["scaled_up_count"], 1)
            self.assertEqual(payload["scaled_up"][0]["id"], "lane-b")
            self.assertEqual(payload["scaling"]["groups"]["codex-core"]["allowed_parallel_lanes"], 2)
            self.assertEqual(start.call_count, 2)

    def test_start_lanes_background_respects_mesh_scale_down_decision(self):
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
            with mock.patch(
                "orxaq_autonomy.manager._latest_mesh_scaling_decision",
                return_value={"action": "scale_down", "reason": "failures_present", "target_delta": -1},
            ), mock.patch("orxaq_autonomy.manager.start_lane_background") as start:
                payload = manager.start_lanes_background(cfg)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["started_count"], 0)
            self.assertEqual(payload["skipped_count"], 1)
            self.assertIn("mesh_decision_scale_down", payload["skipped"][0]["reason"])
            start.assert_not_called()

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

    def test_ensure_lanes_background_scales_down_when_npv_gate_holds(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            env_file.write_text(
                env_file.read_text(encoding="utf-8")
                + "ORXAQ_AUTONOMY_SCALING_ENABLED=1\n",
                encoding="utf-8",
            )
            decision_file = root / "artifacts" / "autonomy" / "scaling_decision.json"
            decision_file.parent.mkdir(parents=True, exist_ok=True)
            decision_file.write_text(
                json.dumps(
                    {
                        "decision": "hold",
                        "should_scale": False,
                        "marginal_npv_usd": -0.2,
                        "capacity": {
                            "requested_parallel_agents": 2,
                            "requested_subagents_per_agent": 1,
                        },
                        "constraints": {
                            "projected_daily_spend_usd": 1.0,
                            "daily_budget_usd": 20.0,
                            "max_parallel_agents": 2,
                            "max_subagents_per_agent": 2,
                        },
                    }
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
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                                "scaling_mode": "npv",
                                "scaling_group": "codex-core",
                                "scaling_rank": 1,
                                "scaling_decision_file": str(decision_file),
                                "scaling_max_parallel_agents": 2,
                                "scaling_max_subagents_per_agent": 2,
                            },
                            {
                                "id": "lane-b",
                                "enabled": True,
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                                "scaling_mode": "npv",
                                "scaling_group": "codex-core",
                                "scaling_rank": 2,
                                "scaling_decision_file": str(decision_file),
                                "scaling_max_parallel_agents": 2,
                                "scaling_max_subagents_per_agent": 2,
                            },
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {"id": "lane-a", "running": True, "heartbeat_stale": False, "build_current": True},
                        {"id": "lane-b", "running": True, "heartbeat_stale": False, "build_current": True},
                    ]
                },
            ), mock.patch(
                "orxaq_autonomy.manager.stop_lane_background",
                return_value={"id": "lane-b", "running": False},
            ) as stop, mock.patch("orxaq_autonomy.manager.start_lane_background") as start:
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["ensured_count"], 1)
            self.assertEqual(payload["scaled_down_count"], 1)
            self.assertEqual(payload["scaled_down"][0]["id"], "lane-b")
            stop.assert_called_once_with(cfg, "lane-b", reason="scale_down_npv_gate", pause=False)
            start.assert_not_called()

    def test_lane_spec_rebases_legacy_artifact_paths_when_artifacts_dir_overridden(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            overridden_artifacts = root / "runtime-artifacts"
            env_file = root / ".env.autonomy"
            env_file.write_text(
                env_file.read_text(encoding="utf-8")
                + f"ORXAQ_AUTONOMY_ARTIFACTS_DIR={overridden_artifacts}\n",
                encoding="utf-8",
            )
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
                                "artifacts_dir": "artifacts/autonomy/lanes/lane-a",
                                "conversation_log_file": "artifacts/autonomy/lanes/lane-a/conversations.ndjson",
                                "handoff_dir": "artifacts/autonomy/handoffs",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            lane = manager.load_lane_specs(cfg)[0]
            self.assertEqual(lane["artifacts_dir"], (overridden_artifacts / "lanes" / "lane-a").resolve())
            self.assertEqual(
                lane["conversation_log_file"],
                (overridden_artifacts / "lanes" / "lane-a" / "conversations.ndjson").resolve(),
            )
            self.assertEqual(lane["handoff_dir"], (overridden_artifacts / "handoffs").resolve())

    def test_lane_spec_keeps_non_legacy_relative_paths_root_relative(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            overridden_artifacts = root / "runtime-artifacts"
            env_file = root / ".env.autonomy"
            env_file.write_text(
                env_file.read_text(encoding="utf-8")
                + f"ORXAQ_AUTONOMY_ARTIFACTS_DIR={overridden_artifacts}\n",
                encoding="utf-8",
            )
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
                                "handoff_dir": "custom/handoffs",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            lane = manager.load_lane_specs(cfg)[0]
            self.assertEqual(lane["handoff_dir"], (root / "custom" / "handoffs").resolve())

    def test_lane_spec_defaults_queue_paths_under_lane_runtime_dir(self):
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
            lane = manager.load_lane_specs(cfg)[0]
            self.assertEqual(
                lane["task_queue_file"],
                (cfg.lanes_runtime_dir / "lane-a" / "task_queue.ndjson").resolve(),
            )
            self.assertEqual(
                lane["task_queue_state_file"],
                (cfg.lanes_runtime_dir / "lane-a" / "task_queue_claimed.json").resolve(),
            )

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
            self.assertIn("--task-queue-file", argv)
            self.assertIn(str(lane["task_queue_file"]), argv)
            self.assertIn("--task-queue-state-file", argv)
            self.assertIn(str(lane["task_queue_state_file"]), argv)

    def test_build_lane_runner_cmd_uses_lane_command_and_model_overrides(self):
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
                                "codex_cmd": "codex-local",
                                "gemini_cmd": "gemini-local",
                                "claude_cmd": "claude-local",
                                "codex_model": "fast-codex-model",
                                "gemini_model": "gemini-fast",
                                "claude_model": "claude-fast",
                                "gemini_fallback_models": ["gemini-fallback-a", "gemini-fallback-b"],
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
            self.assertIn("--codex-cmd", argv)
            self.assertIn("codex-local", argv)
            self.assertIn("--gemini-cmd", argv)
            self.assertIn("gemini-local", argv)
            self.assertIn("--claude-cmd", argv)
            self.assertIn("claude-local", argv)
            self.assertIn("--codex-model", argv)
            self.assertIn("fast-codex-model", argv)
            self.assertIn("--gemini-model", argv)
            self.assertIn("gemini-fast", argv)
            self.assertIn("--claude-model", argv)
            self.assertIn("claude-fast", argv)
            self.assertEqual(argv.count("--gemini-fallback-model"), 2)
            self.assertIn("gemini-fallback-a", argv)
            self.assertIn("gemini-fallback-b", argv)

    def test_build_lane_spec_includes_lane_env_overrides(self):
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
                                "env": {
                                    "ORXAQ_AUTONOMY_LOCAL_ONLY": "1",
                                    "ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.91:1234/v1",
                                },
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            lane = manager.load_lane_specs(cfg)[0]
            self.assertEqual(
                lane["env"],
                {
                    "ORXAQ_AUTONOMY_LOCAL_ONLY": "1",
                    "ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.91:1234/v1",
                },
            )

    def test_build_lane_spec_extra_high_sets_autonomy_env_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            lane = manager._build_lane_spec(
                cfg,
                {
                    "id": "lane-a",
                    "enabled": True,
                    "owner": "codex",
                    "impl_repo": str(root / "impl_repo"),
                    "test_repo": str(root / "test_repo"),
                    "tasks_file": "config/tasks.json",
                    "objective_file": "config/objective.md",
                    "execution_profile": "extra-high",
                    "max_cycles": 21,
                    "continuous": False,
                },
            )
            assert lane is not None
            self.assertEqual(lane["execution_profile"], "extra_high")
            self.assertTrue(lane["continuous"])
            self.assertGreaterEqual(lane["max_cycles"], manager.EXTRA_HIGH_MIN_MAX_CYCLES)
            self.assertEqual(lane["env"]["ORXAQ_TASK_QUEUE_PERSISTENT_MODE"], "1")
            self.assertEqual(lane["env"]["ORXAQ_AUTONOMY_EXECUTION_PROFILE"], "extra_high")
            self.assertEqual(lane["env"]["ORXAQ_AUTONOMY_ASSUME_TRUE_FULL_AUTONOMY"], "1")

    def test_build_lane_runner_cmd_respects_runtime_repo_and_continuous_overrides(self):
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
                                "max_cycles": 77,
                                "max_attempts": 4,
                                "continuous": True,
                                "continuous_recycle_delay_sec": 123,
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            lane = manager.load_lane_specs(cfg)[0]
            lane_runtime = dict(lane)
            lane_runtime["runtime_impl_repo"] = (root / "runtime_impl").resolve()
            lane_runtime["runtime_test_repo"] = (root / "runtime_test").resolve()

            argv = manager._build_lane_runner_cmd(cfg, lane_runtime)

            impl_idx = argv.index("--impl-repo") + 1
            test_idx = argv.index("--test-repo") + 1
            self.assertEqual(argv[impl_idx], str(lane_runtime["runtime_impl_repo"]))
            self.assertEqual(argv[test_idx], str(lane_runtime["runtime_test_repo"]))
            self.assertIn("--continuous", argv)
            delay_idx = argv.index("--continuous-recycle-delay-sec") + 1
            self.assertEqual(argv[delay_idx], "123")
            cycles_idx = argv.index("--max-cycles") + 1
            attempts_idx = argv.index("--max-attempts") + 1
            self.assertEqual(argv[cycles_idx], "77")
            self.assertEqual(argv[attempts_idx], "4")

    def test_build_lane_runner_cmd_enforces_extra_high_profile(self):
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
                                "execution_profile": "extra-high",
                                "max_cycles": 17,
                                "continuous": False,
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
            profile_idx = argv.index("--execution-profile") + 1
            cycles_idx = argv.index("--max-cycles") + 1
            self.assertEqual(argv[profile_idx], "extra_high")
            self.assertIn("--continuous", argv)
            self.assertGreaterEqual(int(argv[cycles_idx]), manager.EXTRA_HIGH_MIN_MAX_CYCLES)

    def test_build_lane_runner_cmd_uses_lane_routellm_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            lane_policy = root / "config" / "lane_policy.json"
            lane_policy.write_text('{"version":1,"enabled":true}\n', encoding="utf-8")
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
                                "routellm_enabled": True,
                                "routellm_url": "http://127.0.0.1:9100/route",
                                "routellm_timeout_sec": 19,
                                "routellm_policy_file": str(lane_policy),
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

            self.assertIn("--routellm-enabled", argv)
            self.assertIn("--routellm-url", argv)
            self.assertIn("http://127.0.0.1:9100/route", argv)
            timeout_idx = argv.index("--routellm-timeout-sec") + 1
            self.assertEqual(argv[timeout_idx], "19")
            policy_idx = argv.index("--routellm-policy-file") + 1
            self.assertEqual(argv[policy_idx], str(lane_policy.resolve()))

    def test_start_lane_background_validates_owner_cli_with_lane_override(self):
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
                                "codex_cmd": "codex-local",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager._resolve_binary",
                side_effect=lambda cmd: "/usr/local/bin/codex-local" if cmd == "codex-local" else None,
            ) as resolve_binary, mock.patch(
                "orxaq_autonomy.manager._repo_basic_check",
                return_value=(True, "ok"),
            ), mock.patch(
                "orxaq_autonomy.manager.subprocess.Popen",
                return_value=mock.Mock(pid=4242),
            ), mock.patch(
                "orxaq_autonomy.manager._pid_running",
                return_value=True,
            ):
                payload = manager.start_lanes_background(cfg, lane_id="lane-a")
            lane = manager.load_lane_specs(cfg)[0]
            argv = manager._build_lane_runner_cmd(cfg, lane)
            self.assertEqual(payload["started_count"], 1)
            self.assertIn(mock.call("codex-local"), resolve_binary.call_args_list)
            self.assertIn("--handoff-dir", argv)
            handoff_value = argv[argv.index("--handoff-dir") + 1]
            self.assertEqual(handoff_value, str(lane["handoff_dir"]))
            self.assertNotIn("--continuous", argv)
            self.assertNotIn("--continuous-recycle-delay-sec", argv)
            self.assertIn("--gemini-fallback-model", argv)
            self.assertIn("--auto-push-guard", argv)
            self.assertIn("--auto-push-interval-sec", argv)

    def test_start_lane_background_applies_lane_env_overrides(self):
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
                                "env": {
                                    "ORXAQ_AUTONOMY_LOCAL_ONLY": "1",
                                    "ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.91:1234/v1",
                                },
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
                "orxaq_autonomy.manager._prepare_lane_runtime_repos",
                return_value={
                    "runtime_impl_repo": str((root / "impl_repo").resolve()),
                    "runtime_test_repo": str((root / "test_repo").resolve()),
                    "worktree_branches": {},
                },
            ), mock.patch(
                "orxaq_autonomy.manager.subprocess.Popen",
                return_value=mock.Mock(pid=5151),
            ) as popen, mock.patch(
                "orxaq_autonomy.manager._pid_running",
                side_effect=lambda pid: pid == 5151,
            ):
                payload = manager.start_lane_background(cfg, "lane-a")
            self.assertEqual(payload["id"], "lane-a")
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["ORXAQ_AUTONOMY_LOCAL_ONLY"], "1")
            self.assertEqual(env["ORXAQ_LOCAL_OPENAI_BASE_URLS"], "http://192.168.50.91:1234/v1")
            lane_meta = cfg.lanes_runtime_dir / "lane-a" / "lane.json"
            lane_meta_payload = json.loads(lane_meta.read_text(encoding="utf-8"))
            self.assertEqual(lane_meta_payload["execution_profile"], "high")

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

    def test_ensure_lanes_background_targets_single_lane_case_insensitively(self):
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
                payload = manager.ensure_lanes_background(cfg, lane_id="LANE-B")
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
                "orxaq_autonomy.manager._prepare_lane_runtime_repos",
                return_value={
                    "runtime_impl_repo": (root / "impl_repo").resolve(),
                    "runtime_test_repo": (root / "test_repo").resolve(),
                    "worktree_branches": {},
                },
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

    def test_start_lanes_background_targets_single_lane_case_insensitively(self):
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
            with mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                return_value={"id": "lane-a", "pid": 777},
            ) as start:
                payload = manager.start_lanes_background(cfg, lane_id="LANE-A")
            self.assertEqual(payload["requested_lane"], "lane-a")
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["started"][0]["id"], "lane-a")
            start.assert_called_once_with(cfg, "lane-a")

    def test_start_lanes_background_respects_parallel_provider_model_limit(self):
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
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-c",
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
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {"id": "lane-a", "running": False, "owner": "codex", "codex_model": ""},
                        {"id": "lane-b", "running": False, "owner": "codex", "codex_model": ""},
                        {"id": "lane-c", "running": False, "owner": "codex", "codex_model": ""},
                    ]
                },
            ), mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                side_effect=lambda _cfg, lane_name: {"id": lane_name, "pid": 90},
            ) as start:
                payload = manager.start_lanes_background(cfg)
            self.assertEqual(payload["started_count"], 2)
            self.assertEqual(payload["skipped_count"], 1)
            self.assertEqual(payload["skipped"][0]["reason"], "provider_model_parallel_limit")
            self.assertEqual(start.call_count, 2)

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
                                "continuous": True,
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

    def test_ensure_lanes_background_skips_completed_lane_for_non_continuous_mode(self):
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
            self.assertEqual(payload["started_count"], 0)
            self.assertEqual(payload["skipped_count"], 1)
            self.assertEqual(payload["skipped"][0]["reason"], "completed_non_continuous")
            start.assert_not_called()

    def test_ensure_lanes_background_skips_terminal_blocked_lane_for_non_continuous_mode(self):
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
                            "state_counts": {"done": 0, "pending": 0, "in_progress": 0, "blocked": 1},
                            "task_total": 1,
                        }
                    ]
                },
            ):
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["started_count"], 0)
            self.assertEqual(payload["skipped_count"], 1)
            self.assertEqual(payload["skipped"][0]["reason"], "blocked_terminal_non_continuous")
            start.assert_not_called()

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

    def test_ensure_lanes_background_scales_down_parallel_limit_overflow(self):
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
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-c",
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
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {
                            "id": "lane-a",
                            "running": True,
                            "heartbeat_stale": False,
                            "build_current": True,
                            "owner": "codex",
                            "codex_model": "",
                            "latest_log_line": "provider reported rate limit while processing request",
                        },
                        {"id": "lane-b", "running": True, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                        {"id": "lane-c", "running": True, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                    ]
                },
            ), mock.patch(
                "orxaq_autonomy.manager.stop_lane_background",
                return_value={"id": "lane-c", "running": False},
            ) as stop, mock.patch(
                "orxaq_autonomy.manager.start_lane_background"
            ) as start:
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["scaled_down_count"], 1)
            self.assertEqual(payload["scaled_down"][0]["reason"], "provider_model_parallel_limit")
            stop.assert_called_once()
            stop_args, stop_kwargs = stop.call_args
            self.assertEqual(stop_args[0], cfg)
            self.assertIn(stop_args[1], {"lane-a", "lane-b", "lane-c"})
            self.assertEqual(stop_kwargs, {"reason": "scale_down_parallel_limit", "pause": False})
            start.assert_not_called()

    def test_ensure_lanes_background_rebalances_at_limit_with_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            env_file.write_text(
                env_file.read_text(encoding="utf-8")
                + "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_DEFAULT_LIMIT=2\n"
                + "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_MAX_LIMIT=2\n",
                encoding="utf-8",
            )
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
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-c",
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

            class FixedDateTime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    base = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
                    if tz is None:
                        return base.replace(tzinfo=None)
                    return base.astimezone(tz)

            with mock.patch(
                "orxaq_autonomy.manager.dt.datetime",
                FixedDateTime,
            ), mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {"id": "lane-a", "running": False, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                        {"id": "lane-b", "running": True, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                        {"id": "lane-c", "running": True, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                    ]
                },
            ), mock.patch(
                "orxaq_autonomy.manager.stop_lane_background",
                return_value={"id": "lane-c", "running": False},
            ) as stop, mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                return_value={"id": "lane-a", "pid": 991},
            ) as start:
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["scaled_down_count"], 1)
            self.assertEqual(payload["scaled_down"][0]["id"], "lane-c")
            self.assertEqual(payload["scaled_down"][0]["reason"], "rotation_rebalance")
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["started"][0]["id"], "lane-a")
            stop.assert_called_once_with(cfg, "lane-c", reason="scale_down_parallel_limit", pause=False)
            start.assert_called_once_with(cfg, "lane-a")

    def test_start_lanes_background_prefers_rotated_lane_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            env_file = root / ".env.autonomy"
            env_file.write_text(
                env_file.read_text(encoding="utf-8")
                + "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_DEFAULT_LIMIT=2\n"
                + "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_MAX_LIMIT=2\n",
                encoding="utf-8",
            )
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
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-c",
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

            class FixedDateTime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    base = dt.datetime(2026, 1, 1, 0, 1, 0, tzinfo=dt.timezone.utc)
                    if tz is None:
                        return base.replace(tzinfo=None)
                    return base.astimezone(tz)

            with mock.patch(
                "orxaq_autonomy.manager.dt.datetime",
                FixedDateTime,
            ), mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {"id": "lane-a", "running": False, "owner": "codex", "codex_model": ""},
                        {"id": "lane-b", "running": False, "owner": "codex", "codex_model": ""},
                        {"id": "lane-c", "running": False, "owner": "codex", "codex_model": ""},
                    ]
                },
            ), mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                side_effect=lambda _cfg, lane_name: {"id": lane_name, "pid": 90},
            ) as start:
                payload = manager.start_lanes_background(cfg)
            self.assertEqual(payload["started_count"], 2)
            started_ids = {item["id"] for item in payload["started"]}
            self.assertEqual(started_ids, {"lane-b", "lane-c"})
            self.assertEqual(payload["skipped_count"], 1)
            self.assertEqual(payload["skipped"][0]["id"], "lane-a")
            self.assertEqual(payload["skipped"][0]["reason"], "provider_model_parallel_limit")
            self.assertEqual(start.call_count, 2)

    def test_ensure_lanes_background_bootstraps_parallel_limit_without_capacity_signals(self):
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
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-c",
                                "enabled": True,
                                "owner": "codex",
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-d",
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
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {"id": "lane-a", "running": True, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                        {"id": "lane-b", "running": True, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                        {"id": "lane-c", "running": True, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                        {"id": "lane-d", "running": False, "heartbeat_stale": False, "build_current": True, "owner": "codex", "codex_model": ""},
                    ]
                },
            ), mock.patch(
                "orxaq_autonomy.manager.stop_lane_background",
                return_value={"id": "lane-c", "running": False},
            ) as stop, mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                return_value={"id": "lane-d", "pid": 999},
            ) as start:
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["started_count"], 1)
            self.assertEqual(payload["started"][0]["id"], "lane-d")
            self.assertEqual(payload["scaled_down_count"], 0)
            self.assertEqual(payload["skipped_count"], 0)
            stop.assert_not_called()
            start.assert_called_once_with(cfg, "lane-d")

    def test_start_lanes_background_uses_endpoint_aware_parallel_groups(self):
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
                                "codex_model": "qwen/qwen2.5-coder-32b",
                                "env": {"ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.86:1234/v1"},
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-b",
                                "enabled": True,
                                "owner": "codex",
                                "codex_model": "qwen/qwen2.5-coder-32b",
                                "env": {"ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.238:1234/v1"},
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
            (root / ".env").write_text(
                "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_DEFAULT_LIMIT=1\n"
                "ORXAQ_AUTONOMY_PARALLEL_CAPACITY_MAX_LIMIT=4\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {"id": "lane-a", "running": False, "owner": "codex", "codex_model": "qwen/qwen2.5-coder-32b"},
                        {"id": "lane-b", "running": False, "owner": "codex", "codex_model": "qwen/qwen2.5-coder-32b"},
                    ]
                },
            ), mock.patch(
                "orxaq_autonomy.manager.start_lane_background",
                side_effect=lambda _cfg, lane_id: {"id": lane_id, "pid": 2000 if lane_id == "lane-a" else 2001},
            ) as start:
                payload = manager.start_lanes_background(cfg)
            self.assertEqual(payload["started_count"], 2)
            self.assertEqual(payload["skipped_count"], 0)
            self.assertEqual(start.call_count, 2)

    def test_ensure_lanes_background_applies_fleet_endpoint_capacity_limit(self):
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
                                "codex_model": "qwen/qwen2.5-coder-32b",
                                "env": {"ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.86:1234/v1"},
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-b",
                                "enabled": True,
                                "owner": "codex",
                                "codex_model": "qwen/qwen2.5-coder-32b",
                                "env": {"ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.86:1234/v1"},
                                "impl_repo": str(root / "impl_repo"),
                                "test_repo": str(root / "test_repo"),
                                "tasks_file": "config/tasks.json",
                                "objective_file": "config/objective.md",
                            },
                            {
                                "id": "lane-c",
                                "enabled": True,
                                "owner": "codex",
                                "codex_model": "qwen/qwen2.5-coder-32b",
                                "env": {"ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.86:1234/v1"},
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
            fleet_status_file = cfg.artifacts_dir / "local_models" / "fleet_status.json"
            fleet_status_file.parent.mkdir(parents=True, exist_ok=True)
            fleet_status_file.write_text(
                json.dumps(
                    {
                        "probe": {
                            "endpoints": [
                                {
                                    "id": "lan-86",
                                    "base_url": "http://192.168.50.86:1234/v1",
                                    "max_parallel": 4,
                                }
                            ]
                        },
                        "capability_scan": {
                            "summary": {
                                "by_endpoint": {
                                    "lan-86": {
                                        "recommended_parallel": 2,
                                        "max_context_tokens_success": 8192,
                                    }
                                }
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={
                    "lanes": [
                        {"id": "lane-a", "running": True, "heartbeat_stale": False, "build_current": True},
                        {"id": "lane-b", "running": True, "heartbeat_stale": False, "build_current": True},
                        {"id": "lane-c", "running": True, "heartbeat_stale": False, "build_current": True},
                    ]
                },
            ), mock.patch(
                "orxaq_autonomy.manager.stop_lane_background",
                return_value={"id": "lane-c", "running": False},
            ) as stop, mock.patch(
                "orxaq_autonomy.manager.start_lane_background"
            ) as start:
                payload = manager.ensure_lanes_background(cfg)
            self.assertEqual(payload["scaled_down_count"], 1)
            self.assertEqual(payload["scaled_down"][0]["reason"], "provider_model_parallel_limit")
            stop.assert_called_once()
            stop_args, stop_kwargs = stop.call_args
            self.assertEqual(stop_args[0], cfg)
            self.assertIn(stop_args[1], {"lane-a", "lane-b", "lane-c"})
            self.assertEqual(stop_kwargs, {"reason": "scale_down_parallel_limit", "pause": False})
            start.assert_not_called()

    def test_apply_local_model_capacity_env_injects_endpoint_token_controls(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            fleet_status_file = cfg.artifacts_dir / "local_models" / "fleet_status.json"
            fleet_status_file.parent.mkdir(parents=True, exist_ok=True)
            fleet_status_file.write_text(
                json.dumps(
                    {
                        "probe": {
                            "endpoints": [
                                {
                                    "id": "lan-86",
                                    "base_url": "http://192.168.50.86:1234/v1",
                                    "max_parallel": 2,
                                }
                            ]
                        },
                        "capability_scan": {
                            "summary": {
                                "by_endpoint": {
                                    "lan-86": {
                                        "base_url": "http://192.168.50.86:1234/v1",
                                        "recommended_parallel": 2,
                                        "max_context_tokens_success": 4096,
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            lane = {
                "env": {
                    "ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.86:1234/v1",
                    "ORXAQ_LOCAL_OPENAI_CONTEXT_FRACTION": "0.5",
                }
            }
            env = manager._apply_local_model_capacity_env(cfg, lane)
            self.assertEqual(env["ORXAQ_LOCAL_OPENAI_MAX_TOKENS"], "2048")
            self.assertEqual(env["ORXAQ_LOCAL_OPENAI_DYNAMIC_MAX_TOKENS"], "1")
            self.assertIn("192.168.50.86:1234=4096", env["ORXAQ_LOCAL_OPENAI_MAX_TOKENS_BY_ENDPOINT"])
            self.assertEqual(env["ORXAQ_LOCAL_MODEL_FLEET_STATUS_FILE"], str(fleet_status_file.resolve()))

    def test_stop_lanes_background_skips_disabled_lane_when_not_running(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": False,
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
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"lanes": [{"id": "lane-a", "running": False}]},
            ), mock.patch("orxaq_autonomy.manager.stop_lane_background") as stop:
                payload = manager.stop_lanes_background(cfg)
            self.assertEqual(payload["stopped_count"], 0)
            self.assertEqual(payload["skipped_count"], 1)
            self.assertEqual(payload["skipped"][0]["id"], "lane-a")
            self.assertEqual(payload["skipped"][0]["reason"], "disabled")
            self.assertEqual(payload["failed_count"], 0)
            self.assertTrue(payload["ok"])
            stop.assert_not_called()

    def test_stop_lanes_background_targets_single_lane_case_insensitively(self):
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
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"lanes": [{"id": "lane-a", "running": True}]},
            ), mock.patch(
                "orxaq_autonomy.manager.stop_lane_background",
                return_value={"id": "lane-a", "running": False},
            ) as stop:
                payload = manager.stop_lanes_background(cfg, lane_id="LANE-A")
            self.assertEqual(payload["requested_lane"], "lane-a")
            self.assertEqual(payload["stopped_count"], 1)
            stop.assert_called_once_with(cfg, "lane-a", reason="manual", pause=True)

    def test_stop_lanes_background_keeps_valid_lane_when_another_lane_is_invalid(self):
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
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"lanes": [{"id": "lane-a", "running": False}]},
            ), mock.patch(
                "orxaq_autonomy.manager.stop_lane_background",
                return_value={"id": "lane-a", "running": False},
            ) as stop:
                payload = manager.stop_lanes_background(cfg)
            self.assertEqual(payload["stopped_count"], 1)
            self.assertEqual(payload["config_error_count"], 1)
            self.assertEqual(payload["failed_count"], 1)
            self.assertFalse(payload["ok"])
            self.assertIn("lane-b", payload["config_errors"][0])
            self.assertEqual(payload["failed"][0]["source"], "lane_config")
            stop.assert_called_once_with(cfg, "lane-a", reason="manual", pause=True)

    def test_stop_lanes_background_stops_running_disabled_lane_without_pause(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-a",
                                "enabled": False,
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
            with mock.patch(
                "orxaq_autonomy.manager.lane_status_snapshot",
                return_value={"lanes": [{"id": "lane-a", "running": True}]},
            ), mock.patch(
                "orxaq_autonomy.manager.stop_lane_background",
                return_value={"id": "lane-a", "running": False},
            ) as stop:
                payload = manager.stop_lanes_background(cfg)
            self.assertEqual(payload["stopped_count"], 1)
            self.assertEqual(payload["failed_count"], 0)
            self.assertTrue(payload["ok"])
            stop.assert_called_once_with(cfg, "lane-a", reason="manual", pause=False)

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
            self.assertFalse(snapshot["partial"])
            lane = snapshot["lanes"][0]
            self.assertEqual(lane["owner"], "codex")
            self.assertEqual(lane["health"], "stale")
            self.assertTrue(lane["heartbeat_stale"])
            self.assertGreaterEqual(lane["heartbeat_age_sec"], 1)
            self.assertEqual(snapshot["health_counts"]["stale"], 1)
            self.assertEqual(snapshot["owner_counts"]["codex"]["total"], 1)
            self.assertEqual(snapshot["owner_counts"]["codex"]["degraded"], 1)
            self.assertEqual(lane["execution_profile"], "high")

    def test_lane_status_snapshot_marks_disabled_lane_as_paused(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            (root / "config" / "lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "lane-disabled",
                                "enabled": False,
                                "owner": "gemini",
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
            snapshot = manager.lane_status_snapshot(cfg)
            self.assertTrue(snapshot["ok"])
            lane = snapshot["lanes"][0]
            self.assertFalse(lane["running"])
            self.assertEqual(lane["health"], "paused")
            self.assertEqual(snapshot["health_counts"]["paused"], 1)
            self.assertEqual(snapshot["owner_counts"]["gemini"]["healthy"], 1)

    def test_lane_status_snapshot_marks_scale_down_stopped_lane_as_paused(self):
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
            (lane_runtime / "events.ndjson").write_text(
                json.dumps(
                    {
                        "event_type": "stopped",
                        "lane_id": "lane-a",
                        "payload": {"reason": "scale_down_npv_gate"},
                        "timestamp": manager._now_iso(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager.lane_status_snapshot(cfg)
            self.assertTrue(snapshot["ok"])
            lane = snapshot["lanes"][0]
            self.assertFalse(lane["running"])
            self.assertEqual(lane["health"], "paused")
            self.assertEqual(snapshot["health_counts"]["paused"], 1)

    def test_lane_status_snapshot_includes_execution_profile(self):
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
                                "execution_profile": "extra-high",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = manager.ManagerConfig.from_root(root)
            snapshot = manager.lane_status_snapshot(cfg)
            self.assertTrue(snapshot["ok"])
            self.assertEqual(snapshot["lanes"][0]["execution_profile"], "extra_high")

    def test_lane_status_snapshot_includes_scaling_event_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            decision_file = root / "artifacts" / "autonomy" / "scaling_decision.json"
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
                                "scaling_mode": "npv",
                                "scaling_group": "codex-core",
                                "scaling_rank": 2,
                                "scaling_decision_file": str(decision_file),
                                "scaling_min_marginal_npv_usd": 1.0,
                                "scaling_daily_budget_usd": 15.0,
                                "scaling_max_parallel_agents": 3,
                                "scaling_max_subagents_per_agent": 2,
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
            (lane_runtime / "events.ndjson").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-01-01T00:00:01+00:00",
                                "lane_id": "lane-a",
                                "event_type": "scale_up",
                                "payload": {"reason": "approved"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-01-01T00:00:02+00:00",
                                "lane_id": "lane-a",
                                "event_type": "scale_down",
                                "payload": {"reason": "stop_loss_triggered"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = manager.lane_status_snapshot(cfg)
            lane = snapshot["lanes"][0]
            self.assertEqual(lane["scaling_mode"], "npv")
            self.assertEqual(lane["scaling_group"], "codex-core")
            self.assertEqual(lane["scaling_rank"], 2)
            self.assertEqual(lane["scale_up_events"], 1)
            self.assertEqual(lane["scale_down_events"], 1)
            self.assertEqual(lane["latest_scale_event"]["event_type"], "scale_down")
            self.assertEqual(snapshot["scaling_event_counts"]["scale_up"], 1)
            self.assertEqual(snapshot["scaling_event_counts"]["scale_down"], 1)

    def test_lane_status_fallback_snapshot_preserves_lane_owner_identity(self):
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
                                "owner": "gemini",
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
            snapshot = manager.lane_status_fallback_snapshot(cfg, error="lane source unavailable")
            self.assertFalse(snapshot["ok"])
            self.assertTrue(snapshot["partial"])
            self.assertEqual(snapshot["total_count"], 2)
            lane_ids = {lane["id"] for lane in snapshot["lanes"]}
            self.assertEqual(lane_ids, {"lane-a", "lane-b"})
            lane_a = next(lane for lane in snapshot["lanes"] if lane["id"] == "lane-a")
            lane_b = next(lane for lane in snapshot["lanes"] if lane["id"] == "lane-b")
            self.assertEqual(lane_a["owner"], "codex")
            self.assertEqual(lane_b["owner"], "gemini")
            self.assertEqual(lane_a["health"], "unknown")
            self.assertEqual(lane_b["health"], "unknown")
            self.assertEqual(snapshot["owner_counts"]["codex"]["total"], 1)
            self.assertEqual(snapshot["owner_counts"]["gemini"]["total"], 1)
            self.assertIn("lane source unavailable", snapshot["errors"][0])

    def test_lane_status_fallback_snapshot_preserves_owner_for_invalid_lane_config(self):
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
            snapshot = manager.lane_status_fallback_snapshot(cfg, error="lane source unavailable")
            invalid_lane = next(item for item in snapshot["lanes"] if item["id"] == "lane-b")
            self.assertEqual(invalid_lane["owner"], "unsupported-owner")
            self.assertEqual(snapshot["owner_counts"]["unsupported-owner"]["total"], 1)

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
            self.assertEqual(snapshot["total_count"], 2)
            lane_ids = {item["id"] for item in snapshot["lanes"]}
            self.assertIn("lane-a", lane_ids)
            self.assertIn("lane-b", lane_ids)
            invalid_lane = next(item for item in snapshot["lanes"] if item["id"] == "lane-b")
            self.assertEqual(invalid_lane["source"], "lane_config")
            self.assertEqual(invalid_lane["owner"], "unsupported-owner")
            self.assertEqual(invalid_lane["health"], "error")
            self.assertFalse(snapshot["ok"])
            self.assertTrue(snapshot["partial"])
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

    def test_resolve_worktree_base_ref_prefers_configured_ref(self):
        with mock.patch(
            "orxaq_autonomy.manager._git_output",
            side_effect=[(True, "abc123\n")],
        ) as git_output:
            resolved = manager._resolve_worktree_base_ref(pathlib.Path("/tmp/repo"), preferred="origin/main")
        self.assertEqual(resolved, "origin/main")
        git_output.assert_called_once_with(pathlib.Path("/tmp/repo"), ["rev-parse", "--verify", "origin/main"])

    def test_prepare_lane_worktree_checkout_existing_path_switches_without_reset(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            target = pathlib.Path(td) / "worktrees" / "shared"
            repo.mkdir(parents=True, exist_ok=True)
            target.mkdir(parents=True, exist_ok=True)
            expected_branch = manager._lane_worktree_branch_name("lane-a", "shared")
            with mock.patch(
                "orxaq_autonomy.manager._repo_basic_check",
                return_value=(True, "ok"),
            ), mock.patch(
                "orxaq_autonomy.manager._git_output",
                side_effect=[
                    (True, ".git\n"),
                    (True, "abc123\n"),
                    (True, "codex/legacy-branch\n"),
                    (True, "refs/heads/" + expected_branch),
                ],
            ), mock.patch("orxaq_autonomy.manager.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                checkout, branch = manager._prepare_lane_worktree_checkout(
                    repo=repo,
                    lane_id="lane-a",
                    role="shared",
                    worktree_root=pathlib.Path(td) / "worktrees",
                    base_ref="origin/main",
                )
            self.assertEqual(checkout, target.resolve())
            self.assertEqual(branch, expected_branch)
            run.assert_called_once_with(
                ["git", "-C", str(target.resolve()), "checkout", expected_branch],
                capture_output=True,
                text=True,
            )

    def test_prepare_lane_worktree_checkout_new_path_uses_base_ref_in_add_command(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            worktree_root = pathlib.Path(td) / "worktrees"
            expected_branch = manager._lane_worktree_branch_name("lane-a", "shared")
            with mock.patch(
                "orxaq_autonomy.manager._repo_basic_check",
                return_value=(True, "ok"),
            ), mock.patch(
                "orxaq_autonomy.manager._git_output",
                side_effect=[
                    (True, ".git\n"),
                    (True, "abc123\n"),
                    (False, ""),
                ],
            ), mock.patch("orxaq_autonomy.manager.subprocess.run") as run:
                run.side_effect = [
                    mock.Mock(returncode=0, stdout="", stderr=""),
                    mock.Mock(returncode=0, stdout="", stderr=""),
                ]
                checkout, branch = manager._prepare_lane_worktree_checkout(
                    repo=repo,
                    lane_id="lane-a",
                    role="shared",
                    worktree_root=worktree_root,
                    base_ref="origin/main",
                )
            self.assertEqual(checkout, (worktree_root / "shared").resolve())
            self.assertEqual(branch, expected_branch)
            self.assertEqual(run.call_count, 2)
            add_cmd = run.call_args_list[1][0][0]
            self.assertEqual(
                add_cmd,
                [
                    "git",
                    "-C",
                    str(repo.resolve()),
                    "worktree",
                    "add",
                    "--force",
                    "--checkout",
                    "-b",
                    expected_branch,
                    str((worktree_root / "shared").resolve()),
                    "origin/main",
                ],
            )

    def test_has_checkout_overwrite_conflict_detects_commit_or_stash_prompt(self):
        output = (
            "error: some files were changed\n"
            "Please commit your changes or stash them before you switch branches.\n"
            "Aborting\n"
        )
        self.assertTrue(manager._has_checkout_overwrite_conflict(output))

    def test_prepare_lane_worktree_checkout_existing_path_recovers_from_checkout_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            target = pathlib.Path(td) / "worktrees" / "shared"
            repo.mkdir(parents=True, exist_ok=True)
            target.mkdir(parents=True, exist_ok=True)
            worktree_root = pathlib.Path(td) / "worktrees"
            expected_branch = manager._lane_worktree_branch_name("lane-a", "shared")
            expected_recovery_branch = manager._lane_worktree_recovery_branch_name("lane-a", "shared")
            with mock.patch(
                "orxaq_autonomy.manager._repo_basic_check",
                return_value=(True, "ok"),
            ), mock.patch(
                "orxaq_autonomy.manager._git_output",
                side_effect=[
                    (True, ".git\n"),
                    (True, "abc123\n"),
                    (True, "codex/legacy-branch\n"),
                    (True, "refs/heads/" + expected_branch),
                    (False, ""),
                ],
            ), mock.patch("orxaq_autonomy.manager.subprocess.run") as run:
                run.side_effect = [
                    mock.Mock(
                        returncode=1,
                        stdout="",
                        stderr=(
                            "error: Your local changes to the following files would be overwritten by checkout:\n"
                            "\tsrc/orxaq_autonomy/manager.py\n"
                            "Please commit your changes or stash them before you switch branches.\n"
                            "Aborting\n"
                        ),
                    ),
                    mock.Mock(returncode=0, stdout="", stderr=""),
                ]
                checkout, branch = manager._prepare_lane_worktree_checkout(
                    repo=repo,
                    lane_id="lane-a",
                    role="shared",
                    worktree_root=worktree_root,
                    base_ref="origin/main",
                )
            self.assertEqual(checkout, manager._lane_worktree_recovery_target(worktree_root, "shared"))
            self.assertEqual(branch, expected_recovery_branch)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[1][0][0],
                [
                    "git",
                    "-C",
                    str(repo.resolve()),
                    "worktree",
                    "add",
                    "--force",
                    "--checkout",
                    "-b",
                    expected_recovery_branch,
                    str(manager._lane_worktree_recovery_target(worktree_root, "shared")),
                    expected_branch,
                ],
            )

    def test_prepare_lane_worktree_checkout_existing_path_reuses_recovery_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            target = pathlib.Path(td) / "worktrees" / "shared"
            worktree_root = pathlib.Path(td) / "worktrees"
            recovery_target = manager._lane_worktree_recovery_target(worktree_root, "shared")
            repo.mkdir(parents=True, exist_ok=True)
            target.mkdir(parents=True, exist_ok=True)
            recovery_target.mkdir(parents=True, exist_ok=True)
            expected_branch = manager._lane_worktree_branch_name("lane-a", "shared")
            expected_recovery_branch = manager._lane_worktree_recovery_branch_name("lane-a", "shared")
            with mock.patch(
                "orxaq_autonomy.manager._repo_basic_check",
                side_effect=[(True, "ok"), (True, "ok"), (True, "ok")],
            ), mock.patch(
                "orxaq_autonomy.manager._git_output",
                side_effect=[
                    (True, ".git\n"),
                    (True, "abc123\n"),
                    (True, "codex/legacy-branch\n"),
                    (True, "refs/heads/" + expected_branch),
                    (True, expected_recovery_branch + "\n"),
                ],
            ), mock.patch("orxaq_autonomy.manager.subprocess.run") as run:
                run.side_effect = [
                    mock.Mock(
                        returncode=1,
                        stdout="",
                        stderr=(
                            "error: Your local changes to the following files would be overwritten by checkout:\n"
                            "\tsrc/orxaq_autonomy/manager.py\n"
                            "Please commit your changes or stash them before you switch branches.\n"
                            "Aborting\n"
                        ),
                    ),
                ]
                checkout, branch = manager._prepare_lane_worktree_checkout(
                    repo=repo,
                    lane_id="lane-a",
                    role="shared",
                    worktree_root=worktree_root,
                    base_ref="origin/main",
                )
            self.assertEqual(checkout, recovery_target)
            self.assertEqual(branch, expected_recovery_branch)
            self.assertEqual(run.call_count, 1)

    def test_build_lane_spec_defaults_worktree_base_ref(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            lane = manager._build_lane_spec(
                cfg,
                {
                    "id": "lane-a",
                    "enabled": True,
                    "owner": "codex",
                    "impl_repo": str(root / "impl_repo"),
                    "test_repo": str(root / "test_repo"),
                    "tasks_file": "config/tasks.json",
                    "objective_file": "config/objective.md",
                },
            )
            self.assertIsNotNone(lane)
            self.assertEqual(lane["worktree_base_ref"], "origin/main")


if __name__ == "__main__":
    unittest.main()
