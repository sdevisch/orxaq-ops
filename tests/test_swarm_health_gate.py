"""Tests for Issue #23: Swarm Health Gate integration across orxaq + orxaq-ops."""

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.swarm_health_gate import (
    BudgetHealthInput,
    HeartbeatHealthInput,
    HealthGateResult,
    ProviderHealthInput,
    TaskHealthInput,
    compute_health_score,
    evaluate_health_gate,
    generate_health_artifacts,
    parse_budget_for_health,
    parse_connectivity_report,
    parse_state_for_task_health,
    run_health_gate,
)


class ProviderScoreTests(unittest.TestCase):
    def test_all_up(self):
        report = compute_health_score(
            providers=ProviderHealthInput(total=3, up=3, required_total=1, required_up=1),
            tasks=TaskHealthInput(total=5, done=5),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(age_sec=10, stale_threshold_sec=300),
        )
        self.assertGreater(report["components"]["providers"]["score"], 90.0)

    def test_required_down_penalized(self):
        report = compute_health_score(
            providers=ProviderHealthInput(total=3, up=2, required_total=2, required_up=1),
            tasks=TaskHealthInput(total=1, done=1),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(age_sec=10, stale_threshold_sec=300),
        )
        # Penalty for required down
        self.assertLess(report["components"]["providers"]["score"], 80.0)

    def test_no_providers_neutral(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(),
        )
        self.assertEqual(report["components"]["providers"]["score"], 50.0)


class TaskScoreTests(unittest.TestCase):
    def test_all_done(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(total=10, done=10),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(),
        )
        self.assertEqual(report["components"]["tasks"]["score"], 100.0)

    def test_blocked_penalizes(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(total=10, done=5, blocked=3, pending=2),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(),
        )
        score = report["components"]["tasks"]["score"]
        # 50% done, minus penalty for blocked
        self.assertLess(score, 50.0)

    def test_no_tasks_full_score(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(),
        )
        self.assertEqual(report["components"]["tasks"]["score"], 100.0)


class BudgetScoreTests(unittest.TestCase):
    def test_violations_zero_score(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(),
            budget=BudgetHealthInput(violations=["runtime exceeded"]),
            heartbeat=HeartbeatHealthInput(),
        )
        self.assertEqual(report["components"]["budget"]["score"], 0.0)

    def test_high_utilization_reduces_score(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(),
            budget=BudgetHealthInput(
                elapsed_sec=3500, max_runtime_sec=3600,
                tokens_used=95000, max_tokens=100000,
            ),
            heartbeat=HeartbeatHealthInput(),
        )
        self.assertLess(report["components"]["budget"]["score"], 50.0)


class HeartbeatScoreTests(unittest.TestCase):
    def test_fresh_heartbeat(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(age_sec=10, stale_threshold_sec=300),
        )
        self.assertEqual(report["components"]["heartbeat"]["score"], 100.0)

    def test_stale_heartbeat_degrades(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(age_sec=600, stale_threshold_sec=300),
        )
        self.assertLess(report["components"]["heartbeat"]["score"], 50.0)

    def test_no_heartbeat_neutral(self):
        report = compute_health_score(
            providers=ProviderHealthInput(),
            tasks=TaskHealthInput(),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(age_sec=-1),
        )
        self.assertEqual(report["components"]["heartbeat"]["score"], 50.0)


class CompositeScoreTests(unittest.TestCase):
    def test_perfect_score(self):
        report = compute_health_score(
            providers=ProviderHealthInput(total=3, up=3, required_total=1, required_up=1),
            tasks=TaskHealthInput(total=5, done=5),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(age_sec=10, stale_threshold_sec=300),
        )
        self.assertGreater(report["score"], 90.0)

    def test_score_never_exceeds_100(self):
        report = compute_health_score(
            providers=ProviderHealthInput(total=1, up=1, required_total=1, required_up=1),
            tasks=TaskHealthInput(total=1, done=1),
            budget=BudgetHealthInput(),
            heartbeat=HeartbeatHealthInput(age_sec=1, stale_threshold_sec=300),
        )
        self.assertLessEqual(report["score"], 100.0)


class HealthGateTests(unittest.TestCase):
    def test_passes_above_threshold(self):
        result = evaluate_health_gate(90.0, min_score=85.0)
        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "all gates passed")

    def test_fails_below_threshold(self):
        result = evaluate_health_gate(75.0, min_score=85.0)
        self.assertFalse(result.passed)
        self.assertIn("below minimum", result.reason)

    def test_fails_on_budget_violations(self):
        result = evaluate_health_gate(
            95.0,
            min_score=85.0,
            budget_violations=["runtime exceeded"],
        )
        self.assertFalse(result.passed)
        self.assertIn("budget violations", result.reason)

    def test_fails_on_required_providers_down(self):
        result = evaluate_health_gate(
            95.0,
            min_score=85.0,
            required_providers_down=1,
        )
        self.assertFalse(result.passed)
        self.assertIn("required providers down", result.reason)

    def test_skips_provider_check_when_not_required(self):
        result = evaluate_health_gate(
            95.0,
            min_score=85.0,
            require_required_providers_up=False,
            required_providers_down=2,
        )
        self.assertTrue(result.passed)

    def test_skips_budget_check_when_not_required(self):
        result = evaluate_health_gate(
            95.0,
            min_score=85.0,
            require_no_budget_violations=False,
            budget_violations=["cost exceeded"],
        )
        self.assertTrue(result.passed)


