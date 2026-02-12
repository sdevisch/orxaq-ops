import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.task_queue import (
    read_checkpoint,
    validate_task_queue_payload,
    write_checkpoint,
)


class TaskQueueTests(unittest.TestCase):
    def test_validate_payload(self):
        errors = validate_task_queue_payload(
            [
                {
                    "id": "T1",
                    "owner": "codex",
                    "priority": 1,
                    "title": "x",
                    "description": "y",
                }
            ]
        )
        self.assertEqual(errors, [])

    def test_duplicate_id_detected(self):
        errors = validate_task_queue_payload(
            [
                {"id": "T1", "owner": "codex", "priority": 1, "title": "a", "description": "b"},
                {"id": "T1", "owner": "gemini", "priority": 2, "title": "c", "description": "d"},
            ]
        )
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_checkpoint_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "cp.json"
            write_checkpoint(path=path, run_id="run1", cycle=2, state={"T1": {"status": "done"}})
            payload = read_checkpoint(path)
            self.assertEqual(payload["run_id"], "run1")
            self.assertEqual(payload["cycle"], 2)
            self.assertEqual(payload["state"]["T1"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
