"""Tests for watchdog log doctor (issue #65)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import importlib
import sys

root = Path(__file__).resolve().parents[1]
src = root / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

from orxaq_autonomy import watchdog_log_doctor


class TestDiagnoseWatchdogLogs(unittest.TestCase):
    """Test the diagnose function with patched configs."""

    def test_missing_script_reports_broken(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_configs = {
                "test_watcher": {
                    "script": Path(tmpdir) / "nonexistent.py",
                    "plist": Path(tmpdir) / "test.plist",
                    "status_file": Path(tmpdir) / "status.json",
                    "real_log": Path(tmpdir) / "test.ndjson",
                    "stdout_log": Path(tmpdir) / "test.stdout.log",
                    "stderr_log": Path(tmpdir) / "test.stderr.log",
                    "launchctl_label": "com.test.watcher",
                },
            }
            with mock.patch.dict(watchdog_log_doctor.WATCHDOG_CONFIGS, test_configs, clear=True):
                results = watchdog_log_doctor.diagnose_watchdog_logs()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["name"], "test_watcher")
            self.assertEqual(results[0]["status"], "broken")
            self.assertTrue(any("Script missing" in i for i in results[0]["issues"]))

    @mock.patch.object(watchdog_log_doctor, "_is_running_launchctl", return_value=True)
    @mock.patch.object(watchdog_log_doctor, "_status_file_recent", return_value=True)
    def test_empty_stdout_log_reports_issue(self, mock_recent, mock_running):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            script = tmppath / "watcher.py"
            script.write_text("#!/usr/bin/env python3\n")
            plist = tmppath / "test.plist"
            plist.write_text("<plist></plist>")
            status = tmppath / "status.json"
            status.write_text("{}")
            # Create empty stdout log
            stdout_log = tmppath / "test.stdout.log"
            stdout_log.write_text("")
            stderr_log = tmppath / "test.stderr.log"
            stderr_log.write_text("")
            # Create non-empty real log
            real_log = tmppath / "test.ndjson"
            real_log.write_text('{"event":"test"}\n')

            test_configs = {
                "test_watcher": {
                    "script": script,
                    "plist": plist,
                    "status_file": status,
                    "real_log": real_log,
                    "stdout_log": stdout_log,
                    "stderr_log": stderr_log,
                    "launchctl_label": "com.test.watcher",
                },
            }
            with mock.patch.dict(watchdog_log_doctor.WATCHDOG_CONFIGS, test_configs, clear=True):
                results = watchdog_log_doctor.diagnose_watchdog_logs()
            self.assertEqual(len(results), 1)
            # Should be degraded because running + status recent, but stdout empty
            self.assertEqual(results[0]["status"], "degraded")
            self.assertTrue(
                any("stdout log empty" in i for i in results[0]["issues"])
            )

    @mock.patch.object(watchdog_log_doctor, "_is_running_launchctl", return_value=True)
    @mock.patch.object(watchdog_log_doctor, "_status_file_recent", return_value=True)
    def test_healthy_when_all_good(self, mock_recent, mock_running):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            script = tmppath / "watcher.py"
            script.write_text("#!/usr/bin/env python3\n")
            plist = tmppath / "test.plist"
            plist.write_text("<plist></plist>")
            status = tmppath / "status.json"
            status.write_text("{}")
            stdout_log = tmppath / "test.stdout.log"
            stdout_log.write_text("some output\n")
            stderr_log = tmppath / "test.stderr.log"
            stderr_log.write_text("")
            real_log = tmppath / "test.ndjson"
            real_log.write_text('{"event":"test"}\n')

            test_configs = {
                "test_watcher": {
                    "script": script,
                    "plist": plist,
                    "status_file": status,
                    "real_log": real_log,
                    "stdout_log": stdout_log,
                    "stderr_log": stderr_log,
                    "launchctl_label": "com.test.watcher",
                },
            }
            with mock.patch.dict(watchdog_log_doctor.WATCHDOG_CONFIGS, test_configs, clear=True):
                results = watchdog_log_doctor.diagnose_watchdog_logs()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "healthy")
            self.assertEqual(results[0]["issues"], [])


class TestFixWatchdogLogs(unittest.TestCase):
    @mock.patch.object(watchdog_log_doctor, "_is_running_launchctl", return_value=True)
    def test_fix_creates_log_files(self, mock_running):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            log_dir = tmppath / "logs"

            stdout_log = log_dir / "test.stdout.log"
            stderr_log = log_dir / "test.stderr.log"

            test_configs = {
                "test_watcher": {
                    "script": tmppath / "watcher.py",
                    "plist": tmppath / "test.plist",
                    "status_file": tmppath / "status.json",
                    "real_log": tmppath / "test.ndjson",
                    "stdout_log": stdout_log,
                    "stderr_log": stderr_log,
                    "launchctl_label": "com.test.watcher",
                },
            }
            with mock.patch.dict(watchdog_log_doctor.WATCHDOG_CONFIGS, test_configs, clear=True), \
                 mock.patch.object(watchdog_log_doctor, "LOG_DIR", log_dir):
                actions = watchdog_log_doctor.fix_watchdog_logs()
            # Should have created the log dir and initialized both log files
            init_actions = [a for a in actions if a["action"] == "init_log"]
            self.assertGreaterEqual(len(init_actions), 2)
            self.assertTrue(stdout_log.exists())
            self.assertGreater(stdout_log.stat().st_size, 0)


class TestDoctorReport(unittest.TestCase):
    @mock.patch.object(watchdog_log_doctor, "diagnose_watchdog_logs")
    def test_report_structure(self, mock_diagnose):
        mock_diagnose.return_value = [
            {"name": "w1", "status": "healthy", "issues": []},
            {"name": "w2", "status": "broken", "issues": ["script missing"]},
        ]
        report = watchdog_log_doctor.doctor_report()
        self.assertIn("timestamp", report)
        self.assertIn("watchdogs", report)
        self.assertIn("summary", report)
        self.assertEqual(report["summary"]["total"], 2)
        self.assertEqual(report["summary"]["healthy"], 1)
        self.assertEqual(report["summary"]["broken"], 1)


class TestFileAgeSec(unittest.TestCase):
    def test_missing_file_returns_none(self):
        result = watchdog_log_doctor._file_age_sec(Path("/nonexistent/file"))
        self.assertIsNone(result)

    def test_existing_file_returns_positive(self):
        with tempfile.NamedTemporaryFile() as f:
            result = watchdog_log_doctor._file_age_sec(Path(f.name))
            self.assertIsNotNone(result)
            self.assertGreaterEqual(result, 0)


class TestStatusFileRecent(unittest.TestCase):
    def test_missing_file_not_recent(self):
        self.assertFalse(
            watchdog_log_doctor._status_file_recent(Path("/nonexistent"))
        )

    def test_fresh_file_is_recent(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as f:
            f.write("{}")
            f.flush()
            self.assertTrue(
                watchdog_log_doctor._status_file_recent(Path(f.name), max_age_sec=60)
            )


if __name__ == "__main__":
    unittest.main()