class ArtifactGenerationTests(unittest.TestCase):
    def test_generates_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = pathlib.Path(td) / "health_artifacts"
            health_report = {
                "score": 92.5,
                "components": {
                    "providers": {"score": 100.0, "weight": 0.30},
                    "tasks": {"score": 80.0, "weight": 0.30},
                    "budget": {"score": 100.0, "weight": 0.20},
                    "heartbeat": {"score": 100.0, "weight": 0.20},
                },
            }
            gate_result = HealthGateResult(
                passed=True, score=92.5, min_score=85.0, reason="all gates passed"
            )
            artifacts = generate_health_artifacts(
                health_report=health_report,
                gate_result=gate_result,
                output_dir=output_dir,
            )
            self.assertIn("json", artifacts)
            self.assertIn("markdown", artifacts)
            json_path = pathlib.Path(artifacts["json"])
            md_path = pathlib.Path(artifacts["markdown"])
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())

            # Validate JSON
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], "swarm-health.v1")
            self.assertEqual(data["health"]["score"], 92.5)
            self.assertTrue(data["gate"]["passed"])

            # Validate Markdown
            md = md_path.read_text(encoding="utf-8")
            self.assertIn("92.5", md)
            self.assertIn("yes", md)


class ParseConnectivityReportTests(unittest.TestCase):
    def test_parses_valid_report(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "providers.json"
            path.write_text(json.dumps({
                "summary": {
                    "provider_total": 4,
                    "provider_up": 3,
                    "required_total": 2,
                    "required_down": 1,
                }
            }), encoding="utf-8")
            result = parse_connectivity_report(path)
            self.assertEqual(result.total, 4)
            self.assertEqual(result.up, 3)
            self.assertEqual(result.required_total, 2)
            self.assertEqual(result.required_up, 1)

    def test_missing_file_returns_empty(self):
        result = parse_connectivity_report(pathlib.Path("/nonexistent/file.json"))
        self.assertEqual(result.total, 0)


class ParseStateTests(unittest.TestCase):
    def test_parses_state(self):
        state = {
            "t1": {"status": "done"},
            "t2": {"status": "blocked"},
            "t3": {"status": "pending"},
            "t4": {"status": "in_progress"},
        }
        result = parse_state_for_task_health(state)
        self.assertEqual(result.total, 4)
        self.assertEqual(result.done, 1)
        self.assertEqual(result.blocked, 1)
        self.assertEqual(result.pending, 2)  # pending + in_progress

    def test_ignores_non_dict(self):
        state = {"t1": "not-a-dict", "t2": {"status": "done"}}
        result = parse_state_for_task_health(state)
        self.assertEqual(result.total, 1)


class ParseBudgetTests(unittest.TestCase):
    def test_parses_budget(self):
        budget = {
            "elapsed_sec": 100,
            "totals": {"tokens": 5000, "cost_usd": 0.5},
            "limits": {"max_runtime_sec": 3600, "max_total_tokens": 100000, "max_total_cost_usd": 15.0},
            "violations": ["token warning"],
        }
        result = parse_budget_for_health(budget)
        self.assertEqual(result.elapsed_sec, 100)
        self.assertEqual(result.tokens_used, 5000)
        self.assertEqual(result.max_cost_usd, 15.0)
        self.assertEqual(result.violations, ["token warning"])


class RunHealthGateEndToEndTests(unittest.TestCase):
    def test_full_pipeline_passes(self):
        result = run_health_gate(
            state={"t1": {"status": "done"}, "t2": {"status": "done"}},
            budget={
                "elapsed_sec": 100,
                "totals": {"tokens": 500, "cost_usd": 0.1},
                "limits": {"max_runtime_sec": 3600, "max_total_tokens": 100000, "max_total_cost_usd": 15.0},
            },
            heartbeat_age_sec=10,
            min_score=50.0,
        )
        self.assertTrue(result["gate"]["passed"])
        self.assertGreater(result["health"]["score"], 50.0)

    def test_full_pipeline_fails_on_violations(self):
        result = run_health_gate(
            budget={
                "violations": ["runtime exceeded"],
                "elapsed_sec": 999,
                "totals": {"tokens": 0, "cost_usd": 0.0},
                "limits": {},
            },
            min_score=50.0,
        )
        self.assertFalse(result["gate"]["passed"])
        self.assertIn("budget violations", result["gate"]["reason"])

    def test_full_pipeline_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = pathlib.Path(td) / "output"
            result = run_health_gate(
                state={"t1": {"status": "done"}},
                min_score=50.0,
                output_dir=output_dir,
            )
            self.assertIn("json", result["artifacts"])
            self.assertTrue(pathlib.Path(result["artifacts"]["json"]).exists())

    def test_full_pipeline_no_artifacts_when_no_output_dir(self):
        result = run_health_gate(
            state={"t1": {"status": "done"}},
            min_score=50.0,
        )
        self.assertEqual(result["artifacts"], {})


if __name__ == "__main__":
    unittest.main()
