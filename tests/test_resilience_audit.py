"""Tests for manager.resilience_audit (Issue #7)."""

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.manager import ManagerConfig, resilience_audit


class ResilienceAuditTests(unittest.TestCase):
    """Issue #7: Audit autonomy stability runbooks and resilience controls."""

    def _build_root(self, tmp: pathlib.Path, *, with_scripts: bool = True) -> pathlib.Path:
        """Scaffold a minimal orxaq-ops root with optional scripts."""
        (tmp / "config").mkdir(parents=True, exist_ok=True)
        (tmp / "state").mkdir(parents=True, exist_ok=True)
        (tmp / "artifacts" / "autonomy").mkdir(parents=True, exist_ok=True)
        (tmp / "config" / "tasks.json").write_text("[]\n", encoding="utf-8")
        (tmp / "config" / "objective.md").write_text("obj\n", encoding="utf-8")
        (tmp / "config" / "codex_result.schema.json").write_text("{}\n", encoding="utf-8")
        (tmp / "config" / "skill_protocol.json").write_text("{}\n", encoding="utf-8")
        (tmp / ".env.autonomy").write_text("OPENAI_API_KEY=test\nGEMINI_API_KEY=test\n", encoding="utf-8")

        if with_scripts:
            scripts = tmp / "scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "autonomy_manager.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (scripts / "autonomy_runner.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            (scripts / "preflight.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            res = scripts / "resilience"
            res.mkdir(parents=True, exist_ok=True)
            (res / "backup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (res / "restore.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (res / "healthcheck.sh").write_text("#!/bin/bash\n", encoding="utf-8")

        return tmp

    def test_all_scripts_present_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td), with_scripts=True)
            cfg = ManagerConfig.from_root(root)
            report = resilience_audit(cfg)
            self.assertTrue(report["ok"])
            self.assertEqual(report["failed"], 0)
            self.assertEqual(report["passed"], report["total"])
            self.assertEqual(report["total"], 6)

    def test_missing_all_scripts_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td), with_scripts=False)
            cfg = ManagerConfig.from_root(root)
            report = resilience_audit(cfg)
            self.assertFalse(report["ok"])
            self.assertEqual(report["passed"], 0)
            self.assertEqual(report["failed"], 6)

    def test_partial_scripts_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td), with_scripts=False)
            scripts = root / "scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "autonomy_manager.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            cfg = ManagerConfig.from_root(root)
            report = resilience_audit(cfg)
            self.assertFalse(report["ok"])
            self.assertEqual(report["passed"], 1)
            self.assertEqual(report["failed"], 5)

    def test_report_structure(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td), with_scripts=True)
            cfg = ManagerConfig.from_root(root)
            report = resilience_audit(cfg)
            # Validate top-level keys
            for key in ("ok", "timestamp", "root_dir", "checks", "passed", "failed", "total"):
                self.assertIn(key, report, f"Missing key: {key}")
            # Validate each check entry
            for check in report["checks"]:
                for key in ("name", "category", "path", "exists", "passed"):
                    self.assertIn(key, check, f"Missing key in check: {key}")
                self.assertIn(check["category"], ("core", "resilience"))

    def test_total_equals_passed_plus_failed(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td), with_scripts=True)
            cfg = ManagerConfig.from_root(root)
            report = resilience_audit(cfg)
            self.assertEqual(report["total"], report["passed"] + report["failed"])

    def test_cli_resilience_audit_exit_code_zero(self):
        """The CLI subcommand should exit 0 when all scripts exist."""
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td), with_scripts=True)
            from orxaq_autonomy.cli import main as cli_main
            rc = cli_main(["--root", str(root), "resilience-audit"])
            self.assertEqual(rc, 0)

    def test_cli_resilience_audit_exit_code_nonzero_on_failure(self):
        """The CLI subcommand should exit 1 when scripts are missing."""
        with tempfile.TemporaryDirectory() as td:
            root = self._build_root(pathlib.Path(td), with_scripts=False)
            from orxaq_autonomy.cli import main as cli_main
            rc = cli_main(["--root", str(root), "resilience-audit"])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
