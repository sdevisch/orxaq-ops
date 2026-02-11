import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import breakglass


class BreakglassTests(unittest.TestCase):
    def test_open_validate_close_records_hash_chain(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            opened = breakglass.open_session(
                root,
                scopes=["allow_protected_branch_commits"],
                reason="incident-123",
                ttl_sec=120,
                actor="tester",
                token="test-token",
            )
            self.assertTrue(opened["ok"])
            self.assertTrue(opened["active"])
            self.assertTrue(opened["valid"])

            ok, message = breakglass.validate_scope(
                root,
                scope="allow_protected_branch_commits",
                actor="validator",
                token="test-token",
                context="unit-test",
            )
            self.assertTrue(ok)
            self.assertIn("granted", message)

            closed = breakglass.close_session(
                root,
                actor="tester",
                reason="resolved",
                token="test-token",
                require_token=True,
            )
            self.assertTrue(closed["ok"])
            self.assertFalse(closed["active"])

            ledger_path = pathlib.Path(opened["ledger_file"])
            lines = [line.strip() for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 3)
            entries = [json.loads(line) for line in lines]
            self.assertEqual(entries[0]["event"], "open")
            self.assertEqual(entries[1]["event"], "authorize")
            self.assertEqual(entries[2]["event"], "close")
            self.assertEqual(entries[0]["prev_hash"], breakglass.GENESIS_HASH)
            self.assertEqual(entries[1]["prev_hash"], entries[0]["entry_hash"])
            self.assertEqual(entries[2]["prev_hash"], entries[1]["entry_hash"])

    def test_validate_scope_requires_token(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict("os.environ", {}, clear=True):
            root = pathlib.Path(td)
            breakglass.open_session(
                root,
                scopes=["allow_behind_push"],
                reason="incident-456",
                ttl_sec=120,
                actor="tester",
                token="known-token",
            )
            ok, message = breakglass.validate_scope(root, scope="allow_behind_push")
            self.assertFalse(ok)
            self.assertIn(breakglass.TOKEN_ENV, message)

    def test_validate_scope_rejects_scope_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            breakglass.open_session(
                root,
                scopes=["allow_agent_branch_reuse"],
                reason="incident-789",
                ttl_sec=120,
                actor="tester",
                token="known-token",
            )
            ok, message = breakglass.validate_scope(
                root,
                scope="allow_behind_push",
                token="known-token",
            )
            self.assertFalse(ok)
            self.assertIn("not enabled", message)

    def test_close_session_rejects_wrong_token_when_required(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            breakglass.open_session(
                root,
                scopes=["allow_behind_push"],
                reason="incident",
                ttl_sec=120,
                actor="tester",
                token="known-token",
            )
            with self.assertRaises(ValueError):
                breakglass.close_session(root, token="wrong", require_token=True)


if __name__ == "__main__":
    unittest.main()
