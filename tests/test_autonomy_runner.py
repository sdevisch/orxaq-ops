import datetime as dt
import importlib
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

    def test_select_next_task_uses_external_dependency_state(self):
        task = runner.Task(
            id="task-b",
            owner="gemini",
            priority=1,
            title="B",
            description="Depends on external task",
            depends_on=["task-a"],
            acceptance=[],
        )
        state = {
            "task-b": {
                "status": runner.STATUS_PENDING,
                "attempts": 0,
                "retryable_failures": 0,
                "not_before": "",
                "last_update": "",
                "last_summary": "",
                "last_error": "",
                "owner": "gemini",
            }
        }
        dep_state = {"task-a": {"status": runner.STATUS_DONE}}
        selected = runner.select_next_task([task], state, dependency_state=dep_state)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "task-b")

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

    def test_recover_deadlocked_tasks_reopens_dependency_and_unblocks_blocked(self):
        impl = runner.Task(
            id="impl",
            owner="codex",
            priority=1,
            title="impl",
            description="impl",
            depends_on=[],
            acceptance=[],
        )
        tests = runner.Task(
            id="tests",
            owner="gemini",
            priority=2,
            title="tests",
            description="tests",
            depends_on=["impl"],
            acceptance=[],
        )
        state = {
            "impl": {
                "status": runner.STATUS_DONE,
                "attempts": 1,
                "retryable_failures": 0,
                "deadlock_recoveries": 0,
                "deadlock_reopens": 0,
                "not_before": "",
                "last_update": "",
                "last_summary": "",
                "last_error": "",
                "owner": "codex",
            },
            "tests": {
                "status": runner.STATUS_BLOCKED,
                "attempts": 8,
                "retryable_failures": 0,
                "deadlock_recoveries": 0,
                "deadlock_reopens": 0,
                "not_before": "",
                "last_update": "",
                "last_summary": "",
                "last_error": "failing edge case",
                "owner": "gemini",
            },
        }
        payload = runner.recover_deadlocked_tasks(tasks=[impl, tests], state=state)
        self.assertTrue(payload["changed"])
        self.assertIn("impl", payload["reopened_tasks"])
        self.assertIn("tests", payload["unblocked_tasks"])
        self.assertEqual(state["impl"]["status"], runner.STATUS_PENDING)
        self.assertEqual(state["tests"]["status"], runner.STATUS_PENDING)

    def test_recover_deadlocked_tasks_respects_recovery_limit(self):
        blocked = runner.Task(
            id="blocked",
            owner="gemini",
            priority=1,
            title="blocked",
            description="blocked",
            depends_on=[],
            acceptance=[],
        )
        state = {
            "blocked": {
                "status": runner.STATUS_BLOCKED,
                "attempts": 9,
                "retryable_failures": 0,
                "deadlock_recoveries": 3,
                "deadlock_reopens": 0,
                "not_before": "",
                "last_update": "",
                "last_summary": "",
                "last_error": "persisting issue",
                "owner": "gemini",
            }
        }
        payload = runner.recover_deadlocked_tasks(
            tasks=[blocked],
            state=state,
            max_recoveries_per_task=3,
        )
        self.assertFalse(payload["changed"])
        self.assertEqual(payload["reason"], "recovery_limits_reached")
        self.assertEqual(state["blocked"]["status"], runner.STATUS_BLOCKED)

    def test_recycle_tasks_for_continuous_mode_resets_done_tasks(self):
        task = runner.Task(
            id="task-a",
            owner="codex",
            priority=1,
            title="A",
            description="A",
            depends_on=[],
            acceptance=[],
        )
        state = {
            "task-a": {
                "status": runner.STATUS_DONE,
                "attempts": 4,
                "retryable_failures": 2,
                "not_before": "",
                "last_update": "",
                "last_summary": "done",
                "last_error": "old",
                "owner": "codex",
            }
        }
        runner.recycle_tasks_for_continuous_mode(state, [task], delay_sec=45)
        entry = state["task-a"]
        self.assertEqual(entry["status"], runner.STATUS_PENDING)
        self.assertEqual(entry["attempts"], 0)
        self.assertEqual(entry["retryable_failures"], 0)
        self.assertTrue(entry["not_before"])
        self.assertEqual(entry["last_error"], "")


