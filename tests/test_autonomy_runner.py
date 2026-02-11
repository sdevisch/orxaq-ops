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

    def test_run_command_returns_nonzero_when_binary_missing(self):
        result = runner.run_command(["definitely-not-a-real-binary"], cwd=pathlib.Path("/tmp"), timeout_sec=1)
        self.assertEqual(result.returncode, 127)
        stderr_lower = result.stderr.lower()
        self.assertTrue(
            "no such file" in stderr_lower or "cannot find the file" in stderr_lower,
            msg=f"unexpected missing-binary message: {result.stderr}",
        )


class DeliveryContractTests(unittest.TestCase):
    def _task(self, owner: str = "codex") -> runner.Task:
        return runner.Task(
            id="task-1",
            owner=owner,
            priority=1,
            title="Title",
            description="Desc",
            depends_on=[],
            acceptance=["a1"],
        )

    def test_extract_delivery_signals_parses_review_metadata(self):
        outcome = {
            "summary": "review_status=failed review_score=41 urgent_fix=yes",
            "commit": "abc123",
            "validations": ["make test", "make lint"],
            "next_actions": [
                "pr_url=https://github.com/Orxaq/orxaq-ops/pull/20",
                "higher_level_review_todo=ops/backlog/distributed_todo.yaml#r20",
                "merge_effective=pending",
            ],
        }
        signals = runner.extract_delivery_signals(outcome)
        self.assertTrue(signals["has_pr_url"])
        self.assertTrue(signals["has_higher_level_review_todo"])
        self.assertTrue(signals["has_unit_test_evidence"])
        self.assertEqual(signals["review_status"], "failed")
        self.assertEqual(signals["review_score"], 41)
        self.assertTrue(signals["urgent_fix"])
        self.assertEqual(signals["merge_effective"], "pending")

    def test_evaluate_delivery_contract_rejects_missing_review_score(self):
        outcome = {
            "summary": "review_status=passed merge_effective=branch_gone",
            "commit": "abc123",
            "validations": ["pytest -q"],
            "next_actions": [
                "pr_url=https://github.com/Orxaq/orxaq-ops/pull/21",
                "higher_level_review_todo=todo-21",
            ],
        }
        ok, reason = runner.evaluate_delivery_contract(self._task("codex"), outcome)
        self.assertFalse(ok)
        self.assertIn("review_score", reason)

    def test_evaluate_delivery_contract_rejects_failed_review_without_urgent_fix(self):
        outcome = {
            "summary": "review_status=failed review_score=23 merge_effective=pending",
            "commit": "abc123",
            "validations": ["make test"],
            "next_actions": [
                "pr_url=https://github.com/Orxaq/orxaq-ops/pull/22",
                "higher_level_review_todo=todo-22",
            ],
        }
        ok, reason = runner.evaluate_delivery_contract(self._task("codex"), outcome)
        self.assertFalse(ok)
        self.assertIn("urgent_fix", reason)

    def test_evaluate_delivery_contract_accepts_passed_review_with_branch_gone(self):
        outcome = {
            "summary": "review_status=passed review_score=93 urgent_fix=no",
            "commit": "abc123",
            "validations": ["make lint", "make test"],
            "next_actions": [
                "pr_url=https://github.com/Orxaq/orxaq-ops/pull/23",
                "higher_level_review_todo=todo-23",
                "merge_effective=branch_gone",
            ],
        }
        ok, reason = runner.evaluate_delivery_contract(self._task("codex"), outcome)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


