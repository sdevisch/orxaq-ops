import importlib.util
import json
import pathlib
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_session_autonomy_privileges.py"

module_spec = importlib.util.spec_from_file_location("check_session_autonomy_privileges", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
check_session_autonomy_privileges = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("check_session_autonomy_privileges", check_session_autonomy_privileges)
module_spec.loader.exec_module(check_session_autonomy_privileges)


class SessionAutonomyPrivilegesCheckTests(unittest.TestCase):
    def _agents_file(self, root: pathlib.Path) -> pathlib.Path:
        path = root / "AGENTS.md"
        path.write_text(
            textwrap.dedent(
                """
                # Workspace Preferences

                `policy_version`: `2.1`

                ### Active Entry
                - `identity_id`: `codex.gpt5.high.v1`
                - `agent_name`: `Codex`
                - `model_family`: `GPT-5`
                - `capability_tier`: `high`
                - `identity_fingerprint`: `abc123`
                - `runtime_markers_required`:
                  - `approval_policy=never`
                  - `sandbox_mode=danger-full-access`
                  - `execution_context=codex_desktop_or_cli`
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_evaluate_session_privileges_passes_for_matching_extra_high_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            agents_file = self._agents_file(root)
            policy = check_session_autonomy_privileges.load_agents_policy(agents_file)
            report = check_session_autonomy_privileges.evaluate_session_privileges(
                policy=policy,
                runtime={
                    "identity_id": "codex.gpt5.high.v1",
                    "agent_name": "Codex",
                    "model_family": "GPT-5",
                    "capability_tier": "high",
                    "identity_fingerprint": "abc123",
                    "execution_profile": "extra-high",
                    "user_grant_detected": True,
                    "critical_security_decision": False,
                    "runtime_markers": {
                        "approval_policy": "never",
                        "sandbox_mode": "danger-full-access",
                        "execution_context": "codex_desktop_or_cli",
                    },
                },
            )
            self.assertTrue(report["ok"])
            self.assertTrue(report["autonomy_authorized"])
            self.assertEqual(report["execution_profile"], "extra_high")
            self.assertTrue(report["effective_privileges"]["force_continuation"])
            self.assertTrue(report["effective_privileges"]["assume_true_full_autonomy"])
            self.assertTrue(report["effective_privileges"]["queue_persistent_mode"])

    def test_evaluate_session_privileges_fails_on_runtime_marker_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            agents_file = self._agents_file(root)
            policy = check_session_autonomy_privileges.load_agents_policy(agents_file)
            report = check_session_autonomy_privileges.evaluate_session_privileges(
                policy=policy,
                runtime={
                    "identity_id": "codex.gpt5.high.v1",
                    "agent_name": "Codex",
                    "model_family": "GPT-5",
                    "capability_tier": "high",
                    "identity_fingerprint": "abc123",
                    "execution_profile": "extra_high",
                    "user_grant_detected": True,
                    "critical_security_decision": False,
                    "runtime_markers": {
                        "approval_policy": "on-request",
                        "sandbox_mode": "danger-full-access",
                        "execution_context": "codex_desktop_or_cli",
                    },
                },
            )
            self.assertFalse(report["ok"])
            self.assertFalse(report["runtime_marker_match"])
            self.assertFalse(report["autonomy_authorized"])

    def test_main_strict_returns_nonzero_when_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            agents_file = self._agents_file(root)
            output_file = root / "artifacts" / "autonomy" / "session_autonomy_privileges.json"
            rc = check_session_autonomy_privileges.main(
                [
                    "--root",
                    str(root),
                    "--agents-file",
                    str(agents_file),
                    "--output",
                    str(output_file),
                    "--identity-id",
                    "codex.gpt6.high.v1",
                    "--agent-name",
                    "Codex",
                    "--model-family",
                    "GPT-5",
                    "--capability-tier",
                    "high",
                    "--identity-fingerprint",
                    "abc123",
                    "--execution-profile",
                    "extra_high",
                    "--approval-policy",
                    "never",
                    "--sandbox-mode",
                    "danger-full-access",
                    "--execution-context",
                    "codex_desktop_or_cli",
                    "--user-grant-detected",
                    "--strict",
                    "--json",
                ]
            )
            self.assertEqual(rc, 1)
            report = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertFalse(report["identity_match"])


if __name__ == "__main__":
    unittest.main()
