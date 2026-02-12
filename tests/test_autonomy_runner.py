import datetime as dt
import importlib
import json
import os
import pathlib
import time
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

    def test_codex_model_selection_error_true(self):
        self.assertTrue(
            runner.is_codex_model_selection_error(
                "The 'gpt-5-mini' model is not supported when using Codex with a ChatGPT account."
            )
        )
        self.assertTrue(runner.is_codex_model_selection_error("invalid model requested"))

    def test_codex_model_selection_error_false(self):
        self.assertFalse(runner.is_codex_model_selection_error("network timeout while calling model"))


class ExecutionProfilePolicyTests(unittest.TestCase):
    def test_normalize_execution_profile_aliases(self):
        self.assertEqual(runner.normalize_execution_profile("extra-high"), "extra_high")
        self.assertEqual(runner.normalize_execution_profile("xhigh"), "extra_high")
        self.assertEqual(runner.normalize_execution_profile("high"), "high")
        self.assertEqual(runner.normalize_execution_profile(None), "standard")
        self.assertEqual(runner.normalize_execution_profile("unknown"), "standard")

    def test_resolve_execution_policy_keeps_requested_flags_for_high(self):
        policy = runner.resolve_execution_policy(
            execution_profile="high",
            continuous_requested=False,
            queue_persistent_mode_requested=True,
            max_cycles_requested=33,
        )
        self.assertEqual(policy["execution_profile"], "high")
        self.assertFalse(policy["continuous"])
        self.assertTrue(policy["queue_persistent_mode"])
        self.assertEqual(policy["effective_max_cycles"], 33)
        self.assertFalse(policy["force_continuation"])
        self.assertFalse(policy["assume_true_full_autonomy"])

    def test_resolve_execution_policy_forces_extra_high_continuation(self):
        policy = runner.resolve_execution_policy(
            execution_profile="extra-high",
            continuous_requested=False,
            queue_persistent_mode_requested=False,
            max_cycles_requested=5,
        )
        self.assertEqual(policy["execution_profile"], "extra_high")
        self.assertTrue(policy["continuous"])
        self.assertTrue(policy["queue_persistent_mode"])
        self.assertTrue(policy["force_continuation"])
        self.assertTrue(policy["assume_true_full_autonomy"])
        self.assertEqual(policy["effective_max_cycles"], runner.EXTRA_HIGH_MIN_MAX_CYCLES)