class RuntimeSafeguardTests(unittest.TestCase):
    def test_build_subprocess_env_sets_non_interactive_defaults(self):
        env = runner.build_subprocess_env()
        self.assertEqual(env["CI"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["PIP_NO_INPUT"], "1")

    def test_run_command_missing_binary_returns_127(self):
        result = runner.run_command(
            ["missing_command_for_orxaq_tests_12345"],
            cwd=pathlib.Path("/tmp"),
            timeout_sec=1,
        )
        self.assertEqual(result.returncode, 127)
        self.assertIn("command not found", result.stderr)

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
        self.assertIn("Scope boundary: complete only the current autonomous task listed above.", prompt)
        self.assertIn("Do not start another task in this run", prompt)

    def test_prompt_includes_startup_instructions(self):
        task = runner.Task(
            id="task-2",
            owner="gemini",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a1"],
        )
        prompt = runner.build_agent_prompt(
            task=task,
            objective_text="Objective",
            role="test-owner",
            repo_path=pathlib.Path("/tmp/repo"),
            retry_context={},
            repo_context="Top file types: py:10.",
            repo_hints=[],
            skill_protocol=SkillProtocolSpec(name="proto", version="2", description="d"),
            mcp_context=None,
            startup_instructions="Always include adversarial tests.",
        )
        self.assertIn("Role startup instructions", prompt)
        self.assertIn("adversarial tests", prompt)

    def test_record_handoff_event_and_render_context(self):
        with tempfile.TemporaryDirectory() as td:
            handoff_dir = pathlib.Path(td)
            task = runner.Task(
                id="impl-1",
                owner="codex",
                priority=1,
                title="impl",
                description="impl",
                depends_on=[],
                acceptance=[],
            )
            runner.record_handoff_event(
                handoff_dir=handoff_dir,
                task=task,
                outcome={
                    "status": "done",
                    "summary": "Implemented retention fix",
                    "blocker": "",
                    "next_actions": ["Add adversarial test for contradictory facts."],
                    "commit": "abc123",
                },
            )
            text = runner.render_handoff_context(handoff_dir, "gemini")
            self.assertIn("Recent implementation handoffs for testing", text)
            self.assertIn("impl-1", text)
            self.assertIn("contradictory facts", text)

    def test_handoff_round_trip_between_codex_and_gemini(self):
        with tempfile.TemporaryDirectory() as td:
            handoff_dir = pathlib.Path(td)
            impl_task = runner.Task(
                id="impl-task",
                owner="codex",
                priority=1,
                title="impl",
                description="impl",
                depends_on=[],
                acceptance=[],
            )
            test_task = runner.Task(
                id="test-task",
                owner="gemini",
                priority=1,
                title="test",
                description="test",
                depends_on=[],
                acceptance=[],
            )
            runner.record_handoff_event(
                handoff_dir=handoff_dir,
                task=impl_task,
                outcome={
                    "status": "done",
                    "summary": "Changed merge logic",
                    "blocker": "",
                    "next_actions": ["Add regression for small-context detail loss."],
                    "commit": "abc123",
                },
            )
            gemini_context = runner.render_handoff_context(handoff_dir, "gemini")
            self.assertIn("impl-task", gemini_context)
            self.assertIn("small-context", gemini_context)

            runner.record_handoff_event(
                handoff_dir=handoff_dir,
                task=test_task,
                outcome={
                    "status": "blocked",
                    "summary": "Found failing edge test",
                    "blocker": "Compaction dropped contradictory facts in merge stage.",
                    "next_actions": ["Preserve contradictory fact markers in recursive merge."],
                    "commit": "",
                },
            )
            codex_context = runner.render_handoff_context(handoff_dir, "codex")
            self.assertIn("test-task", codex_context)
            self.assertIn("contradictory facts", codex_context)

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

    def test_ensure_repo_pushed_returns_synced_when_not_ahead(self):
        with mock.patch.object(
            runner,
            "_git_output",
            side_effect=[
                (True, "true"),
                (True, "origin/main"),
                (True, "0 0"),
            ],
        ):
            ok, details = runner.ensure_repo_pushed(pathlib.Path("/tmp/repo"))
        self.assertTrue(ok)
        self.assertIn("ahead=0", details)

    def test_ensure_repo_pushed_blocks_when_behind(self):
        with mock.patch.object(
            runner,
            "_git_output",
            side_effect=[
                (True, "true"),
                (True, "origin/main"),
                (True, "0 3"),
            ],
        ), mock.patch.object(runner, "run_command") as run_command:
            ok, details = runner.ensure_repo_pushed(pathlib.Path("/tmp/repo"))
        self.assertFalse(ok)
        self.assertIn("behind upstream", details)
        run_command.assert_not_called()

    def test_ensure_repo_pushed_blocks_when_diverged(self):
        with mock.patch.object(
            runner,
            "_git_output",
            side_effect=[
                (True, "true"),
                (True, "origin/main"),
                (True, "2 1"),
            ],
        ), mock.patch.object(runner, "run_command") as run_command:
            ok, details = runner.ensure_repo_pushed(pathlib.Path("/tmp/repo"))
        self.assertFalse(ok)
        self.assertIn("diverged", details)
        run_command.assert_not_called()

    def test_ensure_repo_pushed_pushes_when_ahead(self):
        with mock.patch.object(
            runner,
            "_git_output",
            side_effect=[
                (True, "true"),
                (True, "origin/main"),
                (True, "2 0"),
                (True, "0 0"),
            ],
        ), mock.patch.object(
            runner,
            "run_command",
            return_value=runner.subprocess.CompletedProcess(["git", "push"], returncode=0, stdout="", stderr=""),
        ) as run_command:
            ok, details = runner.ensure_repo_pushed(pathlib.Path("/tmp/repo"))
        self.assertTrue(ok)
        run_command.assert_called_once()
        self.assertIn("push verified", details)

    def test_prompt_difficulty_score_increases_with_complexity(self):
        simple = "Implement one helper."
        complex_prompt = (
            "Maybe add recursive merge support, maybe keep details, and include tests?\n"
            "- Support retries\n"
            "- Add validation\n"
            "- Document assumptions\n"
            "If possible, include benchmarks and edge cases."
        )
        self.assertLess(runner.prompt_difficulty_score(simple), runner.prompt_difficulty_score(complex_prompt))

    def test_extract_usage_metrics_reads_payload_and_output(self):
        payload_usage = runner.extract_usage_metrics(
            payload={"usage": {"input_tokens": 101, "output_tokens": 52, "total_tokens": 153}}
        )
        self.assertEqual(payload_usage["source"], "payload")
        self.assertEqual(payload_usage["input_tokens"], 101)
        self.assertEqual(payload_usage["output_tokens"], 52)
        self.assertEqual(payload_usage["total_tokens"], 153)

        output_usage = runner.extract_usage_metrics(
            payload={},
            stdout='{"input_tokens": 11, "output_tokens": 5}',
        )
        self.assertEqual(output_usage["source"], "command_output")
        self.assertEqual(output_usage["total_tokens"], 16)

    def test_compute_response_cost_exact_and_estimated(self):
        pricing = {
            "models": {
                "codex": {"input_per_million": 10.0, "output_per_million": 20.0},
            }
        }
        exact = runner.compute_response_cost(
            pricing=pricing,
            owner="codex",
            model="codex",
            usage={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
            prompt_tokens_est=0,
            response_tokens_est=0,
        )
        self.assertAlmostEqual(exact["cost_usd"], 0.02, places=8)
        self.assertTrue(exact["cost_exact"])
        self.assertEqual(exact["cost_source"], "exact_usage")

        estimated = runner.compute_response_cost(
            pricing=pricing,
            owner="codex",
            model="codex",
            usage={"source": "none"},
            prompt_tokens_est=1000,
            response_tokens_est=500,
        )
        self.assertAlmostEqual(estimated["cost_usd"], 0.02, places=8)
        self.assertFalse(estimated["cost_exact"])
        self.assertEqual(estimated["cost_source"], "estimated_tokens")

    def test_compute_response_cost_unpriced_still_reports_token_flow(self):
        pricing = {"models": {"codex": {"input_per_million": 0.0, "output_per_million": 0.0}}}
        payload = runner.compute_response_cost(
            pricing=pricing,
            owner="codex",
            model="codex",
            usage={"source": "none"},
            prompt_tokens_est=321,
            response_tokens_est=123,
        )
        self.assertIsNone(payload["cost_usd"])
        self.assertEqual(payload["input_tokens"], 321)
        self.assertEqual(payload["output_tokens"], 123)
        self.assertEqual(payload["total_tokens"], 444)
        self.assertEqual(payload["cost_source"], "unpriced_model_estimated_tokens")

    def test_update_response_metrics_summary_tracks_prompt_difficulty_and_recommendations(self):
        with tempfile.TemporaryDirectory() as td:
            summary_path = pathlib.Path(td) / "summary.json"
            first = runner.update_response_metrics_summary(
                summary_path,
                {
                    "owner": "gemini",
                    "quality_score": 0.2,
                    "latency_sec": 200.0,
                    "prompt_difficulty_score": 70,
                    "first_time_pass": False,
                    "validation_passed": False,
                    "cost_exact": False,
                    "cost_usd": 0.1,
                    "token_count_exact": False,
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                },
            )
            self.assertEqual(first["responses_total"], 1)
            self.assertAlmostEqual(first["prompt_difficulty_score_avg"], 70.0, places=6)
            self.assertEqual(first["tokens_total"], 150)
            self.assertAlmostEqual(first["token_rate_per_minute"], 45.0, places=6)
            self.assertGreater(len(first["optimization_recommendations"]), 0)
            self.assertIn("gemini", first["by_owner"])

            second = runner.update_response_metrics_summary(
                summary_path,
                {
                    "owner": "gemini",
                    "quality_score": 1.0,
                    "latency_sec": 10.0,
                    "prompt_difficulty_score": 20,
                    "first_time_pass": True,
                    "validation_passed": True,
                    "cost_exact": True,
                    "cost_usd": 0.01,
                    "token_count_exact": True,
                    "input_tokens": 40,
                    "output_tokens": 10,
                    "total_tokens": 50,
                },
            )
            self.assertEqual(second["responses_total"], 2)
            self.assertAlmostEqual(second["prompt_difficulty_score_avg"], 45.0, places=6)
            self.assertEqual(second["tokens_total"], 200)
            self.assertAlmostEqual(second["token_exact_coverage"], 0.5, places=6)
            self.assertIn("quality_score_avg", second["by_owner"]["gemini"])
            self.assertIn("latency_sec_avg", second["by_owner"]["gemini"])
            self.assertIn("prompt_difficulty_score_avg", second["by_owner"]["gemini"])
            self.assertIn("tokens_avg", second["by_owner"]["gemini"])

    def test_load_tasks_supports_claude_owner(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "tasks.json"
            path.write_text(
                '[{"id":"t1","owner":"claude","priority":1,"title":"t","description":"d","depends_on":[],"acceptance":[]}]',
                encoding="utf-8",
            )
            tasks = runner.load_tasks(path)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].owner, "claude")

    def test_append_conversation_event_writes_ndjson(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "conversations.ndjson"
            task = runner.Task(
                id="t",
                owner="codex",
                priority=1,
                title="Title",
                description="Desc",
                depends_on=[],
                acceptance=[],
            )
            runner.append_conversation_event(
                path,
                cycle=3,
                task=task,
                owner="codex",
                event_type="agent_output",
                content="hello world",
                meta={"agent": "codex"},
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = runner.json.loads(lines[0])
            self.assertEqual(payload["task_id"], "t")
            self.assertEqual(payload["owner"], "codex")
            self.assertEqual(payload["event_type"], "agent_output")

    def test_run_claude_task_parses_json_stdout(self):
        task = runner.Task(
            id="task-c",
            owner="claude",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            with mock.patch.object(
                runner,
                "run_command",
                return_value=runner.subprocess.CompletedProcess(
                    ["claude"],
                    returncode=0,
                    stdout='{"status":"done","summary":"ok","commit":"","validations":[],"next_actions":[],"blocker":""}',
                    stderr="",
                ),
            ):
                ok, outcome = runner.run_claude_task(
                    task=task,
                    repo=repo,
                    objective_text="obj",
                    claude_cmd="claude",
                    claude_model=None,
                    timeout_sec=5,
                    retry_context={},
                    progress_callback=None,
                    repo_context="Top file types: py:1.",
                    repo_hints=[],
                    skill_protocol=SkillProtocolSpec(name="proto", version="1", description="d"),
                    mcp_context=None,
                    startup_instructions="",
                    handoff_context="",
                    conversation_log_file=None,
                    cycle=1,
                )
        self.assertTrue(ok)
        self.assertEqual(outcome["status"], "done")


if __name__ == "__main__":
    unittest.main()
