import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_backend_upgrade_policy.py"

module_spec = importlib.util.spec_from_file_location("check_backend_upgrade_policy", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_backend_upgrade_policy = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_backend_upgrade_policy", check_backend_upgrade_policy)
module_spec.loader.exec_module(check_backend_upgrade_policy)


class BackendUpgradePolicyCheckTests(unittest.TestCase):
    def _backend_policy(self) -> dict:
        return {
            "schema_version": "backend-portfolio-policy.v1",
            "release_phase": "foundation",
            "portfolio": {
                "backends": [
                    {"id": "spark", "supports_user_mode": True},
                    {"id": "dask", "supports_user_mode": True},
                    {"id": "spark_jit_numpy_hybrid", "supports_user_mode": True},
                ],
                "coding_variants": [
                    {"id": "pandas"},
                    {"id": "numpy"},
                    {"id": "jit_numpy"},
                    {"id": "narwhals"},
                    {"id": "ibis"},
                ],
            },
            "routing": {
                "specialist_router_required": True,
                "cloud_escalation_requires_trigger": True,
                "criteria": [
                    "latency",
                    "throughput",
                    "cost",
                    "determinism",
                    "correctness",
                    "memory_pressure",
                    "startup_overhead",
                    "operability",
                    "failure_blast_radius",
                ],
            },
            "distributed_mode": {
                "github_coordination_required": True,
                "user_mode_only": True,
                "organization_opt_in_compute": {
                    "enabled": True,
                    "required_fields": [
                        "user_id",
                        "machine_id",
                        "consent_reason",
                        "granted_at",
                        "expires_at",
                        "revoked",
                        "revoked_at",
                        "revoke_reason",
                    ],
                },
            },
            "os_parity": {"required_os": ["macos", "windows"]},
            "telemetry": {
                "require_version_capture": True,
                "required_dimensions": [
                    "backend_id",
                    "coding_variant",
                    "dataset_profile",
                    "package_versions",
                    "latency_ms",
                    "quality_score",
                ],
            },
            "causal_learning": {
                "enabled": True,
                "require_hypothesis_ids": True,
            },
        }

    def _upgrade_policy(self) -> dict:
        return {
            "schema_version": "upgrade-lifecycle-policy.v1",
            "activation": {
                "routing_mechanism_required": True,
                "ab_testing_required": True,
                "requires_backlog_tasks": ["B9-T2", "B10-T7", "B10-T8"],
            },
            "state_machine": {
                "ordered_states": [
                    "preflight",
                    "shadow",
                    "canary",
                    "ramp",
                    "steady",
                    "deprecate",
                    "retire",
                ],
                "invalid_transition_action": "block",
            },
            "rollout": {
                "strategies": ["shadow", "canary", "weighted_ramp"],
                "weight_steps_percent": [1, 5, 10, 25, 50, 100],
            },
            "scale": {
                "old_new_coexistence_required": True,
                "graceful_scale_down_required": True,
                "rollback_headroom_percent_min": 25,
            },
            "migration": {
                "schema_contract_required": True,
                "forward_backward_compat_required": True,
                "reversible_when_feasible": True,
            },
            "safety": {
                "automatic_pause_on_warning": True,
                "automatic_rollback_on_hard_fail": True,
                "breakglass_required_fields": [
                    "reason",
                    "scope",
                    "ttl",
                    "rollback_proof",
                    "audit_trail",
                ],
            },
            "environment": {
                "required_targets": ["macos", "windows", "local", "cloud"],
                "cloud_requires_explicit_trigger": True,
            },
        }

    def _backlog(self) -> dict:
        return {
            "tasks": [
                {"id": "B9-T2", "dependencies": []},
                {"id": "B10-EPIC", "dependencies": []},
                {"id": "B10-T7", "dependencies": ["B10-T5", "B10-T6", "B3-EPIC"]},
                {"id": "B10-T8", "dependencies": ["B10-T5", "B5-EPIC"]},
                {"id": "B11-EPIC", "dependencies": ["B9-T2", "B10-T7", "B10-T8"]},
                {"id": "B11-T2", "dependencies": ["B11-T1", "B9-T2", "B10-T7"]},
                {"id": "B11-T4", "dependencies": ["B11-T2", "B11-T3", "B0-T5"]},
                {"id": "B11-T5", "dependencies": ["B11-T2", "B11-T4", "B0-T3"]},
            ]
        }

    def test_evaluate_passes_for_valid_payloads(self):
        report = check_backend_upgrade_policy.evaluate(
            backend_policy=self._backend_policy(),
            upgrade_policy=self._upgrade_policy(),
            backlog=self._backlog(),
            backlog_parse_mode="yaml",
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 0)

    def test_evaluate_fails_when_required_backend_missing(self):
        backend = self._backend_policy()
        backend["portfolio"]["backends"] = [
            {"id": "spark", "supports_user_mode": True},
            {"id": "dask", "supports_user_mode": True},
        ]
        report = check_backend_upgrade_policy.evaluate(
            backend_policy=backend,
            upgrade_policy=self._upgrade_policy(),
            backlog=self._backlog(),
            backlog_parse_mode="yaml",
        )
        self.assertFalse(report["ok"])
        self.assertGreater(report["summary"]["violation_count"], 0)

    def test_evaluate_fails_when_upgrade_dependency_missing(self):
        backlog = self._backlog()
        for task in backlog["tasks"]:
            if task.get("id") == "B11-EPIC":
                task["dependencies"] = ["B10-T7", "B10-T8"]
                break
        report = check_backend_upgrade_policy.evaluate(
            backend_policy=self._backend_policy(),
            upgrade_policy=self._upgrade_policy(),
            backlog=backlog,
            backlog_parse_mode="yaml",
        )
        self.assertFalse(report["ok"])
        self.assertGreater(report["summary"]["violation_count"], 0)

    def test_main_writes_report_and_honors_strict(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            backend_policy_file = td_path / "backend_policy.json"
            upgrade_policy_file = td_path / "upgrade_policy.json"
            backlog_file = td_path / "distributed_todo.yaml"
            output_file = td_path / "report.json"

            invalid_backend = self._backend_policy()
            invalid_backend["routing"]["criteria"] = ["latency"]

            backend_policy_file.write_text(json.dumps(invalid_backend) + "\n", encoding="utf-8")
            upgrade_policy_file.write_text(json.dumps(self._upgrade_policy()) + "\n", encoding="utf-8")
            backlog_file.write_text("tasks:\n  - id: B9-T2\n", encoding="utf-8")

            rc = check_backend_upgrade_policy.main(
                [
                    "--root",
                    td,
                    "--backend-policy-file",
                    str(backend_policy_file),
                    "--upgrade-policy-file",
                    str(upgrade_policy_file),
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