class RoutedModelEdgeCaseTests(unittest.TestCase):
    def _policy(self):
        return {
            "enabled": True,
            "router": {"url": "http://router.invalid", "timeout_sec": 2},
            "providers": {"codex": {"enabled": True, "fallback_model": "gpt-4o-mini"}},
        }

    def test_resolve_routed_model_router_non_object_payload_falls_back(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"[]"

            def getcode(self):
                return 200

        with mock.patch.object(runner.urllib.request, "urlopen", return_value=_FakeResponse()):
            selected, decision = runner.resolve_routed_model(
                provider="codex",
                requested_model=None,
                prompt="Implement helper.",
                routellm_enabled=True,
                routellm_policy=self._policy(),
            )
        self.assertEqual(selected, "gpt-4o-mini")
        self.assertEqual(decision["reason"], "router_unavailable")
        self.assertIn("router_response_not_object", decision["router_error"])

    def test_resolve_routed_model_router_missing_model_falls_back(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"status":"ok"}'

            def getcode(self):
                return 200

        with mock.patch.object(runner.urllib.request, "urlopen", return_value=_FakeResponse()):
            selected, decision = runner.resolve_routed_model(
                provider="codex",
                requested_model=None,
                prompt="Implement helper.",
                routellm_enabled=True,
                routellm_policy=self._policy(),
            )
        self.assertEqual(selected, "gpt-4o-mini")
        self.assertEqual(decision["reason"], "router_unavailable")
        self.assertIn("router_response_missing_model", decision["router_error"])

    def test_resolve_routed_model_router_http_error_falls_back(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"selected_model":"gpt-5-mini"}'

            def getcode(self):
                return 503

        with mock.patch.object(runner.urllib.request, "urlopen", return_value=_FakeResponse()):
            selected, decision = runner.resolve_routed_model(
                provider="codex",
                requested_model=None,
                prompt="Implement helper.",
                routellm_enabled=True,
                routellm_policy=self._policy(),
            )
        self.assertEqual(selected, "gpt-4o-mini")
        self.assertEqual(decision["reason"], "router_unavailable")
        self.assertIn("router_http_503", decision["router_error"])


class LocalOpenAIEndpointStateTests(unittest.TestCase):
    def setUp(self):
        self._old_failure_state = dict(runner._LOCAL_OPENAI_ENDPOINT_FAILURE_STATE)
        self._old_inflight = dict(runner._LOCAL_OPENAI_ENDPOINT_INFLIGHT)
        runner._LOCAL_OPENAI_ENDPOINT_FAILURE_STATE.clear()
        runner._LOCAL_OPENAI_ENDPOINT_INFLIGHT.clear()

    def tearDown(self):
        runner._LOCAL_OPENAI_ENDPOINT_FAILURE_STATE.clear()
        runner._LOCAL_OPENAI_ENDPOINT_FAILURE_STATE.update(self._old_failure_state)
        runner._LOCAL_OPENAI_ENDPOINT_INFLIGHT.clear()
        runner._LOCAL_OPENAI_ENDPOINT_INFLIGHT.update(self._old_inflight)

    def test_local_openai_endpoint_is_cooled_down_true(self):
        endpoint = "http://127.0.0.1:1234/v1"
        key = runner._local_openai_endpoint_key(endpoint)
        runner._LOCAL_OPENAI_ENDPOINT_FAILURE_STATE[key] = {"cooldown_until": 101.0}
        with mock.patch.object(runner.time, "monotonic", return_value=100.0):
            self.assertTrue(runner._local_openai_endpoint_is_cooled_down(endpoint))

    def test_local_openai_inflight_enter_exit_cycle(self):
        endpoint = "http://127.0.0.1:1234/v1"
        runner._local_openai_endpoint_inflight_enter(endpoint)
        self.assertEqual(runner._local_openai_endpoint_inflight_count(endpoint), 1)
        runner._local_openai_endpoint_inflight_enter(endpoint)
        self.assertEqual(runner._local_openai_endpoint_inflight_count(endpoint), 2)
        runner._local_openai_endpoint_inflight_exit(endpoint)
        self.assertEqual(runner._local_openai_endpoint_inflight_count(endpoint), 1)
        runner._local_openai_endpoint_inflight_exit(endpoint)
        self.assertEqual(runner._local_openai_endpoint_inflight_count(endpoint), 0)


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

    def test_select_next_task_prefers_non_backlog_when_ready(self):
        now = dt.datetime.now(dt.timezone.utc)
        backlog = runner.Task(
            id="codex-backlog-sweep",
            owner="codex",
            priority=1,
            title="Backlog Sweep",
            description="housekeeping backlog work",
            depends_on=[],
            acceptance=[],
            backlog=True,
        )
        live = runner.Task(
            id="codex-live-fix",
            owner="codex",
            priority=2,
            title="Live Fix",
            description="user requested issue",
            depends_on=[],
            acceptance=[],
            backlog=False,
        )
        state = {
            backlog.id: {
                "status": runner.STATUS_PENDING,
                "attempts": 0,
                "retryable_failures": 0,
                "not_before": "",
                "last_update": "",
                "last_summary": "",
                "last_error": "",
                "owner": "codex",
            },
            live.id: {
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
        selected = runner.select_next_task([backlog, live], state, now=now)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, live.id)

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

    def test_schedule_retry_sanitizes_large_error_payload(self):
        entry = {
            "status": runner.STATUS_IN_PROGRESS,
            "attempts": 1,
            "retryable_failures": 0,
            "not_before": "",
            "last_update": "",
            "last_summary": "",
            "last_error": "",
            "owner": "codex",
        }
        noisy_error = (
            "ERROR start\n"
            + ("line\n" * 100)
            + "Current autonomous task:\n"
            + ("very long echoed prompt\n" * 200)
        )
        delay = runner.schedule_retry(
            entry=entry,
            summary="temporary failure",
            error=noisy_error,
            retryable=True,
            backoff_base_sec=5,
            backoff_max_sec=60,
        )
        self.assertEqual(delay, 5)
        self.assertIn("omitted echoed prompt", entry["last_error"])
        self.assertLessEqual(len(entry["last_error"]), runner.MAX_STORED_ERROR_CHARS + 32)

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

    def test_recycle_stalled_tasks_for_continuous_mode_reopens_blocked(self):
        task = runner.Task(
            id="task-b",
            owner="claude",
            priority=1,
            title="B",
            description="B",
            depends_on=[],
            acceptance=[],
        )
        state = {
            "task-b": {
                "status": runner.STATUS_BLOCKED,
                "attempts": 8,
                "retryable_failures": 3,
                "not_before": "",
                "last_update": "",
                "last_summary": "blocked",
                "last_error": "permission deadlock",
                "owner": "claude",
            }
        }
        reopened = runner.recycle_stalled_tasks_for_continuous_mode(state, [task], delay_sec=60)
        self.assertEqual(reopened, ["task-b"])
        entry = state["task-b"]
        self.assertEqual(entry["status"], runner.STATUS_PENDING)
        self.assertEqual(entry["attempts"], 0)
        self.assertEqual(entry["retryable_failures"], 0)
        self.assertTrue(entry["not_before"])
        self.assertIn("reopened blocked task", entry["last_error"])


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

    def test_run_command_timeout_kills_process_tree(self):
        cmd = [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
                "time.sleep(30)"
            ),
        ]
        started = time.monotonic()
        result = runner.run_command(
            cmd,
            cwd=pathlib.Path("/tmp"),
            timeout_sec=1,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 124)
        self.assertLess(elapsed, 10)
        self.assertIn("[TIMEOUT] command exceeded 1s", result.stderr)

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
        self.assertIn("Issue-first workflow", prompt)
        self.assertIn("issue-linked branch (`codex/issue-<id>-<topic>`)", prompt)
        self.assertIn("only mention again if the file set changes or conflicts appear", prompt)
        self.assertIn("Merge/rebase operations are allowed when there are no unresolved conflicts", prompt)
        self.assertIn("review-owner (Codex preferred)", prompt)

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

    def test_prompt_uses_filesystem_baseline_when_repo_is_not_worktree(self):
        task = runner.Task(
            id="task-non-worktree",
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
            mcp_context=None,
            repo_is_worktree=False,
        )
        self.assertIn("non-worktree mode", prompt)
        self.assertIn("Record one filesystem baseline before edits", prompt)
        self.assertNotIn("Record one baseline before edits: `git status -sb`", prompt)

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

    def test_run_validations_parses_env_prefixed_command(self):
        calls = []

        def fake_run_command(cmd, **kwargs):
            calls.append((cmd, kwargs.get("extra_env")))
            return runner.subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

        with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
            ok, details = runner.run_validations(
                repo=pathlib.Path("/tmp"),
                validate_commands=["PYTHONPATH=packages FOO=bar python3 -m pytest tests -q"],
                timeout_sec=1,
                retries_per_command=0,
            )
        self.assertTrue(ok)
        self.assertEqual(details, "ok")
        self.assertEqual(len(calls), 1)
        cmd, extra_env = calls[0]
        self.assertEqual(cmd, ["python3", "-m", "pytest", "tests", "-q"])
        self.assertEqual(extra_env, {"PYTHONPATH": "packages", "FOO": "bar"})

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
                (True, "codex/autonomy-orxaq"),
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

    def test_resolve_routed_model_disabled_uses_static_model(self):
        selected, decision = runner.resolve_routed_model(
            provider="codex",
            requested_model="gpt-4o-mini",
            prompt="Implement helper.",
            routellm_enabled=False,
            routellm_policy={"enabled": True, "router": {"url": "http://router.invalid"}},
        )
        self.assertEqual(selected, "gpt-4o-mini")
        self.assertEqual(decision["strategy"], "static_fallback")
        self.assertEqual(decision["reason"], "router_disabled")
        self.assertFalse(decision["fallback_used"])

    def test_resolve_routed_model_router_failure_falls_back_deterministically(self):
        policy = {
            "enabled": True,
            "router": {"url": "http://router.invalid", "timeout_sec": 2},
            "providers": {"codex": {"enabled": True, "fallback_model": "gpt-4o-mini"}},
        }
        with mock.patch.object(
            runner.urllib.request,
            "urlopen",
            side_effect=runner.urllib.error.URLError("connection refused"),
        ):
            selected, decision = runner.resolve_routed_model(
                provider="codex",
                requested_model=None,
                prompt="Implement helper.",
                routellm_enabled=True,
                routellm_policy=policy,
            )
        self.assertEqual(selected, "gpt-4o-mini")
        self.assertEqual(decision["selected_model"], "gpt-4o-mini")
        self.assertEqual(decision["strategy"], "static_fallback")
        self.assertTrue(decision["fallback_used"])
        self.assertEqual(decision["reason"], "router_unavailable")

    def test_resolve_routed_model_uses_router_selected_model(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"selected_model":"gpt-5-mini"}'

            def getcode(self):
                return 200

        policy = {
            "enabled": True,
            "router": {"url": "http://router.invalid", "timeout_sec": 2},
            "providers": {"codex": {"enabled": True, "allowed_models": ["gpt-5-mini", "gpt-4o-mini"]}},
        }
        with mock.patch.object(runner.urllib.request, "urlopen", return_value=_FakeResponse()):
            selected, decision = runner.resolve_routed_model(
                provider="codex",
                requested_model="gpt-4o-mini",
                prompt="Implement helper.",
                routellm_enabled=True,
                routellm_policy=policy,
            )
        self.assertEqual(selected, "gpt-5-mini")
        self.assertEqual(decision["strategy"], "routellm")
        self.assertEqual(decision["reason"], "router_selected_model")
        self.assertFalse(decision["fallback_used"])

    def test_resolve_routed_model_enforces_allowed_fallback_when_requested_model_disallowed(self):
        policy = {
            "enabled": True,
            "router": {"url": "http://router.invalid", "timeout_sec": 2},
            "providers": {
                "codex": {
                    "enabled": True,
                    "fallback_model": "gpt-5-mini",
                    "allowed_models": ["gpt-5-mini", "gpt-4o-mini"],
                }
            },
        }
        selected, decision = runner.resolve_routed_model(
            provider="codex",
            requested_model="not-allowed-model",
            prompt="Implement helper.",
            routellm_enabled=False,
            routellm_policy=policy,
        )
        self.assertEqual(selected, "gpt-5-mini")
        self.assertEqual(decision["selected_model"], "gpt-5-mini")
        self.assertFalse(decision["requested_model_allowed"])
        self.assertEqual(decision["allowed_models"], ["gpt-5-mini", "gpt-4o-mini"])

    def test_resolve_routed_model_disallowed_router_selection_uses_deterministic_allowed_fallback(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"selected_model":"rogue-model"}'

            def getcode(self):
                return 200

        policy = {
            "enabled": True,
            "router": {"url": "http://router.invalid", "timeout_sec": 2},
            "providers": {
                "codex": {
                    "enabled": True,
                    "fallback_model": "outside-policy-model",
                    "allowed_models": ["gpt-5-mini", "gpt-4o-mini"],
                }
            },
        }
        with mock.patch.object(runner.urllib.request, "urlopen", return_value=_FakeResponse()):
            selected, decision = runner.resolve_routed_model(
                provider="codex",
                requested_model=None,
                prompt="Implement helper.",
                routellm_enabled=True,
                routellm_policy=policy,
            )
        self.assertEqual(selected, "gpt-5-mini")
        self.assertEqual(decision["strategy"], "static_fallback")
        self.assertTrue(decision["fallback_used"])
        self.assertEqual(decision["reason"], "router_model_disallowed_fallback")
        self.assertEqual(decision["router_error"], "")
        self.assertIn("router_model_not_allowed:rogue-model", decision["router_notice"])

    def test_codex_model_candidates_local_only_prefers_explicit_requested_model(self):
        route_decision = {
            "requested_model": "qwen/qwen2.5-coder-32b",
            "requested_model_allowed": True,
            "selected_model": "deepseek-coder-v2-lite-instruct",
            "fallback_model": "deepseek-coder-v2-lite-instruct",
            "allowed_models": [
                "deepseek-coder-v2-lite-instruct",
                "qwen/qwen2.5-coder-32b",
            ],
        }
        with mock.patch.dict(runner.os.environ, {}, clear=False):
            models = runner.codex_model_candidates(
                "deepseek-coder-v2-lite-instruct",
                route_decision,
                local_only_mode=True,
            )
        self.assertEqual(models, ["qwen/qwen2.5-coder-32b"])

    def test_codex_model_candidates_local_only_can_disable_requested_model_strictness(self):
        route_decision = {
            "requested_model": "qwen/qwen2.5-coder-32b",
            "requested_model_allowed": True,
            "fallback_model": "deepseek-coder-v2-lite-instruct",
            "policy_fallback_model": "google/gemma-3-4b",
            "allowed_models": [
                "deepseek-coder-v2-lite-instruct",
                "qwen/qwen2.5-coder-32b",
            ],
        }
        with mock.patch.dict(runner.os.environ, {"ORXAQ_LOCAL_OPENAI_STRICT_REQUESTED_MODEL": "0"}, clear=False):
            models = runner.codex_model_candidates(
                "deepseek-coder-v2-lite-instruct",
                route_decision,
                local_only_mode=True,
            )
        self.assertEqual(
            models,
            [
                "deepseek-coder-v2-lite-instruct",
                "google/gemma-3-4b",
                "qwen/qwen2.5-coder-32b",
            ],
        )

    def test_codex_model_candidates_filters_incompatible_hosted_models(self):
        route_decision = {
            "requested_model": "",
            "requested_model_allowed": False,
            "fallback_model": "qwen/qwen2.5-coder-32b",
            "policy_fallback_model": "deepseek-coder-v2-lite-instruct",
            "allowed_models": [
                "qwen/qwen2.5-coder-32b",
                "deepseek-coder-v2-lite-instruct",
                "gpt-5-codex",
            ],
        }
        with mock.patch.dict(runner.os.environ, {"ORXAQ_AUTONOMY_CODEX_FILTER_INCOMPAT_MODELS": "1"}, clear=False):
            models = runner.codex_model_candidates(
                "qwen/qwen2.5-coder-32b",
                route_decision,
                local_only_mode=False,
            )
        self.assertIn("gpt-5-codex", models)
        self.assertIn("gpt-5.3-codex", models)
        self.assertNotIn("qwen/qwen2.5-coder-32b", models)
        self.assertNotIn("deepseek-coder-v2-lite-instruct", models)

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
            self.assertEqual(first["estimated_tokens_total"], 150)
            self.assertAlmostEqual(first["token_rate_per_minute"], 45.0, places=6)
            self.assertAlmostEqual(first["estimated_cost_per_million_tokens"], 666.666667, places=6)
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
            self.assertIn("cost_per_million_tokens", second["by_owner"]["gemini"])

    def test_update_response_metrics_summary_tracks_routing_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            summary_path = pathlib.Path(td) / "summary.json"
            summary = runner.update_response_metrics_summary(
                summary_path,
                {
                    "owner": "codex",
                    "quality_score": 0.8,
                    "latency_sec": 10.0,
                    "prompt_difficulty_score": 30,
                    "first_time_pass": True,
                    "validation_passed": True,
                    "cost_exact": True,
                    "cost_usd": 0.02,
                    "token_count_exact": True,
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "routing_provider": "codex",
                    "routing_strategy": "routellm",
                    "routing_requested_model": "gpt-4o-mini",
                    "routing_selected_model": "gpt-5-mini",
                    "routing_fallback_used": False,
                    "routing_reason": "router_selected_model",
                    "routing_router_error": "",
                    "routing_router_latency_sec": 0.25,
                },
            )
        self.assertEqual(summary["routing_decisions_total"], 1)
        self.assertEqual(summary["routing_routellm_count"], 1)
        self.assertEqual(summary["routing_fallback_count"], 0)
        self.assertEqual(summary["routing_router_error_count"], 0)
        self.assertIn("codex", summary["routing_by_provider"])
        self.assertEqual(summary["routing_by_provider"]["codex"]["routellm_count"], 1)
        self.assertEqual(summary["routing_by_provider"]["codex"]["tokens_total"], 150)
        self.assertAlmostEqual(summary["routing_by_provider"]["codex"]["cost_per_million_tokens"], 133.333333, places=6)

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

    def test_load_tasks_normalizes_legacy_single_task_payload(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "tasks.json"
            path.write_text(
                runner.json.dumps(
                    {
                        "task": "claude-autonomy-stability-audit",
                        "description": "Audit resilience controls.",
                        "validation_steps": ["Verify retries", "Review logging"],
                    }
                ),
                encoding="utf-8",
            )
            tasks = runner.load_tasks(path)
            self.assertEqual(len(tasks), 1)
            task = tasks[0]
            self.assertEqual(task.id, "claude-autonomy-stability-audit")
            self.assertEqual(task.owner, "claude")
            self.assertEqual(task.priority, 1)
            self.assertEqual(task.acceptance, ["Verify retries", "Review logging"])

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


class AgentExecutionHardeningTests(unittest.TestCase):
    def test_run_codex_task_retries_with_fallback_model_after_unsupported_model(self):
        task = runner.Task(
            id="task-codex-fallback",
            owner="codex",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )
        calls: list[list[str]] = []

        def fake_run_command(cmd, **kwargs):  # noqa: ANN001
            calls.append(list(cmd))
            output_path: pathlib.Path | None = None
            if "--output-last-message" in cmd:
                output_path = pathlib.Path(cmd[cmd.index("--output-last-message") + 1])
            if len(calls) == 1:
                return runner.subprocess.CompletedProcess(
                    cmd,
                    returncode=1,
                    stdout="",
                    stderr=(
                        "ERROR: {\"detail\":\"The 'gpt-5-mini' model is not supported when using Codex "
                        "with a ChatGPT account.\"}"
                    ),
                )
            if output_path is not None:
                output_path.write_text(
                    '{"status":"done","summary":"ok","commit":"","validations":[],"next_actions":[],"blocker":""}',
                    encoding="utf-8",
                )
            return runner.subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            schema_path = repo / "schema.json"
            schema_path.write_text("{}", encoding="utf-8")
            output_dir = repo / "artifacts"
            with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
                ok, outcome = runner.run_codex_task(
                    task=task,
                    repo=repo,
                    objective_text="obj",
                    schema_path=schema_path,
                    output_dir=output_dir,
                    codex_cmd="codex",
                    codex_model="gpt-5-mini",
                    routellm_enabled=False,
                    routellm_policy={
                        "enabled": True,
                        "providers": {
                            "codex": {
                                "enabled": True,
                                "fallback_model": "gpt-5.3-codex",
                                "allowed_models": ["gpt-5.3-codex", "gpt-5-mini"],
                            }
                        },
                    },
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
        self.assertEqual(len(calls), 2)
        self.assertIn("--model", calls[0])
        self.assertIn("gpt-5-mini", calls[0])
        self.assertIn("--model", calls[1])
        self.assertIn("gpt-5.3-codex", calls[1])
        telemetry = outcome.get("_telemetry", {})
        self.assertEqual(telemetry.get("model"), "gpt-5.3-codex")
        routing = telemetry.get("routing", {})
        self.assertTrue(routing.get("fallback_used"))
        self.assertEqual(routing.get("reason"), "unsupported_model_fallback")

    def test_run_codex_task_retries_with_fallback_model_after_timeout(self):
        task = runner.Task(
            id="task-codex-timeout-fallback",
            owner="codex",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )
        calls: list[list[str]] = []

        def fake_run_command(cmd, **kwargs):  # noqa: ANN001
            calls.append(list(cmd))
            output_path: pathlib.Path | None = None
            if "--output-last-message" in cmd:
                output_path = pathlib.Path(cmd[cmd.index("--output-last-message") + 1])
            if len(calls) == 1:
                return runner.subprocess.CompletedProcess(
                    cmd,
                    returncode=124,
                    stdout="",
                    stderr="[TIMEOUT] command exceeded 30s",
                )
            if output_path is not None:
                output_path.write_text(
                    '{"status":"done","summary":"ok","commit":"","validations":[],"next_actions":[],"blocker":""}',
                    encoding="utf-8",
                )
            return runner.subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            schema_path = repo / "schema.json"
            schema_path.write_text("{}", encoding="utf-8")
            output_dir = repo / "artifacts"
            with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
                ok, outcome = runner.run_codex_task(
                    task=task,
                    repo=repo,
                    objective_text="obj",
                    schema_path=schema_path,
                    output_dir=output_dir,
                    codex_cmd="codex",
                    codex_model="gpt-5.3-codex",
                    routellm_enabled=False,
                    routellm_policy={
                        "enabled": True,
                        "providers": {
                            "codex": {
                                "enabled": True,
                                "fallback_model": "gpt-5-codex",
                                "allowed_models": ["gpt-5.3-codex", "gpt-5-codex"],
                            }
                        },
                    },
                    timeout_sec=30,
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
        self.assertEqual(len(calls), 2)
        self.assertIn("gpt-5.3-codex", calls[0])
        self.assertIn("gpt-5-codex", calls[1])
        telemetry = outcome.get("_telemetry", {})
        routing = telemetry.get("routing", {})
        self.assertTrue(routing.get("fallback_used"))
        self.assertEqual(routing.get("reason"), "timeout_fallback")

    def test_run_gemini_task_uses_fallback_model_after_capacity_error(self):
        task = runner.Task(
            id="task-g",
            owner="gemini",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )
        calls: list[list[str]] = []

        def fake_run_command(cmd, **kwargs):  # noqa: ANN001
            calls.append(list(cmd))
            if len(calls) == 1:
                return runner.subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="503 model overloaded")
            return runner.subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout='{"status":"done","summary":"ok","commit":"","validations":[],"next_actions":[],"blocker":""}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
                ok, outcome = runner.run_gemini_task(
                    task=task,
                    repo=repo,
                    objective_text="obj",
                    gemini_cmd="gemini",
                    gemini_model=None,
                    gemini_fallback_models=["gemini-2.5-flash"],
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
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--model", calls[0])
        self.assertIn("--model", calls[1])
        self.assertIn("gemini-2.5-flash", calls[1])

    def test_run_gemini_task_falls_back_to_claude_provider(self):
        task = runner.Task(
            id="task-gf-claude",
            owner="gemini",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )

        def gemini_failure(cmd, **kwargs):  # noqa: ANN001
            return runner.subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="429 too many requests")

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            with mock.patch.object(runner, "run_command", side_effect=gemini_failure), mock.patch.object(
                runner,
                "run_claude_task",
                return_value=(
                    True,
                    {
                        "status": "done",
                        "summary": "claude fallback outcome",
                        "commit": "",
                        "validations": [],
                        "next_actions": [],
                        "blocker": "",
                    },
                ),
            ) as run_claude_task, mock.patch.object(runner, "run_codex_task") as run_codex_task:
                ok, outcome = runner.run_gemini_task(
                    task=task,
                    repo=repo,
                    objective_text="obj",
                    gemini_cmd="gemini",
                    gemini_model=None,
                    gemini_fallback_models=[],
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
                    claude_fallback_cmd="claude",
                    claude_fallback_model=None,
                )
        self.assertTrue(ok)
        self.assertIn("Claude fallback succeeded after Gemini failure.", outcome["summary"])
        run_claude_task.assert_called_once()
        run_codex_task.assert_not_called()

    def test_run_gemini_task_prefers_codex_fallback_before_claude(self):
        task = runner.Task(
            id="task-gf-codex",
            owner="gemini",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )

        def gemini_failure(cmd, **kwargs):  # noqa: ANN001
            return runner.subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="429 too many requests")

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            schema_path = repo / "schema.json"
            schema_path.write_text("{}", encoding="utf-8")
            output_dir = repo / "artifacts"
            with mock.patch.object(runner, "run_command", side_effect=gemini_failure), mock.patch.object(
                runner,
                "run_claude_task",
                return_value=(
                    False,
                    {
                        "status": "blocked",
                        "summary": "claude failed",
                        "commit": "",
                        "validations": [],
                        "next_actions": [],
                        "blocker": "claude blocked",
                    },
                ),
            ) as run_claude_task, mock.patch.object(
                runner,
                "run_codex_task",
                return_value=(
                    True,
                    {
                        "status": "done",
                        "summary": "openai fallback outcome",
                        "commit": "",
                        "validations": [],
                        "next_actions": [],
                        "blocker": "",
                    },
                ),
            ) as run_codex_task:
                ok, outcome = runner.run_gemini_task(
                    task=task,
                    repo=repo,
                    objective_text="obj",
                    gemini_cmd="gemini",
                    gemini_model=None,
                    gemini_fallback_models=[],
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
                    claude_fallback_cmd="claude",
                    codex_fallback_cmd="codex",
                    codex_schema_path=schema_path,
                    codex_output_dir=output_dir,
                )
        self.assertTrue(ok)
        self.assertIn("Codex fallback succeeded after Gemini failure.", outcome["summary"])
        run_claude_task.assert_not_called()
        run_codex_task.assert_called_once()

    def test_run_gemini_task_falls_back_after_capacity_partial_output(self):
        task = runner.Task(
            id="task-gf-capacity-partial",
            owner="gemini",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )

        partial_capacity_output = (
            '{"status":"partial","summary":"Rate limit reached, retrying later.",'
            '"commit":"","validations":[],"next_actions":[],"blocker":""}'
        )

        def gemini_partial(cmd, **kwargs):  # noqa: ANN001
            return runner.subprocess.CompletedProcess(cmd, returncode=0, stdout=partial_capacity_output, stderr="")

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            with mock.patch.object(runner, "run_command", side_effect=gemini_partial), mock.patch.object(
                runner,
                "run_claude_task",
                return_value=(
                    True,
                    {
                        "status": "done",
                        "summary": "claude recovered",
                        "commit": "",
                        "validations": [],
                        "next_actions": [],
                        "blocker": "",
                    },
                ),
            ) as run_claude_task, mock.patch.object(runner, "run_codex_task") as run_codex_task:
                ok, outcome = runner.run_gemini_task(
                    task=task,
                    repo=repo,
                    objective_text="obj",
                    gemini_cmd="gemini",
                    gemini_model=None,
                    gemini_fallback_models=[],
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
                    claude_fallback_cmd="claude",
                    claude_fallback_model=None,
                )
        self.assertTrue(ok)
        self.assertIn("Claude fallback succeeded after Gemini failure.", outcome["summary"])
        run_claude_task.assert_called_once()
        run_codex_task.assert_not_called()

    def test_run_claude_task_uses_least_privilege_flags_by_default(self):
        task = runner.Task(
            id="task-c",
            owner="claude",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )
        captured: list[tuple[list[str], dict[str, object]]] = []

        def fake_run_command(cmd, **kwargs):  # noqa: ANN001
            captured.append((list(cmd), dict(kwargs)))
            return runner.subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout='{"status":"done","summary":"ok","commit":"","validations":[],"next_actions":[],"blocker":""}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
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
        self.assertEqual(len(captured), 1)
        cmd, kwargs = captured[0]
        self.assertNotIn("-p", cmd)
        self.assertIn("--print", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertNotIn("bypassPermissions", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertIsInstance(kwargs.get("stdin_text"), str)
        self.assertIn("Current autonomous task:", str(kwargs.get("stdin_text", "")))

    def test_run_claude_task_uses_breakglass_flags_when_grant_is_active(self):
        task = runner.Task(
            id="task-c-breakglass",
            owner="claude",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )
        captured: list[list[str]] = []
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        grant = {
            "grant_id": "bg-test-001",
            "reason": "provider outage mitigation",
            "scope": "claude-only",
            "requested_by": "codex",
            "approved_by": "operator",
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + dt.timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "rollback_proof": "rollback-plan-001",
            "providers": ["claude"],
        }
        privilege_policy = {
            "providers": {
                "claude": {
                    "least_privilege_args": ["--permission-mode", "acceptEdits"],
                    "elevated_args": ["--permission-mode", "bypassPermissions", "--dangerously-skip-permissions"],
                }
            },
            "breakglass": {
                "enabled": True,
                "required_fields": [
                    "grant_id",
                    "reason",
                    "scope",
                    "requested_by",
                    "approved_by",
                    "issued_at",
                    "expires_at",
                    "rollback_proof",
                    "providers",
                ],
                "max_ttl_minutes": 120,
            },
        }

        def fake_run_command(cmd, **kwargs):  # noqa: ANN001
            captured.append(list(cmd))
            return runner.subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout='{"status":"done","summary":"ok","commit":"","validations":[],"next_actions":[],"blocker":""}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            grant_file = repo / "active_grant.json"
            audit_file = repo / "privilege.ndjson"
            grant_file.write_text(json.dumps(grant) + "\n", encoding="utf-8")
            with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
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
                    privilege_policy=privilege_policy,
                    privilege_breakglass_file=grant_file,
                    privilege_audit_log=audit_file,
                )
            audit_rows = [
                json.loads(line)
                for line in audit_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        self.assertTrue(ok)
        self.assertEqual(outcome["status"], "done")
        self.assertEqual(len(captured), 1)
        self.assertIn("bypassPermissions", captured[0])
        self.assertIn("--dangerously-skip-permissions", captured[0])
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(audit_rows[0]["mode"], "breakglass_elevated")

    def test_run_claude_task_retries_with_prompt_argument_when_stdin_not_detected(self):
        task = runner.Task(
            id="task-c-retry",
            owner="claude",
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a"],
        )
        captured: list[tuple[list[str], dict[str, object]]] = []

        def fake_run_command(cmd, **kwargs):  # noqa: ANN001
            captured.append((list(cmd), dict(kwargs)))
            if len(captured) == 1:
                return runner.subprocess.CompletedProcess(
                    cmd,
                    returncode=1,
                    stdout="",
                    stderr="Error: Input must be provided either through stdin or as a prompt argument when using --print",
                )
            return runner.subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout='{"status":"done","summary":"ok","commit":"","validations":[],"next_actions":[],"blocker":""}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            with mock.patch.object(runner, "run_command", side_effect=fake_run_command):
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
        self.assertEqual(len(captured), 2)
        first_cmd, first_kwargs = captured[0]
        second_cmd, second_kwargs = captured[1]
        self.assertIsInstance(first_kwargs.get("stdin_text"), str)
        self.assertGreater(len(second_cmd), len(first_cmd))
        self.assertNotIn("stdin_text", second_kwargs)

    def test_auto_push_repo_if_ahead_pushes(self):
        with mock.patch.object(
            runner,
            "_git_output",
            side_effect=[
                (True, "true"),
                (True, "origin/main"),
                (True, "2 0"),
                (True, "codex/gemini-orxaq"),
            ],
        ), mock.patch.object(
            runner,
            "run_command",
            return_value=runner.subprocess.CompletedProcess(["git", "push"], returncode=0, stdout="", stderr=""),
        ) as run_command:
            status, details = runner.auto_push_repo_if_ahead(pathlib.Path("/tmp/repo"))
        self.assertEqual(status, "pushed")
        self.assertIn("auto-pushed 2 commit", details)
        run_command.assert_called_once()

    def test_auto_push_repo_if_ahead_switches_from_protected_branch(self):
        with mock.patch.object(
            runner,
            "_git_output",
            side_effect=[
                (True, "true"),
                (True, "origin/main"),
                (True, "1 0"),
                (True, "main"),
                (True, "main"),
                (True, "codex/gemini-orxaq"),
            ],
        ), mock.patch.object(
            runner,
            "run_command",
            side_effect=[
                runner.subprocess.CompletedProcess(
                    ["git", "push"],
                    returncode=1,
                    stdout="",
                    stderr="GH013: Changes must be made through a pull request.",
                ),
                runner.subprocess.CompletedProcess(["git", "checkout", "codex/gemini-orxaq"], returncode=1, stdout="", stderr=""),
                runner.subprocess.CompletedProcess(["git", "checkout", "-b", "codex/gemini-orxaq"], returncode=0, stdout="", stderr=""),
                runner.subprocess.CompletedProcess(
                    ["git", "push", "-u", "origin", "codex/gemini-orxaq"], returncode=0, stdout="", stderr=""
                ),
            ],
        ):
            status, details = runner.auto_push_repo_if_ahead(pathlib.Path("/tmp/repo"), owner="gemini")
        self.assertEqual(status, "pushed")
        self.assertIn("codex/gemini-orxaq", details)

    def test_push_with_recovery_updates_moved_remote_and_retries(self):
        repo = pathlib.Path("/tmp/repo")
        with mock.patch.object(
            runner,
            "_git_output",
            return_value=(True, "codex/autonomy-orxaq"),
        ), mock.patch.object(
            runner,
            "run_command",
            side_effect=[
                runner.subprocess.CompletedProcess(
                    ["git", "push"],
                    returncode=1,
                    stdout="",
                    stderr="remote: This repository moved. Please use the new location: https://github.com/Orxaq/orxaq.git",
                ),
                runner.subprocess.CompletedProcess(
                    ["git", "remote", "set-url", "origin", "https://github.com/Orxaq/orxaq.git"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
                runner.subprocess.CompletedProcess(["git", "push"], returncode=0, stdout="", stderr=""),
            ],
        ):
            ok, details = runner._push_with_recovery(repo, timeout_sec=10, owner="codex")
        self.assertTrue(ok)
        self.assertIn("updating origin", details)

    def test_push_with_recovery_rotates_branch_on_agent_branch_reuse_violation(self):
        repo = pathlib.Path("/tmp/repo")
        fixed_now = dt.datetime(2026, 2, 12, 0, 0, 0, tzinfo=dt.timezone.utc)
        branch = "codex/issue-16-dashboard-ui-accessibility"
        rotated = "codex/issue-16-dashboard-ui-accessibility-recovery-20260212T000000Z"
        with mock.patch.object(
            runner,
            "_now_utc",
            return_value=fixed_now,
        ), mock.patch.object(
            runner,
            "_git_output",
            return_value=(True, branch),
        ), mock.patch.object(
            runner,
            "run_command",
            side_effect=[
                runner.subprocess.CompletedProcess(
                    ["git", "push"],
                    returncode=1,
                    stdout="",
                    stderr="Agent branch reuse detected across sessions.",
                ),
                runner.subprocess.CompletedProcess(
                    ["git", "checkout", "-b", rotated],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
                runner.subprocess.CompletedProcess(
                    ["git", "push", "-u", "origin", rotated],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ],
        ) as run_command:
            ok, details = runner._push_with_recovery(repo, timeout_sec=10, owner="codex")
        self.assertTrue(ok)
        self.assertIn("rotated agent branch", details)
        self.assertIn(rotated, details)
        self.assertEqual(run_command.call_count, 3)

    def test_push_with_recovery_handles_protected_branch_on_no_verify_path(self):
        repo = pathlib.Path("/tmp/repo")
        with mock.patch.object(
            runner,
            "_git_output",
            side_effect=[
                (True, "main"),
                (True, "main"),
                (True, "main"),
                (True, "codex/gemini-repo"),
            ],
        ), mock.patch.object(
            runner,
            "run_command",
            side_effect=[
                runner.subprocess.CompletedProcess(
                    ["git", "push"], returncode=1, stdout="", stderr="hook failed before remote check"
                ),
                runner.subprocess.CompletedProcess(
                    ["git", "push", "--no-verify"],
                    returncode=1,
                    stdout="",
                    stderr="GH013: Changes must be made through a pull request.",
                ),
                runner.subprocess.CompletedProcess(["git", "checkout", "codex/gemini-repo"], returncode=1, stdout="", stderr=""),
                runner.subprocess.CompletedProcess(["git", "checkout", "-b", "codex/gemini-repo"], returncode=0, stdout="", stderr=""),
                runner.subprocess.CompletedProcess(
                    ["git", "push", "--no-verify", "-u", "origin", "codex/gemini-repo"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ],
        ):
            ok, details = runner._push_with_recovery(repo, timeout_sec=10, owner="gemini")
        self.assertTrue(ok)
        self.assertIn("protected-branch switch", details)

class LocalOpenAITuningTests(unittest.TestCase):
    def setUp(self):
        runner._LOCAL_OPENAI_ENDPOINT_CONTEXT_CACHE = None
        runner._LOCAL_OPENAI_ENDPOINT_HEALTH_CACHE = None
        runner._LOCAL_OPENAI_ENDPOINT_FAILURE_STATE = {}
        runner._LOCAL_OPENAI_ENDPOINT_INFLIGHT = {}
        runner._LOCAL_OPENAI_MODEL_CURSOR = {}

    def test_local_openai_dynamic_max_tokens_prefers_endpoint_override(self):
        with mock.patch.dict(
            os.environ,
            {
                "ORXAQ_LOCAL_OPENAI_MAX_TOKENS_BY_ENDPOINT": "192.168.50.86:1234=3072",
                "ORXAQ_LOCAL_OPENAI_DYNAMIC_MAX_TOKENS": "1",
            },
            clear=False,
        ):
            tokens = runner._local_openai_dynamic_max_tokens("http://192.168.50.86:1234/v1", 1024)
        self.assertEqual(tokens, 3072)

    def test_local_openai_dynamic_max_tokens_uses_fleet_status_context(self):
        with tempfile.TemporaryDirectory() as td:
            status_file = pathlib.Path(td) / "fleet_status.json"
            status_file.write_text(
                json.dumps(
                    {
                        "capability_scan": {
                            "summary": {
                                "by_endpoint": {
                                    "lan-86": {
                                        "base_url": "http://192.168.50.86:1234/v1",
                                        "max_context_tokens_success": 4096,
                                    }
                                }
                            }
                        },
                        "probe": {
                            "endpoints": [
                                {"id": "lan-86", "base_url": "http://192.168.50.86:1234/v1"}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "ORXAQ_LOCAL_OPENAI_DYNAMIC_MAX_TOKENS": "1",
                    "ORXAQ_LOCAL_OPENAI_CONTEXT_FRACTION": "0.5",
                    "ORXAQ_LOCAL_MODEL_FLEET_STATUS_FILE": str(status_file),
                },
                clear=False,
            ):
                tokens = runner._local_openai_dynamic_max_tokens("http://192.168.50.86:1234/v1", 512)
        self.assertEqual(tokens, 2048)

    def test_select_local_openai_base_url_prefers_healthy_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            status_file = pathlib.Path(td) / "fleet_status.json"
            status_file.write_text(
                json.dumps(
                    {
                        "probe": {
                            "endpoints": [
                                {"id": "a", "base_url": "http://192.168.50.86:1234/v1", "ok": False},
                                {"id": "b", "base_url": "http://192.168.50.91:1234/v1", "ok": True},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                runner,
                "_local_openai_models_for_endpoint",
                return_value={"qwen/qwen2.5-coder-32b"},
            ), mock.patch.dict(
                os.environ,
                {
                    "ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.86:1234/v1,http://192.168.50.91:1234/v1",
                    "ORXAQ_LOCAL_MODEL_FLEET_STATUS_FILE": str(status_file),
                },
                clear=False,
            ):
                selected, slot, total = runner._select_local_openai_base_url("qwen/qwen2.5-coder-32b", 0)
        self.assertEqual(selected, "http://192.168.50.91:1234/v1")
        self.assertEqual(slot, 1)
        self.assertGreaterEqual(total, 2)

    def test_select_local_openai_base_url_avoids_cooled_down_endpoint(self):
        runner._LOCAL_OPENAI_MODEL_CURSOR.clear()
        runner._LOCAL_OPENAI_ENDPOINT_HEALTH_CACHE = None
        runner._LOCAL_OPENAI_ENDPOINT_FAILURE_STATE.clear()
        with mock.patch.object(
            runner,
            "_local_openai_models_for_endpoint",
            return_value={"qwen/qwen2.5-coder-32b"},
        ), mock.patch.dict(
            os.environ,
            {
                "ORXAQ_LOCAL_OPENAI_BASE_URLS": "http://192.168.50.86:1234/v1,http://192.168.50.91:1234/v1",
                "ORXAQ_LOCAL_OPENAI_INCLUDE_FLEET_ENDPOINTS": "0",
            },
            clear=False,
        ):
            endpoint_key = runner._local_openai_endpoint_key("http://192.168.50.86:1234/v1")
            runner._LOCAL_OPENAI_ENDPOINT_FAILURE_STATE[endpoint_key] = {
                "failures": 1,
                "cooldown_until": runner.time.monotonic() + 120.0,
                "last_error": "timeout",
            }
            selected, slot, total = runner._select_local_openai_base_url("qwen/qwen2.5-coder-32b", 0)
        self.assertNotEqual(selected, "http://192.168.50.86:1234/v1")
        self.assertIn(selected, {"http://192.168.50.91:1234/v1", "http://127.0.0.1:1234/v1"})
        self.assertGreaterEqual(slot, 0)
        self.assertGreaterEqual(total, 2)


class TaskQueueIngestionTests(unittest.TestCase):
    def test_ingest_task_queue_imports_new_unclaimed_tasks(self):
        existing = runner.Task(
            id="codex-existing",
            owner="codex",
            priority=1,
            title="existing",
            description="existing",
            depends_on=[],
            acceptance=[],
        )
        tasks = [existing]
        state = {existing.id: runner._default_task_state_entry(existing)}

        with tempfile.TemporaryDirectory() as td:
            queue_file = pathlib.Path(td) / "queue.ndjson"
            queue_state_file = pathlib.Path(td) / "queue_state.json"
            queue_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "codex-queued-1",
                                "owner": "codex",
                                "priority": 2,
                                "title": "queued",
                                "description": "queued work",
                                "acceptance": ["a"],
                            }
                        ),
                        json.dumps(
                            {
                                "id": "gemini-queued-1",
                                "owner": "gemini",
                                "priority": 2,
                                "title": "queued-g",
                                "description": "queued work gemini",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            payload = runner.ingest_task_queue(
                queue_file=queue_file,
                queue_state_file=queue_state_file,
                tasks=tasks,
                state=state,
                owner_filter={"codex"},
            )

            self.assertEqual(payload["imported"], ["codex-queued-1"])
            self.assertEqual(payload["skipped"], 1)
            self.assertIn("codex-queued-1", state)
            self.assertEqual(state["codex-queued-1"]["status"], runner.STATUS_PENDING)

            claimed = runner.load_task_queue_state(queue_state_file)
            self.assertIn("codex-queued-1", claimed)
            self.assertNotIn("gemini-queued-1", claimed)

            second = runner.ingest_task_queue(
                queue_file=queue_file,
                queue_state_file=queue_state_file,
                tasks=tasks,
                state=state,
                owner_filter={"codex"},
            )
            self.assertEqual(second["imported"], [])


if __name__ == "__main__":
    unittest.main()
