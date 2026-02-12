import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_t1_basic_model_policy.py"

module_spec = importlib.util.spec_from_file_location("check_t1_basic_model_policy", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_t1_basic_model_policy = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_t1_basic_model_policy", check_t1_basic_model_policy)
module_spec.loader.exec_module(check_t1_basic_model_policy)


class T1BasicModelPolicyCheckTests(unittest.TestCase):
    def _base_policy(self) -> dict:
        return {
            "enabled": True,
            "lookback_hours": 168,
            "basic_task_max_difficulty": 55,
            "t1_models": ["liquid/lfm2.5-1.2b"],
            "t1_model_prefixes": [],
            "monitoring": {
                "require_recent_metrics": True,
                "max_metrics_age_minutes": 240,
                "min_scanned_metrics": 1,
                "max_parse_skip_ratio": 0.2,
                "required_fields": ["task_id", "prompt_difficulty_score", "routing_reason"],
            },
            "escalation": {
                "min_difficulty": 70,
                "routing_reason_allowlist": ["router_quality_override"],
                "notes_regex": ["escalat"],
                "task_allowlist": ["allowed-task"],
                "task_regex_allowlist": ["^exp-"],
            },
            "max_violations": 0,
        }

    def test_detects_violation_for_basic_non_t1_non_escalated(self):
        now = check_t1_basic_model_policy._utc_now_iso()
        metrics = [
            {
                "timestamp": now,
                "task_id": "simple-fix",
                "prompt_difficulty_score": 25,
                "routing_selected_model": "gpt-5.3-codex",
                "routing_reason": "router_disabled",
                "notes": "",
                "summary": "",
            }
        ]
        report = check_t1_basic_model_policy.evaluate_policy(metrics=metrics, policy=self._base_policy())
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 1)

    def test_allows_t1_model_for_basic_task(self):
        now = check_t1_basic_model_policy._utc_now_iso()
        metrics = [
            {
                "timestamp": now,
                "task_id": "simple-fix",
                "prompt_difficulty_score": 20,
                "routing_selected_model": "liquid/lfm2.5-1.2b",
                "routing_reason": "router_disabled",
                "notes": "",
                "summary": "",
            }
        ]
        report = check_t1_basic_model_policy.evaluate_policy(metrics=metrics, policy=self._base_policy())
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 0)

    def test_fails_when_metrics_are_stale(self):
        stale_ts = (datetime.now(UTC) - timedelta(hours=8)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        metrics = [
            {
                "timestamp": stale_ts,
                "task_id": "simple-fix",
                "prompt_difficulty_score": 20,
                "routing_selected_model": "liquid/lfm2.5-1.2b",
                "routing_reason": "router_disabled",
                "notes": "",
                "summary": "",
            }
        ]
        policy = self._base_policy()
        policy["monitoring"]["max_metrics_age_minutes"] = 60
        report = check_t1_basic_model_policy.evaluate_policy(metrics=metrics, policy=policy)
        self.assertFalse(report["ok"])
        self.assertFalse(report["observability"]["ok"])

    def test_allows_stale_metrics_when_no_basic_workload_or_violations(self):
        stale_ts = (datetime.now(UTC) - timedelta(hours=8)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        metrics = [
            {
                "timestamp": stale_ts,
                "task_id": "complex-research-task",
                "prompt_difficulty_score": 95,
                "routing_selected_model": "gpt-5.3-codex",
                "routing_reason": "router_quality_override",
                "notes": "",
                "summary": "",
            }
        ]
        policy = self._base_policy()
        policy["monitoring"]["max_metrics_age_minutes"] = 60
        report = check_t1_basic_model_policy.evaluate_policy(metrics=metrics, policy=policy)
        self.assertTrue(report["ok"])
        self.assertTrue(report["observability"]["ok"])
        self.assertTrue(report["observability"]["idle_freshness_waiver"])

    def test_fails_when_required_fields_missing(self):
        now = check_t1_basic_model_policy._utc_now_iso()
        metrics = [
            {
                "timestamp": now,
                "task_id": "",
                "prompt_difficulty_score": 20,
                "routing_selected_model": "liquid/lfm2.5-1.2b",
                "routing_reason": "router_disabled",
                "notes": "",
                "summary": "",
            }
        ]
        report = check_t1_basic_model_policy.evaluate_policy(metrics=metrics, policy=self._base_policy())
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["telemetry_missing_required_rows"], 1)

    def test_allows_escalated_basic_task(self):
        now = check_t1_basic_model_policy._utc_now_iso()
        metrics = [
            {
                "timestamp": now,
                "task_id": "simple-fix",
                "prompt_difficulty_score": 30,
                "routing_selected_model": "gpt-5.3-codex",
                "routing_reason": "router_quality_override",
                "notes": "",
                "summary": "",
            },
            {
                "timestamp": now,
                "task_id": "exp-special",
                "prompt_difficulty_score": 35,
                "routing_selected_model": "gpt-5.3-codex",
                "routing_reason": "router_disabled",
                "notes": "",
                "summary": "",
            },
            {
                "timestamp": now,
                "task_id": "simple-fix-2",
                "prompt_difficulty_score": 35,
                "routing_selected_model": "gpt-5.3-codex",
                "routing_reason": "router_disabled",
                "notes": "manual escalation approved",
                "summary": "",
            },
        ]
        report = check_t1_basic_model_policy.evaluate_policy(metrics=metrics, policy=self._base_policy())
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 0)

    def test_main_writes_report_and_honors_strict(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            policy_file = td_path / "policy.json"
            metrics_file = td_path / "metrics.ndjson"
            out_file = td_path / "report.json"

            policy = self._base_policy()
            policy_file.write_text(json.dumps(policy) + "\n", encoding="utf-8")

            row = {
                "timestamp": check_t1_basic_model_policy._utc_now_iso(),
                "task_id": "simple-fix",
                "prompt_difficulty_score": 15,
                "routing_selected_model": "gpt-5.3-codex",
                "routing_reason": "router_disabled",
                "notes": "",
                "summary": "",
            }
            metrics_file.write_text(json.dumps(row) + "\n", encoding="utf-8")

            rc = check_t1_basic_model_policy.main(
                [
                    "--root",
                    td,
                    "--policy-file",
                    str(policy_file),
                    "--metrics-file",
                    str(metrics_file),
                    "--output",
                    str(out_file),
                    "--strict",
                    "--json",
                ]
            )
            self.assertEqual(rc, 1)
            report = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["violation_count"], 1)


if __name__ == "__main__":
    unittest.main()
