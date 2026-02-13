"""Tests for Issue #15: Deterministic baseline metrics pipeline."""

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.baseline_metrics import (
    BaselineMetricsCollector,
    classify_failure,
    extract_failure_signatures,
    parse_budget_for_metrics,
    parse_state_for_retry_metrics,
    _percentile,
)


class ClassifyFailureTests(unittest.TestCase):
    def test_timeout(self):
        self.assertEqual(classify_failure("operation timed out"), "timeout")
        self.assertEqual(classify_failure("deadline exceeded for request"), "timeout")

    def test_rate_limit(self):
        self.assertEqual(classify_failure("HTTP 429 Too Many Requests"), "rate_limit")

    def test_network(self):
        self.assertEqual(classify_failure("network connection reset"), "network")

    def test_validation(self):
        self.assertEqual(classify_failure("assertion error in test suite"), "validation")

    def test_git_lock(self):
        self.assertEqual(classify_failure("Unable to create index.lock"), "git_lock")

    def test_auth(self):
        self.assertEqual(classify_failure("401 Unauthorized"), "auth")

    def test_unknown(self):
        self.assertEqual(classify_failure("some random error"), "unknown")


class ExtractFailureSignaturesTests(unittest.TestCase):
    def test_aggregates_by_category(self):
        errors = [
            "timeout waiting for response",
            "operation timed out",
            "HTTP 429 rate limit",
            "random error",
        ]
        sigs = extract_failure_signatures(errors)
        categories = {s.category for s in sigs}
        self.assertIn("timeout", categories)
        self.assertIn("rate_limit", categories)
        self.assertIn("unknown", categories)
        timeout_sig = next(s for s in sigs if s.category == "timeout")
        self.assertEqual(timeout_sig.count, 2)

    def test_empty_errors(self):
        self.assertEqual(extract_failure_signatures([]), [])


class PercentileTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_percentile([], 50), 0.0)

    def test_single(self):
        self.assertEqual(_percentile([42.0], 50), 42.0)

    def test_p50_even_list(self):
        data = [10.0, 20.0, 30.0, 40.0]
        p50 = _percentile(data, 50)
        self.assertAlmostEqual(p50, 25.0)

    def test_p95_small_list(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        p95 = _percentile(data, 95)
        self.assertGreater(p95, 4.0)


class CollectorTests(unittest.TestCase):
    def test_record_validation(self):
        c = BaselineMetricsCollector()
        c.record_validation("make test", 1200.5, 0, True)
        c.record_validation("make lint", 300.0, 1, False)
        self.assertEqual(len(c.validation_metrics), 2)
        self.assertTrue(c.validation_metrics[0].passed)
        self.assertFalse(c.validation_metrics[1].passed)

    def test_record_retry(self):
        c = BaselineMetricsCollector()
        c.record_retry("task-1", 2, True, 5.0, "timeout waiting")
        self.assertEqual(len(c.retry_metrics), 1)
        self.assertEqual(c.retry_metrics[0].error_signature, "timeout")
        self.assertEqual(len(c.errors), 1)

    def test_record_lane_timing(self):
        c = BaselineMetricsCollector()
        c.record_lane_timing("L0", "task-1", 50.0, 1200.0)
        c.record_lane_timing("L0", "task-2", 20.0, 800.0)
        c.record_lane_timing("L1", "task-3", 100.0, 3000.0)
        self.assertEqual(len(c.lane_timing_metrics), 3)

    def test_aggregate_produces_valid_schema(self):
        c = BaselineMetricsCollector()
        c.record_validation("pytest", 500.0, 0, True)
        c.record_validation("ruff", 100.0, 0, True)
        c.record_retry("t1", 1, True, 5.0, "timeout")
        c.record_lane_timing("L0", "t1", 10.0, 400.0)
        report = c.aggregate()

        self.assertEqual(report["schema_version"], "baseline-metrics.v1")
        self.assertIn("generated_at_utc", report)
        self.assertEqual(report["validation"]["total"], 2)
        self.assertEqual(report["validation"]["passed"], 2)
        self.assertEqual(report["retries"]["total"], 1)
        self.assertEqual(report["retries"]["retryable"], 1)
        self.assertIn("L0", report["lane_timing"])
        self.assertEqual(report["lane_timing"]["L0"]["count"], 1)

    def test_aggregate_with_no_data(self):
        c = BaselineMetricsCollector()
        report = c.aggregate()
        self.assertEqual(report["validation"]["total"], 0)
        self.assertEqual(report["retries"]["total"], 0)
        self.assertEqual(report["lane_timing"], {})
        self.assertEqual(report["failure_signatures"], [])

    def test_flush_writes_json(self):
        c = BaselineMetricsCollector()
        c.record_validation("pytest", 200.0, 0, True)
        with tempfile.TemporaryDirectory() as td:
            path = c.flush(pathlib.Path(td) / "metrics")
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], "baseline-metrics.v1")
            self.assertEqual(data["validation"]["total"], 1)


class ParseStateTests(unittest.TestCase):
    def test_extracts_retry_relevant_tasks(self):
        state = {
            "t1": {"status": "done", "attempts": 3, "retryable_failures": 1, "last_error": "timeout"},
            "t2": {"status": "pending", "attempts": 0, "retryable_failures": 0, "last_error": ""},
            "t3": {"status": "blocked", "attempts": 2, "retryable_failures": 2, "last_error": "rate limit"},
        }
        results = parse_state_for_retry_metrics(state)
        self.assertEqual(len(results), 2)  # t1 and t3 have attempts > 0
        task_ids = {r["task_id"] for r in results}
        self.assertEqual(task_ids, {"t1", "t3"})

    def test_handles_non_dict_entries(self):
        state = {"bad": "not-a-dict", "ok": {"attempts": 1, "retryable_failures": 0}}
        results = parse_state_for_retry_metrics(state)
        self.assertEqual(len(results), 1)


class ParseBudgetTests(unittest.TestCase):
    def test_extracts_kpi_fields(self):
        budget = {
            "elapsed_sec": 120,
            "totals": {"tokens": 5000, "cost_usd": 0.5, "retry_events": 2},
            "limits": {"max_runtime_sec": 3600, "max_total_tokens": 100000},
            "violations": [],
        }
        result = parse_budget_for_metrics(budget)
        self.assertEqual(result["elapsed_sec"], 120)
        self.assertEqual(result["tokens_used"], 5000)
        self.assertEqual(result["cost_usd"], 0.5)
        self.assertEqual(result["violations"], [])

    def test_handles_empty_budget(self):
        result = parse_budget_for_metrics({})
        self.assertEqual(result["elapsed_sec"], 0)
        self.assertEqual(result["tokens_used"], 0)


if __name__ == "__main__":
    unittest.main()
