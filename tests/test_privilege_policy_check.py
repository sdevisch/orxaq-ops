import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_privilege_policy.py"

module_spec = importlib.util.spec_from_file_location("check_privilege_policy", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_privilege_policy = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_privilege_policy", check_privilege_policy)
module_spec.loader.exec_module(check_privilege_policy)


class PrivilegePolicyCheckTests(unittest.TestCase):
    def _base_policy(self) -> dict:
        return {
            "schema_version": "privilege-policy.v1",
            "default_mode": "least_privilege",
            "providers": {
                "codex": {
                    "least_privilege_args": ["--sandbox", "workspace-write"],
                    "elevated_args": ["--dangerously-bypass-approvals-and-sandbox"],
                }
            },
            "breakglass": {
                "enabled": True,
                "max_ttl_minutes": 120,
                "required_fields": [
                    "grant_id",
                    "reason",
                    "scope",
                    "requested_by",
                    "approved_by",
                    "issued_at",
                    "expires_at",
                    "rollback_proof",
                    "providers",
                ],
            },
            "monitoring": {
                "require_recent_events": True,
                "max_event_age_minutes": 240,
                "min_scanned_events": 1,
            },
        }

    def _now_iso(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def test_passes_with_recent_least_privilege_event(self):
        events = [
            {
                "timestamp": self._now_iso(),
                "task_id": "t1",
                "provider": "codex",
                "mode": "least_privilege",
                "command_args": ["--sandbox", "workspace-write"],
            }
        ]
        report = check_privilege_policy.evaluate_policy(
            policy=self._base_policy(),
            events=events,
            active_grant={},
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 0)

    def test_fails_when_least_privilege_contains_elevated_flag(self):
        events = [
            {
                "timestamp": self._now_iso(),
                "task_id": "t1",
                "provider": "codex",
                "mode": "least_privilege",
                "command_args": ["--dangerously-bypass-approvals-and-sandbox"],
            }
        ]
        report = check_privilege_policy.evaluate_policy(
            policy=self._base_policy(),
            events=events,
            active_grant={},
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["violation_count"], 1)

    def test_fails_when_breakglass_event_missing_evidence(self):
        events = [
            {
                "timestamp": self._now_iso(),
                "task_id": "t1",
                "provider": "codex",
                "mode": "breakglass_elevated",
                "command_args": ["--dangerously-bypass-approvals-and-sandbox"],
                "grant_id": "",
                "grant": {},
            }
        ]
        report = check_privilege_policy.evaluate_policy(
            policy=self._base_policy(),
            events=events,
            active_grant={},
        )
        self.assertFalse(report["ok"])
        self.assertGreaterEqual(report["summary"]["violation_count"], 1)

    def test_fails_when_latest_event_is_stale(self):
        stale_ts = (datetime.now(UTC) - timedelta(hours=8)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        events = [
            {
                "timestamp": stale_ts,
                "task_id": "t1",
                "provider": "codex",
                "mode": "least_privilege",
                "command_args": ["--sandbox", "workspace-write"],
            }
        ]
        policy = self._base_policy()
        policy["monitoring"]["max_event_age_minutes"] = 30
        report = check_privilege_policy.evaluate_policy(
            policy=policy,
            events=events,
            active_grant={},
        )
        self.assertFalse(report["ok"])
        self.assertFalse(report["summary"]["freshness_ok"])

    def test_main_writes_report_and_honors_strict(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            policy_file = td_path / "policy.json"
            events_file = td_path / "events.ndjson"
            grant_file = td_path / "grant.json"
            out_file = td_path / "report.json"

            policy_file.write_text(json.dumps(self._base_policy()) + "\n", encoding="utf-8")
            events_file.write_text(
                json.dumps(
                    {
                        "timestamp": self._now_iso(),
                        "task_id": "t1",
                        "provider": "codex",
                        "mode": "least_privilege",
                        "command_args": ["--dangerously-bypass-approvals-and-sandbox"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            grant_file.write_text("{}\n", encoding="utf-8")

            rc = check_privilege_policy.main(
                [
                    "--root",
                    td,
                    "--policy-file",
                    str(policy_file),
                    "--audit-log-file",
                    str(events_file),
                    "--active-grant-file",
                    str(grant_file),
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
