import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "deterministic_backlog_control.py"

module_spec = importlib.util.spec_from_file_location("deterministic_backlog_control", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
deterministic_backlog_control = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("deterministic_backlog_control", deterministic_backlog_control)
module_spec.loader.exec_module(deterministic_backlog_control)


class DeterministicBacklogControlTests(unittest.TestCase):
    def _write_json(self, path: pathlib.Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_backlog(self, path: pathlib.Path, tasks: list[dict]) -> None:
        try:
            import yaml  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"yaml runtime unavailable: {exc}") from exc
        payload = {"cycle_id": "test-cycle", "tasks": tasks}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def _base_policy(self, backlog_file: pathlib.Path, markers_dir: pathlib.Path) -> dict:
        return {
            "schema_version": "deterministic-backlog-control.v1",
            "backlog_file": str(backlog_file),
            "ready_statuses": ["todo", "doing", "review"],
            "completion": {
                "enabled": True,
                "eligible_statuses": ["todo", "doing", "review", "blocked"],
                "markers_dir": str(markers_dir),
                "max_complete_per_cycle": 4,
                "require_task_id_match": True,
            },
            "bounds": {
                "min_ready": 2,
                "target_ready": 2,
                "max_ready": 2,
                "max_activate_per_cycle": 2,
                "max_deactivate_per_cycle": 2,
            },
            "throttle": {"blocked_reason": "deterministic_backlog_throttle"},
        }

    def test_throttle_when_ready_above_max(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "orxaq-ops"
            backlog = pathlib.Path(td) / "orxaq" / "ops" / "backlog" / "distributed_todo.yaml"
            markers = root / "artifacts" / "autonomy" / "task_markers"
            self._write_backlog(
                backlog,
                [
                    {"id": "T1", "status": "doing", "priority_band": "P0", "priority_score": 10.0},
                    {"id": "T2", "status": "todo", "priority_band": "P1", "priority_score": 8.0},
                    {"id": "T3", "status": "todo", "priority_band": "P3", "priority_score": 1.0},
                ],
            )
            policy = self._base_policy(backlog, markers)
            policy_file = root / "config" / "deterministic_backlog_policy.json"
            self._write_json(policy_file, policy)
            output_file = root / "artifacts" / "autonomy" / "deterministic_backlog_health.json"
            history_file = root / "artifacts" / "autonomy" / "deterministic_backlog_history.ndjson"

            rc = deterministic_backlog_control.main(
                [
                    "--root",
                    str(root),
                    "--policy-file",
                    str(policy_file),
                    "--output-file",
                    str(output_file),
                    "--history-file",
                    str(history_file),
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(rc, 0)
            report = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["summary"]["ready_after"], 2)
            self.assertGreaterEqual(report["summary"]["action_counts"].get("throttle", 0), 1)

    def test_release_when_ready_below_min(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "orxaq-ops"
            backlog = pathlib.Path(td) / "orxaq" / "ops" / "backlog" / "distributed_todo.yaml"
            markers = root / "artifacts" / "autonomy" / "task_markers"
            self._write_backlog(
                backlog,
                [
                    {"id": "T1", "status": "doing", "priority_band": "P0", "priority_score": 10.0},
                    {
                        "id": "T2",
                        "status": "blocked",
                        "priority_band": "P1",
                        "priority_score": 8.0,
                        "backlog_control": {"throttled": True},
                    },
                ],
            )
            policy = self._base_policy(backlog, markers)
            policy_file = root / "config" / "deterministic_backlog_policy.json"
            self._write_json(policy_file, policy)
            output_file = root / "artifacts" / "autonomy" / "deterministic_backlog_health.json"
            history_file = root / "artifacts" / "autonomy" / "deterministic_backlog_history.ndjson"

            rc = deterministic_backlog_control.main(
                [
                    "--root",
                    str(root),
                    "--policy-file",
                    str(policy_file),
                    "--output-file",
                    str(output_file),
                    "--history-file",
                    str(history_file),
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(rc, 0)
            report = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["summary"]["ready_after"], 2)
            self.assertGreaterEqual(report["summary"]["action_counts"].get("release", 0), 1)

    def test_complete_from_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "orxaq-ops"
            backlog = pathlib.Path(td) / "orxaq" / "ops" / "backlog" / "distributed_todo.yaml"
            markers = root / "artifacts" / "autonomy" / "task_markers"
            self._write_backlog(
                backlog,
                [
                    {"id": "T1", "status": "doing", "priority_band": "P0", "priority_score": 10.0},
                    {"id": "T2", "status": "todo", "priority_band": "P1", "priority_score": 8.0},
                ],
            )
            markers.mkdir(parents=True, exist_ok=True)
            (markers / "T1.done.json").write_text(
                json.dumps({"task_id": "T1", "complete": True}) + "\n",
                encoding="utf-8",
            )

            policy = self._base_policy(backlog, markers)
            policy["bounds"] = {
                "min_ready": 1,
                "target_ready": 1,
                "max_ready": 2,
                "max_activate_per_cycle": 2,
                "max_deactivate_per_cycle": 2,
            }
            policy_file = root / "config" / "deterministic_backlog_policy.json"
            self._write_json(policy_file, policy)
            output_file = root / "artifacts" / "autonomy" / "deterministic_backlog_health.json"
            history_file = root / "artifacts" / "autonomy" / "deterministic_backlog_history.ndjson"

            rc = deterministic_backlog_control.main(
                [
                    "--root",
                    str(root),
                    "--policy-file",
                    str(policy_file),
                    "--output-file",
                    str(output_file),
                    "--history-file",
                    str(history_file),
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(rc, 0)
            report = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertGreaterEqual(report["summary"]["action_counts"].get("complete", 0), 1)
            self.assertEqual(report["summary"]["done_after"], 1)


if __name__ == "__main__":
    unittest.main()
