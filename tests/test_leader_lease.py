import datetime as dt
import os
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.leader_lease import (
    LeaderLeaseConfig,
    acquire_or_renew_lease,
)


class LeaderLeaseTests(unittest.TestCase):
    def test_acquire_then_renew_keeps_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lease_file = root / "state" / "event_mesh" / "leader_lease.json"
            cfg = LeaderLeaseConfig(root_dir=root, node_id="node-a", lease_file=lease_file, ttl_sec=30, backend="file")
            acquired = acquire_or_renew_lease(cfg)
            renewed = acquire_or_renew_lease(cfg)
            self.assertTrue(acquired["is_leader"])
            self.assertEqual(acquired["outcome"], "acquired")
            self.assertEqual(int(acquired["epoch"]), 1)
            self.assertTrue(renewed["is_leader"])
            self.assertEqual(renewed["outcome"], "renewed")
            self.assertEqual(int(renewed["epoch"]), 1)

    def test_active_other_leader_keeps_follower(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lease_file = root / "state" / "event_mesh" / "leader_lease.json"
            cfg_a = LeaderLeaseConfig(root_dir=root, node_id="node-a", lease_file=lease_file, ttl_sec=45, backend="file")
            cfg_b = LeaderLeaseConfig(root_dir=root, node_id="node-b", lease_file=lease_file, ttl_sec=45, backend="file")
            first = acquire_or_renew_lease(cfg_a)
            follower = acquire_or_renew_lease(cfg_b)
            self.assertTrue(first["is_leader"])
            self.assertFalse(follower["is_leader"])
            self.assertEqual(follower["leader_id"], "node-a")
            self.assertEqual(int(follower["epoch"]), int(first["epoch"]))
            self.assertEqual(follower["outcome"], "follower")

    def test_expired_lease_can_be_reacquired_with_incremented_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lease_file = root / "state" / "event_mesh" / "leader_lease.json"
            cfg_a = LeaderLeaseConfig(root_dir=root, node_id="node-a", lease_file=lease_file, ttl_sec=45, backend="file")
            cfg_b = LeaderLeaseConfig(root_dir=root, node_id="node-b", lease_file=lease_file, ttl_sec=45, backend="file")
            first = acquire_or_renew_lease(cfg_a)
            expired_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
            lease_file.parent.mkdir(parents=True, exist_ok=True)
            lease_file.write_text(
                (
                    "{\n"
                    '  "leader_id": "node-a",\n'
                    f'  "epoch": {int(first["epoch"])},\n'
                    f'  "lease_expires_at": "{expired_at}",\n'
                    '  "ttl_sec": 45,\n'
                    '  "updated_at": "2026-01-01T00:00:00+00:00"\n'
                    "}\n"
                ),
                encoding="utf-8",
            )
            second = acquire_or_renew_lease(cfg_b)
            self.assertTrue(second["is_leader"])
            self.assertEqual(second["leader_id"], "node-b")
            self.assertEqual(second["outcome"], "acquired")
            self.assertEqual(int(second["epoch"]), int(first["epoch"]) + 1)

    def test_non_file_backend_falls_back_to_observer_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lease_file = root / "state" / "event_mesh" / "leader_lease.json"
            cfg = LeaderLeaseConfig(root_dir=root, node_id="node-a", lease_file=lease_file, ttl_sec=30, backend="etcd")
            snapshot = acquire_or_renew_lease(cfg)
            self.assertFalse(snapshot["ok"])
            self.assertFalse(snapshot["is_leader"])
            self.assertTrue(snapshot["observer_mode"])

    def test_non_file_backend_can_fallback_to_file_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lease_file = root / "state" / "event_mesh" / "leader_lease.json"
            cfg = LeaderLeaseConfig(root_dir=root, node_id="node-a", lease_file=lease_file, ttl_sec=30, backend="etcd")
            original = os.environ.get("ORXAQ_AUTONOMY_LEADER_LEASE_FILE_FALLBACK")
            try:
                os.environ["ORXAQ_AUTONOMY_LEADER_LEASE_FILE_FALLBACK"] = "1"
                snapshot = acquire_or_renew_lease(cfg)
            finally:
                if original is None:
                    os.environ.pop("ORXAQ_AUTONOMY_LEADER_LEASE_FILE_FALLBACK", None)
                else:
                    os.environ["ORXAQ_AUTONOMY_LEADER_LEASE_FILE_FALLBACK"] = original
            self.assertTrue(snapshot["ok"])
            self.assertTrue(snapshot["is_leader"])
            self.assertEqual(snapshot["backend"], "file")
            self.assertEqual(snapshot["requested_backend"], "etcd")
            self.assertEqual(snapshot["fallback_backend"], "file")
