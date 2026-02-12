import datetime as dt
import importlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


def load_runner_module():
    root = pathlib.Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return importlib.import_module("orxaq_autonomy.runner")


runner = load_runner_module()
from orxaq_autonomy.protocols import MCPContextBundle, SkillProtocolSpec


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


class CheckpointTests(unittest.TestCase):
    def test_apply_checkpoint_state_restores_known_tasks(self):
        tasks = [
            runner.Task("a", "codex", 1, "A", "A", [], []),
            runner.Task("b", "gemini", 2, "B", "B", [], []),
        ]
        state = {
            "a": {
                "status": runner.STATUS_PENDING,
                "attempts": 0,
                "retryable_failures": 0,
                "not_before": "",
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
                "owner": "gemini",
            },
        }
        checkpoint_state = {
            "a": {"status": "done", "attempts": 3, "last_summary": "ok"},
            "b": {"status": "in_progress", "attempts": 1},
            "unknown": {"status": "done"},
        }

        runner.apply_checkpoint_state(state, tasks, checkpoint_state)

        self.assertEqual(state["a"]["status"], runner.STATUS_DONE)
        self.assertEqual(state["a"]["attempts"], 3)
        self.assertEqual(state["a"]["last_summary"], "ok")
        # in_progress should be normalized to pending on restore.
        self.assertEqual(state["b"]["status"], runner.STATUS_PENDING)
        self.assertEqual(state["b"]["attempts"], 1)

    def test_resume_missing_checkpoint_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            impl_repo = root / "impl"
            test_repo = root / "test"
            config_dir = root / "config"
            state_dir = root / "state"
            artifacts_dir = root / "artifacts"
            impl_repo.mkdir()
            test_repo.mkdir()
            config_dir.mkdir()
            state_dir.mkdir()
            artifacts_dir.mkdir()

            (impl_repo / ".git").mkdir()
            tasks = [
                {
                    "id": "t1",
                    "owner": "codex",
                    "priority": 1,
                    "title": "T1",
                    "description": "D",
                }
            ]
            (config_dir / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
            (config_dir / "objective.md").write_text("objective", encoding="utf-8")
            (config_dir / "codex_result.schema.json").write_text("{}", encoding="utf-8")
            (config_dir / "skill_protocol.json").write_text("{}", encoding="utf-8")

            argv = [
                "--impl-repo",
                str(impl_repo),
                "--test-repo",
                str(test_repo),
                "--tasks-file",
                str(config_dir / "tasks.json"),
                "--state-file",
                str(state_dir / "state.json"),
                "--objective-file",
                str(config_dir / "objective.md"),
                "--codex-schema",
                str(config_dir / "codex_result.schema.json"),
                "--artifacts-dir",
                str(artifacts_dir),
                "--heartbeat-file",
                str(artifacts_dir / "heartbeat.json"),
                "--lock-file",
                str(artifacts_dir / "runner.lock"),
                "--max-cycles",
                "1",
                "--resume",
                "missing-run-id",
            ]

            with mock.patch.object(runner, "ensure_cli_exists"):
                with self.assertRaises(FileNotFoundError):
                    runner.main(argv)


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

    def test_prompt_includes_skill_protocol_and_mcp_context(self):
        task = runner.Task(
            id="task-1",
            owner="codex",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a1"],
        )
        prompt = runner.build_agent_prompt(
            task=task,
            objective_text="Objective",
            role="implementation-owner",
            repo_path=pathlib.Path("/tmp/repo"),
            retry_context={},
            repo_context="Top file types: py:10.",
            repo_hints=[],
            skill_protocol=SkillProtocolSpec(name="proto", version="2", description="d"),
            mcp_context=MCPContextBundle(source="file", snippets=["ctx"]),
        )
        self.assertIn("Autonomy skill protocol", prompt)
        self.assertIn("proto", prompt)
        self.assertIn("MCP context", prompt)

    def test_run_validations_retries_test_command(self):
        calls = []

        def fake_run_command(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                return runner.subprocess.CompletedProcess(args[0], returncode=1, stdout="", stderr="boom")
            return runner.subprocess.CompletedProcess(args[0], returncode=0, stdout="ok", stderr="")

        with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
            ok, details = runner.run_validations(
                repo=pathlib.Path("/tmp"),
                validate_commands=["make test"],
                timeout_sec=1,
                retries_per_command=1,
            )
        self.assertTrue(ok)
        self.assertEqual(details, "ok")
        self.assertEqual(len(calls), 2)

    def test_run_validations_uses_fallback_when_make_target_missing(self):
        def fake_run_command(cmd, **kwargs):
            first = cmd[0]
            if first == "make":
                return runner.subprocess.CompletedProcess(cmd, returncode=2, stdout="", stderr="No rule to make target")
            return runner.subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

        with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
            ok, details = runner.run_validations(
                repo=pathlib.Path("/tmp"),
                validate_commands=["make lint"],
                timeout_sec=1,
                retries_per_command=0,
            )
        self.assertTrue(ok)
        self.assertEqual(details, "ok")

    def test_extract_usage_metrics_prefers_explicit_values(self):
        outcome = {
            "tokens": 42,
            "cost_usd": 0.75,
            "usage": {"total_tokens": 999, "cost_usd": 99.0},
        }
        tokens, cost = runner.extract_usage_metrics(outcome)
        self.assertEqual(tokens, 42)
        self.assertEqual(cost, 0.75)

    def test_run_command_returns_nonzero_when_binary_missing(self):
        result = runner.run_command(["definitely-not-a-real-binary"], cwd=pathlib.Path("/tmp"), timeout_sec=1)
        self.assertEqual(result.returncode, 127)
        stderr_lower = result.stderr.lower()
        self.assertTrue(
            "no such file" in stderr_lower or "cannot find the file" in stderr_lower,
            msg=f"unexpected missing-binary message: {result.stderr}",
        )

    def test_evaluate_budget_violations_detects_all_caps(self):
        budget = runner.init_budget_state(
            max_runtime_sec=10,
            max_total_tokens=100,
            max_total_cost_usd=1.0,
            max_total_retries=3,
            trace_enabled=False,
        )
        budget["elapsed_sec"] = 15
        budget["totals"]["tokens"] = 120
        budget["totals"]["cost_usd"] = 1.5
        budget["totals"]["retry_events"] = 4
        violations = runner.evaluate_budget_violations(budget)
        self.assertEqual(len(violations), 4)
        self.assertTrue(any("runtime budget exceeded" in item for item in violations))
        self.assertTrue(any("token budget exceeded" in item for item in violations))
        self.assertTrue(any("cost budget exceeded" in item for item in violations))
        self.assertTrue(any("retry budget exceeded" in item for item in violations))


if __name__ == "__main__":
    unittest.main()
