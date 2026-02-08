import datetime as dt
import importlib.util
import pathlib
import sys
import unittest


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


if __name__ == "__main__":
    unittest.main()
