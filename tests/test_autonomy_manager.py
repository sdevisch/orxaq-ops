import json
import pathlib
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
        (tmp / "state").mkdir(parents=True, exist_ok=True)
        (tmp / "artifacts" / "autonomy").mkdir(parents=True, exist_ok=True)
        (tmp / "config" / "tasks.json").write_text("[]\n", encoding="utf-8")
        (tmp / "config" / "objective.md").write_text("objective\n", encoding="utf-8")
        (tmp / "config" / "codex_result.schema.json").write_text("{}\n", encoding="utf-8")
        (tmp / "config" / "skill_protocol.json").write_text("{}\n", encoding="utf-8")
        (tmp / ".env.autonomy").write_text("OPENAI_API_KEY=test\nGEMINI_API_KEY=test\n", encoding="utf-8")
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

    def test_runner_argv_contains_skill_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td))
            cfg = manager.ManagerConfig.from_root(root)
            argv = manager.runner_argv(cfg)
            self.assertIn("--skill-protocol-file", argv)
            self.assertIn("--validate-command", argv)
            self.assertIn("--max-runtime-sec", argv)
            self.assertIn("--max-total-tokens", argv)
            self.assertIn("--max-total-cost-usd", argv)
            self.assertIn("--max-total-retries", argv)

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
            with mock.patch("orxaq_autonomy.manager.ensure_runtime"), mock.patch(
                "orxaq_autonomy.manager._repo_is_clean",
                side_effect=[(False, "dirty"), (True, "ok")],
            ):
                payload = manager.preflight(cfg, require_clean=True)
            self.assertFalse(payload["clean"])
            self.assertEqual(len(payload["checks"]), 2)

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


if __name__ == "__main__":
    unittest.main()
