import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_provider_cost_health.py"

module_spec = importlib.util.spec_from_file_location("check_provider_cost_health", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_provider_cost_health = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_provider_cost_health", check_provider_cost_health)
module_spec.loader.exec_module(check_provider_cost_health)


class ProviderCostHealthCheckTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(check_provider_cost_health._int_value("x", 7), 7)
        self.assertEqual(
            check_provider_cost_health._parse_required_providers("OpenAI, gemini,openai"),
            ["openai", "gemini"],
        )

    def test_main_passes_for_fresh_summary(self):
        payload = {
            "ok": True,
            "timestamp": "2026-02-09T20:00:00+00:00",
            "records_total": 3,
            "providers": [
                {"provider": "openai", "ok": True, "status": "ok"},
                {"provider": "anthropic", "ok": False, "status": "skipped"},
            ],
            "data_freshness": {"age_sec": 120, "stale": False, "stale_threshold_sec": 900},
        }
        with tempfile.TemporaryDirectory() as td:
            summary_file = pathlib.Path(td) / "summary.json"
            summary_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            rc = check_provider_cost_health.main(
                [
                    "--summary-file",
                    str(summary_file),
                    "--max-age-sec",
                    "300",
                ]
            )
        self.assertEqual(rc, 0)

    def test_main_fails_when_stale(self):
        payload = {
            "ok": True,
            "timestamp": "2026-02-09T20:00:00+00:00",
            "records_total": 3,
            "providers": [
                {"provider": "openai", "ok": True, "status": "ok"},
            ],
            "data_freshness": {"age_sec": 1200, "stale": True, "stale_threshold_sec": 900},
        }
        with tempfile.TemporaryDirectory() as td:
            summary_file = pathlib.Path(td) / "summary.json"
            summary_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            rc = check_provider_cost_health.main(
                [
                    "--summary-file",
                    str(summary_file),
                    "--max-age-sec",
                    "300",
                ]
            )
        self.assertEqual(rc, 1)

    def test_main_fails_when_required_provider_missing(self):
        payload = {
            "ok": True,
            "timestamp": "2026-02-09T20:00:00+00:00",
            "records_total": 1,
            "providers": [
                {"provider": "openai", "ok": True, "status": "ok"},
            ],
            "data_freshness": {"age_sec": 60, "stale": False, "stale_threshold_sec": 900},
        }
        with tempfile.TemporaryDirectory() as td:
            summary_file = pathlib.Path(td) / "summary.json"
            summary_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            rc = check_provider_cost_health.main(
                [
                    "--summary-file",
                    str(summary_file),
                    "--require-providers",
                    "openai,gemini",
                ]
            )
        self.assertEqual(rc, 1)

    def test_evaluate_health_allow_stale_and_no_provider_ok(self):
        payload = {
            "ok": False,
            "providers": [{"provider": "openai", "ok": False, "status": "failed"}],
            "data_freshness": {"age_sec": 999, "stale": True},
        }
        result = check_provider_cost_health.evaluate_health(
            payload,
            max_age_sec=300,
            required_providers=[],
            allow_stale=True,
            allow_unconfigured=False,
            daily_budget_usd=100.0,
            budget_warning_ratio=0.8,
            budget_enforce_hard_stop=True,
        )
        self.assertFalse(result["ok"])
        self.assertIn("summary_ok_false", result["failures"])
        self.assertIn("no_provider_ok", result["failures"])
        self.assertNotIn("freshness_stale", result["failures"])

    def test_evaluate_health_allows_unconfigured_provider_mode(self):
        payload = {
            "ok": False,
            "providers": [
                {"provider": "openai", "ok": False, "status": "skipped"},
                {"provider": "anthropic", "ok": False, "status": "missing_api_key"},
            ],
            "data_freshness": {"age_sec": -1, "stale": True},
            "cost_windows_usd": {"today": 0.0, "last_7d": 0.0},
        }
        result = check_provider_cost_health.evaluate_health(
            payload,
            max_age_sec=300,
            required_providers=[],
            allow_stale=False,
            allow_unconfigured=True,
            daily_budget_usd=100.0,
            budget_warning_ratio=0.8,
            budget_enforce_hard_stop=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider_telemetry_mode"], "unconfigured")
        self.assertEqual(result["failures"], [])
        self.assertIn("summary_ok_false_unconfigured", result["warnings"])

    def test_main_fails_when_budget_exceeded(self):
        payload = {
            "ok": True,
            "timestamp": "2026-02-09T20:00:00+00:00",
            "records_total": 3,
            "providers": [
                {"provider": "openai", "ok": True, "status": "ok"},
            ],
            "cost_windows_usd": {"today": 101.25, "last_7d": 180.0},
            "data_freshness": {"age_sec": 120, "stale": False, "stale_threshold_sec": 900},
        }
        with tempfile.TemporaryDirectory() as td:
            summary_file = pathlib.Path(td) / "summary.json"
            output_file = pathlib.Path(td) / "provider_cost_health.json"
            summary_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            rc = check_provider_cost_health.main(
                [
                    "--summary-file",
                    str(summary_file),
                    "--daily-budget-usd",
                    "100",
                    "--output",
                    str(output_file),
                    "--json",
                ]
            )
            self.assertTrue(output_file.exists())
            health = json.loads(output_file.read_text(encoding="utf-8"))
        self.assertEqual(rc, 1)
        self.assertEqual(health["budget"]["state"], "exceeded")
        self.assertIn("budget_daily_exceeded", " ".join(health["failures"]))

    def test_load_summary_error_paths(self):
        with tempfile.TemporaryDirectory() as td:
            missing = pathlib.Path(td) / "missing.json"
            payload, err = check_provider_cost_health._load_summary(missing)
            self.assertEqual(payload, {})
            self.assertIn("summary_missing", err)

            directory = pathlib.Path(td) / "dir"
            directory.mkdir()
            payload, err = check_provider_cost_health._load_summary(directory)
            self.assertEqual(payload, {})
            self.assertIn("summary_not_file", err)

            broken = pathlib.Path(td) / "broken.json"
            broken.write_text("not-json", encoding="utf-8")
            payload, err = check_provider_cost_health._load_summary(broken)
            self.assertEqual(payload, {})
            self.assertIn("summary_parse_error", err)

            list_payload = pathlib.Path(td) / "list.json"
            list_payload.write_text("[]", encoding="utf-8")
            payload, err = check_provider_cost_health._load_summary(list_payload)
            self.assertEqual(payload, {})
            self.assertEqual(err, "summary_must_be_object")

    def test_main_json_mode_for_missing_summary(self):
        with tempfile.TemporaryDirectory() as td:
            missing = pathlib.Path(td) / "missing.json"
            with mock.patch("builtins.print") as print_mock:
                rc = check_provider_cost_health.main(["--summary-file", str(missing), "--json"])
            self.assertEqual(rc, 1)
            self.assertTrue(print_mock.called)


if __name__ == "__main__":
    unittest.main()
