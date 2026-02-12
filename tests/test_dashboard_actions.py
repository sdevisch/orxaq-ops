"""Tests for the dashboard action system (dashboard_actions.py + dashboard_v2.py POST routes)."""

import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib import error as urllib_error
from urllib import request as urllib_request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import dashboard_actions, dashboard_v2


# ── Test Fixtures ────────────────────────────────────────────────────────────

def build_fixture(root: pathlib.Path) -> pathlib.Path:
    """Build a minimal fixture tree for action testing. Returns artifacts_dir."""
    # config/tasks.json
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    tasks = [
        {"id": "task-alpha", "owner": "codex", "priority": 1, "title": "Alpha",
         "description": "First task", "depends_on": [], "acceptance": []},
        {"id": "task-beta", "owner": "gemini", "priority": 3, "title": "Beta",
         "description": "Second task", "depends_on": ["task-alpha"], "acceptance": []},
        {"id": "task-gamma", "owner": "codex", "priority": 2, "title": "Gamma",
         "description": "Third task", "depends_on": [], "acceptance": []},
    ]
    (config_dir / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")

    # state/state.json
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    state = {
        "task-alpha": {"status": "done", "attempts": 1, "retryable_failures": 0,
                       "last_error": "", "last_summary": "completed", "owner": "codex",
                       "not_before": "", "last_update": "2026-02-12T00:00:00Z"},
        "task-beta": {"status": "blocked", "attempts": 5, "retryable_failures": 3,
                      "last_error": "timeout after 300s", "last_summary": "stuck on tests",
                      "owner": "gemini", "not_before": "2026-02-12T06:00:00Z",
                      "last_update": "2026-02-12T01:00:00Z"},
        "task-gamma": {"status": "pending", "attempts": 0, "retryable_failures": 0,
                       "last_error": "", "last_summary": "", "owner": "codex",
                       "not_before": "", "last_update": ""},
    }
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    # artifacts/autonomy/
    artifacts_dir = root / "artifacts"
    auto_dir = artifacts_dir / "autonomy"
    auto_dir.mkdir(parents=True)
    (auto_dir / "health.json").write_text('{"ok": true}', encoding="utf-8")

    # Minimal lane for snapshot to work
    lane_dir = auto_dir / "lanes" / "test-lane"
    lane_dir.mkdir(parents=True)
    (lane_dir / "lane.json").write_text('{"owner": "codex"}', encoding="utf-8")
    (lane_dir / "state.json").write_text(
        '{"task-alpha": {"status": "done", "attempts": 1}}', encoding="utf-8")
    (lane_dir / "heartbeat.json").write_text(json.dumps({
        "cycle": 1, "phase": "idle", "pid": 1,
        "timestamp": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")

    return artifacts_dir


# ── AuditLog Tests ───────────────────────────────────────────────────────────

class AuditLogTests(unittest.TestCase):
    """Tests for ActionAuditLog."""

    def test_log_creates_file_and_appends(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = pathlib.Path(td) / "sub" / "audit.ndjson"
            audit = dashboard_actions.ActionAuditLog(log_path)
            audit.log({"action_id": "test.one", "ok": True})
            audit.log({"action_id": "test.two", "ok": False})
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["action_id"], "test.one")
            self.assertEqual(json.loads(lines[1])["action_id"], "test.two")

    def test_recent_reads_tail(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = pathlib.Path(td) / "audit.ndjson"
            audit = dashboard_actions.ActionAuditLog(log_path)
            for i in range(10):
                audit.log({"i": i})
            entries = audit.recent(tail=3)
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[0]["i"], 7)

    def test_recent_empty_file(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = pathlib.Path(td) / "audit.ndjson"
            audit = dashboard_actions.ActionAuditLog(log_path)
            entries = audit.recent()
            self.assertEqual(entries, [])


# ── Confirmation Token Tests ─────────────────────────────────────────────────

class ConfirmTokenTests(unittest.TestCase):
    """Tests for the two-phase confirmation token system."""

    def setUp(self):
        dashboard_actions._PENDING_CONFIRMATIONS.clear()

    def test_generate_and_validate(self):
        token = dashboard_actions.generate_confirm_token("test.action", {"key": "val"})
        self.assertTrue(token.startswith("tok-"))
        self.assertTrue(dashboard_actions.validate_confirm_token(token, "test.action"))

    def test_single_use(self):
        token = dashboard_actions.generate_confirm_token("test.action", {})
        self.assertTrue(dashboard_actions.validate_confirm_token(token, "test.action"))
        # Second use fails
        self.assertFalse(dashboard_actions.validate_confirm_token(token, "test.action"))

    def test_wrong_action_id(self):
        token = dashboard_actions.generate_confirm_token("action.a", {})
        self.assertFalse(dashboard_actions.validate_confirm_token(token, "action.b"))

    def test_nonexistent_token(self):
        self.assertFalse(dashboard_actions.validate_confirm_token("tok-nonexistent", "test"))

    def test_expired_token(self):
        token = dashboard_actions.generate_confirm_token("test.action", {})
        # Manually expire it
        dashboard_actions._PENDING_CONFIRMATIONS[token]["expires_at"] = time.time() - 1
        self.assertFalse(dashboard_actions.validate_confirm_token(token, "test.action"))


# ── Rate Limiter Tests ───────────────────────────────────────────────────────

class RateLimiterTests(unittest.TestCase):
    """Tests for the rate limiting system."""

    def setUp(self):
        dashboard_actions.reset_rate_limits()

    def test_allows_within_limit(self):
        for _ in range(5):
            self.assertIsNone(dashboard_actions.check_rate_limit("low"))

    def test_rejects_over_global_limit(self):
        for _ in range(dashboard_actions.RATE_LIMIT_GLOBAL):
            dashboard_actions.check_rate_limit("low")
        result = dashboard_actions.check_rate_limit("low")
        self.assertIn("Rate limit", result)

    def test_high_risk_separate_limit(self):
        for _ in range(dashboard_actions.RATE_LIMIT_HIGH):
            dashboard_actions.check_rate_limit("high")
        result = dashboard_actions.check_rate_limit("high")
        self.assertIn("high-risk", result)


# ── Task Reprioritize Tests ──────────────────────────────────────────────────

class TaskReprioritizeTests(unittest.TestCase):

    def test_reprioritize_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_reprioritize(
                ctx, {"task_id": "task-beta", "new_priority": 1}, dry_run=False)
            self.assertTrue(result.ok)
            self.assertEqual(result.detail["new_priority"], 1)
            self.assertEqual(result.detail["old_priority"], 3)
            # Verify file was updated
            tasks = json.loads((root / "config" / "tasks.json").read_text())
            beta = next(t for t in tasks if t["id"] == "task-beta")
            self.assertEqual(beta["priority"], 1)

    def test_reprioritize_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_reprioritize(
                ctx, {"task_id": "task-beta", "new_priority": 1}, dry_run=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.dry_run)
            # Verify file was NOT updated
            tasks = json.loads((root / "config" / "tasks.json").read_text())
            beta = next(t for t in tasks if t["id"] == "task-beta")
            self.assertEqual(beta["priority"], 3)

    def test_reprioritize_invalid_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_reprioritize(
                ctx, {"task_id": "nonexistent", "new_priority": 1}, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("not found", result.message)

    def test_reprioritize_invalid_priority(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_reprioritize(
                ctx, {"task_id": "task-beta", "new_priority": 1500}, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("0-999", result.message)


# ── Task Update Status Tests ────────────────────────────────────────────────

class TaskUpdateStatusTests(unittest.TestCase):

    def test_unblock_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_update_status(
                ctx, {"task_id": "task-beta", "new_status": "pending"}, dry_run=False)
            self.assertTrue(result.ok)
            self.assertEqual(result.detail["previous_status"], "blocked")
            self.assertEqual(result.detail["new_status"], "pending")
            # Verify state file
            state = json.loads((root / "state" / "state.json").read_text())
            self.assertEqual(state["task-beta"]["status"], "pending")
            self.assertEqual(state["task-beta"]["last_error"], "")
            self.assertEqual(state["task-beta"]["not_before"], "")

    def test_rejects_in_progress_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_update_status(
                ctx, {"task_id": "task-beta", "new_status": "in_progress"}, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("Invalid status", result.message)

    def test_rejects_nonexistent_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_update_status(
                ctx, {"task_id": "no-such", "new_status": "pending"}, dry_run=False)
            self.assertFalse(result.ok)

    def test_dry_run_does_not_modify(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_update_status(
                ctx, {"task_id": "task-beta", "new_status": "pending"}, dry_run=True)
            self.assertTrue(result.ok)
            state = json.loads((root / "state" / "state.json").read_text())
            self.assertEqual(state["task-beta"]["status"], "blocked")


# ── Task Clear Error Tests ───────────────────────────────────────────────────

class TaskClearErrorTests(unittest.TestCase):

    def test_clear_error_and_unblock(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_clear_error(
                ctx, {"task_id": "task-beta"}, dry_run=False)
            self.assertTrue(result.ok)
            self.assertEqual(result.detail["cleared_failures"], 3)
            state = json.loads((root / "state" / "state.json").read_text())
            self.assertEqual(state["task-beta"]["status"], "pending")
            self.assertEqual(state["task-beta"]["last_error"], "")
            self.assertEqual(state["task-beta"]["retryable_failures"], 0)

    def test_clear_error_on_pending_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_task_clear_error(
                ctx, {"task_id": "task-gamma"}, dry_run=False)
            self.assertTrue(result.ok)
            state = json.loads((root / "state" / "state.json").read_text())
            self.assertEqual(state["task-gamma"]["status"], "pending")


# ── Breakglass Grant Tests ───────────────────────────────────────────────────

class BreakglassGrantTests(unittest.TestCase):

    def test_grant_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            dashboard_actions._PENDING_CONFIRMATIONS.clear()
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_breakglass_grant(
                ctx, {"reason": "Test", "scope": "test scope", "ttl_minutes": 15}, dry_run=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.dry_run)
            self.assertIsNotNone(result.confirm_token)

    def test_grant_creates_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_breakglass_grant(
                ctx, {"reason": "Unblock PR", "scope": "no-verify push",
                      "ttl_minutes": 20, "task_allowlist": ["task-beta"]},
                dry_run=False)
            self.assertTrue(result.ok)
            grant_id = result.detail["grant_id"]
            # Verify grant file exists
            grant_path = root / "artifacts" / "autonomy" / "breakglass" / "grants" / f"{grant_id}.json"
            self.assertTrue(grant_path.exists())
            grant_data = json.loads(grant_path.read_text())
            self.assertEqual(grant_data["scope"], "no-verify push")
            self.assertEqual(grant_data["ttl_minutes"], 20)
            # Verify escalation logged
            esc_path = root / "artifacts" / "autonomy" / "privilege_escalations.ndjson"
            self.assertTrue(esc_path.exists())
            lines = esc_path.read_text().strip().splitlines()
            self.assertTrue(any("breakglass_grant_created" in l for l in lines))

    def test_grant_rejects_missing_reason(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_breakglass_grant(
                ctx, {"scope": "test", "ttl_minutes": 15}, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("reason", result.message)

    def test_grant_rejects_excessive_ttl(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_breakglass_grant(
                ctx, {"reason": "Test", "scope": "test", "ttl_minutes": 9999}, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("1-1440", result.message)


# ── Breakglass Revoke Tests ──────────────────────────────────────────────────

class BreakglassRevokeTests(unittest.TestCase):

    def test_revoke_existing_grant(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            # First create a grant
            create_result = dashboard_actions.action_breakglass_grant(
                ctx, {"reason": "Test", "scope": "test scope", "ttl_minutes": 10}, dry_run=False)
            grant_id = create_result.detail["grant_id"]
            # Now revoke it
            result = dashboard_actions.action_breakglass_revoke(
                ctx, {"grant_id": grant_id}, dry_run=False)
            self.assertTrue(result.ok)
            # Verify grant file was removed
            grant_path = root / "artifacts" / "autonomy" / "breakglass" / "grants" / f"{grant_id}.json"
            self.assertFalse(grant_path.exists())

    def test_revoke_nonexistent_grant(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_breakglass_revoke(
                ctx, {"grant_id": "bg-nonexistent"}, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("not found", result.message)


# ── Git Heal Locks Tests ─────────────────────────────────────────────────────

class GitHealLocksTests(unittest.TestCase):

    def test_heal_no_locks(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            git_dir = root / ".git"
            git_dir.mkdir()
            ctx = dashboard_actions.build_context(root / "artifacts", root)
            result = dashboard_actions.action_git_heal_locks(ctx, {}, dry_run=False)
            self.assertTrue(result.ok)
            self.assertIn("No stale", result.message)

    def test_heal_removes_locks(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            git_dir = root / ".git"
            git_dir.mkdir()
            # Create stale lock files
            (git_dir / "index.lock").write_text("lock", encoding="utf-8")
            (git_dir / "HEAD.lock").write_text("lock", encoding="utf-8")
            ctx = dashboard_actions.build_context(root / "artifacts", root)
            result = dashboard_actions.action_git_heal_locks(ctx, {}, dry_run=False)
            self.assertTrue(result.ok)
            self.assertIn("2", result.message)
            self.assertFalse((git_dir / "index.lock").exists())
            self.assertFalse((git_dir / "HEAD.lock").exists())

    def test_heal_dry_run_does_not_remove(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "index.lock").write_text("lock", encoding="utf-8")
            dashboard_actions._PENDING_CONFIRMATIONS.clear()
            ctx = dashboard_actions.build_context(root / "artifacts", root)
            result = dashboard_actions.action_git_heal_locks(ctx, {}, dry_run=True)
            self.assertTrue(result.ok)
            self.assertIsNotNone(result.confirm_token)
            self.assertTrue((git_dir / "index.lock").exists())

    def test_heal_no_repo_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_git_heal_locks(ctx, {}, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("repo_dir", result.message)


# ── Runner Release Lock Tests ────────────────────────────────────────────────

class RunnerReleaseLockTests(unittest.TestCase):

    def test_no_lock_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_runner_release_lock(ctx, {}, dry_run=False)
            self.assertTrue(result.ok)
            self.assertIn("No runner lock", result.message)

    def test_removes_stale_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            lock_path = root / "artifacts" / "autonomy" / "runner.lock"
            lock_path.write_text(json.dumps({"pid": 999999, "created_at": "2026-01-01T00:00:00Z"}),
                                 encoding="utf-8")
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_runner_release_lock(ctx, {}, dry_run=False)
            self.assertTrue(result.ok)
            self.assertFalse(lock_path.exists())


# ── Runner Subprocess Action Tests ───────────────────────────────────────────

class RunnerSubprocessTests(unittest.TestCase):

    @mock.patch("orxaq_autonomy.dashboard_actions.subprocess.run")
    def test_stop_dry_run_no_subprocess(self, mock_run):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            dashboard_actions._PENDING_CONFIRMATIONS.clear()
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_runner_stop(
                ctx, {"reason": "test"}, dry_run=True)
            self.assertTrue(result.ok)
            self.assertIsNotNone(result.confirm_token)
            mock_run.assert_not_called()

    @mock.patch("orxaq_autonomy.dashboard_actions.subprocess.run")
    def test_stop_calls_cli(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout="stopped", stderr="")
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_runner_stop(
                ctx, {"reason": "manual"}, dry_run=False)
            self.assertTrue(result.ok)
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            self.assertIn("stop", call_args)
            self.assertIn("manual", call_args)

    @mock.patch("orxaq_autonomy.dashboard_actions.subprocess.run")
    def test_start_calls_cli(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout="started", stderr="")
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_runner_start(ctx, {}, dry_run=False)
            self.assertTrue(result.ok)
            call_args = mock_run.call_args[0][0]
            self.assertIn("start", call_args)

    @mock.patch("orxaq_autonomy.dashboard_actions.subprocess.run")
    def test_ensure_calls_cli(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout="ensured", stderr="")
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            ctx = dashboard_actions.build_context(root / "artifacts", None)
            result = dashboard_actions.action_runner_ensure(ctx, {}, dry_run=False)
            self.assertTrue(result.ok)
            call_args = mock_run.call_args[0][0]
            self.assertIn("ensure", call_args)


# ── Action Catalog Tests ─────────────────────────────────────────────────────

class ActionCatalogTests(unittest.TestCase):

    def test_catalog_lists_all_actions(self):
        catalog = dashboard_actions.get_action_catalog()
        self.assertIsInstance(catalog, list)
        action_ids = {a["action_id"] for a in catalog}
        self.assertIn("task.reprioritize", action_ids)
        self.assertIn("task.update-status", action_ids)
        self.assertIn("task.clear-error", action_ids)
        self.assertIn("breakglass.grant", action_ids)
        self.assertIn("breakglass.revoke", action_ids)
        self.assertIn("git.heal-locks", action_ids)
        self.assertIn("runner.release-lock", action_ids)
        self.assertIn("runner.stop", action_ids)
        self.assertIn("runner.start", action_ids)
        self.assertIn("runner.ensure", action_ids)

    def test_catalog_has_required_fields(self):
        catalog = dashboard_actions.get_action_catalog()
        for action in catalog:
            self.assertIn("action_id", action)
            self.assertIn("risk_level", action)
            self.assertIn("description", action)
            self.assertIn("requires_confirmation", action)
            self.assertIn("param_schema", action)


# ── Dispatch Tests ───────────────────────────────────────────────────────────

class DispatchTests(unittest.TestCase):

    def setUp(self):
        dashboard_actions.reset_rate_limits()
        dashboard_actions._PENDING_CONFIRMATIONS.clear()

    def test_dispatch_unknown_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            result = dashboard_actions.dispatch_action(
                action_id="nonexistent.action",
                params={},
                confirm_token="",
                dry_run=False,
                artifacts_dir=root / "artifacts",
                repo_dir=None,
            )
            self.assertFalse(result.ok)
            self.assertIn("Unknown action", result.message)

    def test_dispatch_low_risk_executes_without_token(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            result = dashboard_actions.dispatch_action(
                action_id="task.clear-error",
                params={"task_id": "task-beta"},
                confirm_token="",
                dry_run=False,
                artifacts_dir=root / "artifacts",
                repo_dir=None,
            )
            self.assertTrue(result.ok)
            self.assertIsNotNone(result.audit_id)

    def test_dispatch_medium_risk_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "index.lock").write_text("lock", encoding="utf-8")
            # Without confirmation
            result = dashboard_actions.dispatch_action(
                action_id="git.heal-locks",
                params={},
                confirm_token="",
                dry_run=False,
                artifacts_dir=root / "artifacts",
                repo_dir=root,
            )
            self.assertFalse(result.ok)
            self.assertIn("confirmation", result.message.lower())

    def test_dispatch_two_phase_confirmation_flow(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "index.lock").write_text("lock", encoding="utf-8")

            # Phase 1: dry run to get token
            preview = dashboard_actions.dispatch_action(
                action_id="git.heal-locks",
                params={},
                confirm_token="",
                dry_run=True,
                artifacts_dir=root / "artifacts",
                repo_dir=root,
            )
            self.assertTrue(preview.ok)
            self.assertIsNotNone(preview.confirm_token)

            # Phase 2: execute with token
            result = dashboard_actions.dispatch_action(
                action_id="git.heal-locks",
                params={},
                confirm_token=preview.confirm_token,
                dry_run=False,
                artifacts_dir=root / "artifacts",
                repo_dir=root,
            )
            self.assertTrue(result.ok)
            self.assertFalse((git_dir / "index.lock").exists())

    def test_dispatch_logs_to_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            result = dashboard_actions.dispatch_action(
                action_id="task.reprioritize",
                params={"task_id": "task-beta", "new_priority": 1},
                confirm_token="",
                dry_run=False,
                artifacts_dir=root / "artifacts",
                repo_dir=None,
            )
            self.assertTrue(result.ok)
            audit_path = root / "artifacts" / "autonomy" / "dashboard_actions_audit.ndjson"
            self.assertTrue(audit_path.exists())
            entries = json.loads(audit_path.read_text().strip().splitlines()[0])
            self.assertEqual(entries["action_id"], "task.reprioritize")
            self.assertTrue(entries["result_ok"])

    def test_dispatch_dry_run_skips_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            dashboard_actions.dispatch_action(
                action_id="task.reprioritize",
                params={"task_id": "task-beta", "new_priority": 1},
                confirm_token="",
                dry_run=True,
                artifacts_dir=root / "artifacts",
                repo_dir=None,
            )
            audit_path = root / "artifacts" / "autonomy" / "dashboard_actions_audit.ndjson"
            self.assertFalse(audit_path.exists())


# ── HTTP Integration Tests ───────────────────────────────────────────────────

class ActionHTTPTests(unittest.TestCase):
    """Integration tests for the POST action endpoints."""

    def setUp(self):
        dashboard_actions.reset_rate_limits()
        dashboard_actions._PENDING_CONFIRMATIONS.clear()

    def _start_server(self, artifacts_dir, repo_dir=None):
        handler = dashboard_v2.make_v2_handler(artifacts_dir, repo_dir=repo_dir)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _post(self, base, path, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib_request.Request(
            f"{base}{path}", data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError as err:
            body = json.loads(err.read().decode("utf-8"))
            code = err.code
            err.close()
            return code, body

    def test_get_action_catalog(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            server, thread = self._start_server(root / "artifacts")
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib_request.urlopen(f"{base}/api/v2/actions", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIsInstance(data, list)
                    self.assertTrue(len(data) >= 10)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_post_low_risk_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            server, thread = self._start_server(root / "artifacts")
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                status, data = self._post(base, "/api/v2/actions/task.clear-error",
                                          {"params": {"task_id": "task-beta"}})
                self.assertEqual(status, 200)
                self.assertTrue(data["ok"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_post_unknown_action_404(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            server, thread = self._start_server(root / "artifacts")
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                status, data = self._post(base, "/api/v2/actions/bogus.action",
                                          {"params": {}})
                self.assertEqual(status, 404)
                self.assertFalse(data["ok"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_post_medium_risk_without_token_409(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "index.lock").write_text("lock", encoding="utf-8")
            server, thread = self._start_server(root / "artifacts", repo_dir=root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                status, data = self._post(base, "/api/v2/actions/git.heal-locks",
                                          {"params": {}})
                self.assertEqual(status, 409)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_post_two_phase_confirm_flow(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "index.lock").write_text("lock", encoding="utf-8")
            server, thread = self._start_server(root / "artifacts", repo_dir=root)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                # Phase 1: dry run
                s1, d1 = self._post(base, "/api/v2/actions/git.heal-locks",
                                    {"params": {}, "dry_run": True})
                self.assertEqual(s1, 200)
                self.assertTrue(d1["ok"])
                token = d1["confirm_token"]
                self.assertIsNotNone(token)
                # Phase 2: execute
                s2, d2 = self._post(base, "/api/v2/actions/git.heal-locks",
                                    {"params": {}, "confirm_token": token})
                self.assertEqual(s2, 200)
                self.assertTrue(d2["ok"])
                self.assertFalse((git_dir / "index.lock").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_options_cors_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            server, thread = self._start_server(root / "artifacts")
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                req = urllib_request.Request(
                    f"{base}/api/v2/actions/task.clear-error",
                    method="OPTIONS",
                )
                with urllib_request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 204)
                    self.assertEqual(resp.headers.get("Access-Control-Allow-Methods"),
                                     "GET, POST, OPTIONS")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_get_audit_trail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            server, thread = self._start_server(root / "artifacts")
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                # Execute an action to generate audit entry
                self._post(base, "/api/v2/actions/task.clear-error",
                           {"params": {"task_id": "task-beta"}})
                # Fetch audit trail
                with urllib_request.urlopen(f"{base}/api/v2/actions/audit", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertIsInstance(data, list)
                    self.assertTrue(len(data) >= 1)
                    self.assertEqual(data[0]["action_id"], "task.clear-error")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_invalid_json_body_400(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            build_fixture(root)
            server, thread = self._start_server(root / "artifacts")
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                req = urllib_request.Request(
                    f"{base}/api/v2/actions/task.clear-error",
                    data=b"not json",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    urllib_request.urlopen(req, timeout=5)
                    self.fail("Expected HTTP 400")
                except urllib_error.HTTPError as err:
                    self.assertEqual(err.code, 400)
                    err.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


# ── ActionResult Tests ───────────────────────────────────────────────────────

class ActionResultTests(unittest.TestCase):

    def test_to_dict(self):
        result = dashboard_actions.ActionResult(
            ok=True, action_id="test", dry_run=False, message="ok",
            detail={"key": "val"}, audit_id="aud-1", confirm_token=None)
        d = result.to_dict()
        self.assertEqual(d["ok"], True)
        self.assertEqual(d["action_id"], "test")
        self.assertEqual(d["detail"]["key"], "val")
        self.assertIsNone(d["confirm_token"])


if __name__ == "__main__":
    unittest.main()
