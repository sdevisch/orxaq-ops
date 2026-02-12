import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_api_interop_policy.py"

module_spec = importlib.util.spec_from_file_location("check_api_interop_policy", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_api_interop_policy = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_api_interop_policy", check_api_interop_policy)
module_spec.loader.exec_module(check_api_interop_policy)


class ApiInteropPolicyCheckTests(unittest.TestCase):
    def _policy(self) -> dict:
        return {
            "schema_version": "api-interop-policy.v1",
            "release_phase": "foundation",
            "activation": {
                "late_stage_only": True,
                "requires_upgrade_controls": True,
                "requires_backlog_tasks": ["B11-T6", "B11-T7", "B9-T4"],
                "requires_api_backlog_tasks": ["B12-T1", "B12-T2", "B12-T3", "B12-T6", "B12-T8"],
            },
            "protocols": {
                "required": [
                    {"id": "rest", "enabled": True},
                    {"id": "mcp", "enabled": True},
                    {"id": "webhook", "enabled": True},
                    {"id": "sse", "enabled": True},
                ],
                "standards": ["openapi", "json_schema", "asyncapi", "cloudevents"],
                "async": {
                    "requires_asyncapi": True,
                    "requires_cloudevents_envelope": True,
                    "required_delivery_guarantees": ["at_least_once", "dead_letter", "replay_safe_ids"],
                },
                "adapters": {
                    "required": ["grpc", "jsonrpc"],
                    "parity_with": ["rest", "mcp"],
                    "deterministic_fallback_required": True,
                },
            },
            "contracts": {
                "openapi_required": True,
                "json_schema_required": True,
                "schema_registry_required": True,
                "compatibility": {
                    "backward_required": True,
                    "forward_required": True,
                    "deprecation_window_days_min": 90,
                    "breaking_change_requires_exemption": True,
                    "exemption_audit_required": True,
                },
            },
            "security": {
                "required_controls": ["oauth2", "oidc", "token_scopes"],
                "tenant_isolation_required": True,
                "rate_limits_required": True,
                "webhook_signing_required": True,
                "breakglass_required_fields": ["reason", "scope", "ttl", "rollback_proof", "audit_trail"],
            },
            "routing_and_execution": {
                "local_first_required": True,
                "cloud_requires_explicit_trigger": True,
                "user_mode_only": True,
                "github_coordination_required": True,
            },
            "observability": {
                "slo_required": True,
                "trace_required": True,
                "developer_diagnostics_required": True,
                "causal_learning_required": True,
                "version_capture_required": True,
                "required_dimensions": [
                    "protocol",
                    "route",
                    "auth_context",
                    "latency_ms",
                    "error_class",
                    "downstream_dependency",
                    "contract_version",
                ],
            },
            "release_gates": {
                "conformance_suite_required": True,
                "compatibility_checks_required": True,
                "sdk_generated_from_contracts_required": True,
                "docs_generated_from_contracts_required": True,
                "block_promotion_on_failure": True,
            },
        }

    def _backlog(self) -> dict:
        return {
            "tasks": [
                {"id": "B10-T3", "dependencies": []},
                {"id": "B11-T6", "dependencies": []},
                {"id": "B11-T7", "dependencies": []},
                {"id": "B9-T4", "dependencies": []},
                {"id": "B12-EPIC", "dependencies": ["B11-T6", "B11-T7", "B9-T4"]},
                {"id": "B12-T1", "dependencies": ["B12-EPIC"]},
                {"id": "B12-T2", "dependencies": ["B12-T1"]},
                {"id": "B12-T3", "dependencies": ["B12-T1", "B12-T2"]},
                {"id": "B12-T4", "dependencies": ["B12-T1"]},
                {"id": "B12-T5", "dependencies": ["B12-T1", "B12-T2"]},
                {"id": "B12-T6", "dependencies": ["B12-T2", "B12-T3", "B12-T4"]},
                {"id": "B12-T7", "dependencies": ["B12-T2", "B12-T3", "B12-T4", "B12-T6"]},
                {"id": "B12-T8", "dependencies": ["B12-T5", "B12-T6", "B12-T7"]},
                {"id": "B12-T9", "dependencies": ["B10-T3", "B12-T2", "B12-T3"]},
            ]
        }

    def test_evaluate_passes_for_valid_payloads(self):
        report = check_api_interop_policy.evaluate(
            policy=self._policy(),
            backlog=self._backlog(),
            backlog_parse_mode="yaml",
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 0)

    def test_evaluate_fails_when_required_protocol_missing(self):
        policy = self._policy()
        policy["protocols"]["required"] = [
            {"id": "rest", "enabled": True},
            {"id": "mcp", "enabled": True},
            {"id": "webhook", "enabled": True},
        ]
        report = check_api_interop_policy.evaluate(
            policy=policy,
            backlog=self._backlog(),
            backlog_parse_mode="yaml",
        )
        self.assertFalse(report["ok"])
        self.assertGreater(report["summary"]["violation_count"], 0)

    def test_evaluate_fails_when_b12_dependencies_missing(self):
        backlog = self._backlog()
        for task in backlog["tasks"]:
            if task.get("id") == "B12-EPIC":
                task["dependencies"] = ["B11-T6", "B11-T7"]
                break
        report = check_api_interop_policy.evaluate(
            policy=self._policy(),
            backlog=backlog,
            backlog_parse_mode="yaml",
        )
        self.assertFalse(report["ok"])
        self.assertGreater(report["summary"]["violation_count"], 0)

    def test_main_writes_report_and_honors_strict(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            policy_file = td_path / "api_interop_policy.json"
            backlog_file = td_path / "distributed_todo.yaml"
            output_file = td_path / "report.json"

            invalid_policy = self._policy()
            invalid_policy["release_gates"]["conformance_suite_required"] = False

            policy_file.write_text(json.dumps(invalid_policy) + "\n", encoding="utf-8")
            backlog_file.write_text("tasks:\n  - id: B12-EPIC\n", encoding="utf-8")

            rc = check_api_interop_policy.main(
                [
                    "--root",
                    td,
                    "--policy-file",
                    str(policy_file),
                    "--backlog-file",
                    str(backlog_file),
                    "--output",
                    str(output_file),
                    "--strict",
                    "--json",
                ]
            )
            self.assertEqual(rc, 1)
            report = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertGreater(report["summary"]["violation_count"], 0)


if __name__ == "__main__":
    unittest.main()
