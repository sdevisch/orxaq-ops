import datetime as dt
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


def load_runner_module():
    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "autonomy_runner.py"
    spec = importlib.util.spec_from_file_location("autonomy_runner", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load autonomy_runner module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = load_runner_module()


class ParseJsonTextTests(unittest.TestCase):
    def test_parse_plain_json(self):
        payload = runner.parse_json_text('{"status":"done","summary":"ok"}')
        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "done")

    def test_parse_markdown_fenced_json(self):
        raw = """Result:\n```json\n{\"status\":\"partial\",\"summary\":\"continue\"}\n```"""
        payload = runner.parse_json_text(raw)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "partial")

    def test_parse_embedded_json(self):
        raw = "noise before {\"status\":\"blocked\",\"summary\":\"x\"} noise after"
        payload = runner.parse_json_text(raw)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "blocked")


class RetryClassificationTests(unittest.TestCase):
    def test_retryable_error_true(self):
        self.assertTrue(runner.is_retryable_error("HTTP 429 Too Many Requests"))
        self.assertTrue(runner.is_retryable_error("network timeout while calling model"))
        self.assertTrue(runner.is_retryable_error("Unable to create .git/index.lock"))

    def test_retryable_error_false(self):
        self.assertFalse(runner.is_retryable_error("assertion failed in unit test"))


class SchedulingTests(unittest.TestCase):
    def test_select_next_task_skips_cooldown(self):
        now = dt.datetime.now(dt.timezone.utc)
        t1 = runner.Task(
            id="a",
            owner="codex",
            priority=1,
            title="A",
            description="A",
            depends_on=[],
            acceptance=[],
        )
        t2 = runner.Task(
            id="b",
            owner="codex",
            priority=2,
            title="B",
            description="B",
            depends_on=[],
            acceptance=[],
        )

        state = {
            "a": {
                "status": runner.STATUS_PENDING,
                "attempts": 1,
                "retryable_failures": 0,
                "not_before": (now + dt.timedelta(minutes=5)).isoformat(),
                "last_update": "",
                "last_summary": "",
                "last_error": "",
                "owner": "codex",
            },
            "b": {
                "status": runner.STATUS_PENDING,
                "attempts": 0,
                "retryable_failures": 0,
                "not_before": "",
                "last_update": "",
                "last_summary": "",
                "last_error": "",
                "owner": "codex",
            },
        }

        selected = runner.select_next_task([t1, t2], state, now=now)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "b")

    def test_schedule_retry_sets_not_before_and_pending(self):
        entry = {
            "status": runner.STATUS_IN_PROGRESS,
            "attempts": 3,
            "retryable_failures": 0,
            "not_before": "",
            "last_update": "",
            "last_summary": "",
            "last_error": "",
            "owner": "codex",
        }

        delay = runner.schedule_retry(
            entry=entry,
            summary="temporary failure",
            error="timeout",
            retryable=True,
            backoff_base_sec=5,
            backoff_max_sec=60,
        )
        self.assertEqual(delay, 5)
        self.assertEqual(entry["status"], runner.STATUS_PENDING)
        self.assertGreater(entry["retryable_failures"], 0)
        self.assertTrue(entry["not_before"])


class RuntimeSafeguardTests(unittest.TestCase):
    def test_build_subprocess_env_sets_non_interactive_defaults(self):
        env = runner.build_subprocess_env()
        self.assertEqual(env["CI"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["PIP_NO_INPUT"], "1")

    def test_validation_fallback_commands_for_make_targets(self):
        self.assertGreater(len(runner.validation_fallback_commands("make test")), 0)
        self.assertGreater(len(runner.validation_fallback_commands("make lint")), 0)
        self.assertEqual(runner.validation_fallback_commands("echo ok"), [])

    def test_heal_stale_git_locks_removes_old_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            git_dir = repo / ".git"
            git_dir.mkdir(parents=True, exist_ok=True)
            lock_file = git_dir / "index.lock"
            lock_file.write_text("stale", encoding="utf-8")
            old = dt.datetime.now(dt.timezone.utc).timestamp() - 600
            os.utime(lock_file, (old, old))

            with mock.patch.object(runner, "has_running_git_processes", return_value=False):
                removed = runner.heal_stale_git_locks(repo, stale_after_sec=300)

            self.assertIn(lock_file, removed)
            self.assertFalse(lock_file.exists())


if __name__ == "__main__":
    unittest.main()