class DeliveryCycleProgressionTests(unittest.TestCase):
    def test_main_retries_until_review_passes_and_branch_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            impl_repo = root / "impl"
            test_repo = root / "test"
            impl_repo.mkdir()
            test_repo.mkdir()

            tasks_file = root / "tasks.json"
            state_file = root / "state.json"
            objective_file = root / "objective.md"
            schema_file = root / "schema.json"
            skill_file = root / "skill.json"
            artifacts_dir = root / "artifacts"
            heartbeat_file = root / "heartbeat.json"
            lock_file = root / "runner.lock"

            tasks_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "task-a",
                            "owner": "codex",
                            "priority": 1,
                            "title": "Task A",
                            "description": "Task A desc",
                            "depends_on": [],
                            "acceptance": ["done"],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            objective_file.write_text("Objective", encoding="utf-8")
            schema_file.write_text("{}", encoding="utf-8")
            skill_file.write_text(
                json.dumps({"name": "proto", "version": "1", "description": "d", "required_behaviors": ["x"]}),
                encoding="utf-8",
            )

            outcomes = [
                (
                    True,
                    {
                        "status": "done",
                        "summary": "review_status=failed review_score=35 urgent_fix=yes merge_effective=pending",
                        "commit": "abc123",
                        "validations": ["make test"],
                        "next_actions": [
                            "pr_url=https://github.com/Orxaq/orxaq-ops/pull/31",
                            "higher_level_review_todo=todo-31",
                        ],
                        "blocker": "",
                    },
                ),
                (
                    True,
                    {
                        "status": "done",
                        "summary": "review_status=passed review_score=88 urgent_fix=no merge_effective=pending",
                        "commit": "def456",
                        "validations": ["pytest -q"],
                        "next_actions": [
                            "pr_url=https://github.com/Orxaq/orxaq-ops/pull/31",
                            "higher_level_review_todo=todo-31",
                        ],
                        "blocker": "",
                    },
                ),
                (
                    True,
                    {
                        "status": "done",
                        "summary": "review_status=passed review_score=94 urgent_fix=no merge_effective=branch_gone",
                        "commit": "fedcba",
                        "validations": ["make lint", "make test"],
                        "next_actions": [
                            "pr_url=https://github.com/Orxaq/orxaq-ops/pull/31",
                            "higher_level_review_todo=todo-31",
                        ],
                        "blocker": "",
                    },
                ),
            ]

            run_calls = {"count": 0}

            def fake_run_codex_task(**_kwargs):
                idx = run_calls["count"]
                run_calls["count"] += 1
                return outcomes[idx]

            def immediate_retry(*, entry, summary, error, retryable, backoff_base_sec, backoff_max_sec):
                del summary, error, backoff_base_sec, backoff_max_sec
                entry["status"] = runner.STATUS_PENDING
                entry["not_before"] = ""
                if retryable:
                    entry["retryable_failures"] = int(entry.get("retryable_failures", 0)) + 1
                entry["last_update"] = runner._now_iso()
                return 0

            argv = [
                "--impl-repo",
                str(impl_repo),
                "--test-repo",
                str(test_repo),
                "--tasks-file",
                str(tasks_file),
                "--state-file",
                str(state_file),
                "--objective-file",
                str(objective_file),
                "--codex-schema",
                str(schema_file),
                "--artifacts-dir",
                str(artifacts_dir),
                "--heartbeat-file",
                str(heartbeat_file),
                "--lock-file",
                str(lock_file),
                "--max-cycles",
                "6",
                "--max-attempts",
                "6",
                "--validate-command",
                "make test",
                "--skill-protocol-file",
                str(skill_file),
            ]

            with (
                mock.patch.object(runner, "ensure_cli_exists", return_value=None),
                mock.patch.object(runner, "run_codex_task", side_effect=fake_run_codex_task),
                mock.patch.object(runner, "run_validations", return_value=(True, "ok")),
                mock.patch.object(runner, "summarize_run", return_value=None),
                mock.patch.object(runner, "schedule_retry", side_effect=immediate_retry),
                mock.patch.object(runner, "_print", return_value=None),
            ):
                rc = runner.main(argv)

            self.assertEqual(rc, 0)
            self.assertEqual(run_calls["count"], 3)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["task-a"]["status"], runner.STATUS_DONE)


if __name__ == "__main__":
    unittest.main()
