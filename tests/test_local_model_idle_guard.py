import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.local_model_idle_guard as idle_guard  # noqa: E402


class LocalModelIdleGuardTests(unittest.TestCase):
    def test_lane_queue_depth_counts_unclaimed_tasks_for_lane_owner(self):
        with tempfile.TemporaryDirectory() as td:
            queue_file = pathlib.Path(td) / "queue.ndjson"
            queue_state_file = pathlib.Path(td) / "queue_state.json"
            queue_file.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "codex-q-1", "owner": "codex"}),
                        json.dumps({"id": "codex-q-2", "owner": "codex"}),
                        json.dumps({"id": "gemini-q-1", "owner": "gemini"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            idle_guard.runner_module.save_task_queue_state(
                queue_state_file,
                {"codex-q-1": "2026-02-09T00:00:00Z"},
            )
            lane = {
                "owner": "codex",
                "task_queue_file": str(queue_file),
                "task_queue_state_file": str(queue_state_file),
            }
            payload = idle_guard._lane_queue_depth(lane)
            self.assertEqual(payload["pending"], 1)
            self.assertEqual(payload["queue_file"], str(queue_file.resolve()))

    def test_recycle_backlog_tasks_reopens_done_backlog_items(self):
        with tempfile.TemporaryDirectory() as td:
            tasks_file = pathlib.Path(td) / "tasks.json"
            state_file = pathlib.Path(td) / "state.json"
            tasks_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "codex-live-fix",
                            "owner": "codex",
                            "priority": 1,
                            "title": "live",
                            "description": "live",
                            "depends_on": [],
                            "acceptance": [],
                            "backlog": False,
                        },
                        {
                            "id": "codex-backlog-refresh",
                            "owner": "codex",
                            "priority": 5,
                            "title": "backlog",
                            "description": "backlog",
                            "depends_on": [],
                            "acceptance": [],
                            "backlog": True,
                        },
                    ]
                ),
                encoding="utf-8",
            )
            state_file.write_text(
                json.dumps(
                    {
                        "codex-live-fix": {
                            "status": "done",
                            "attempts": 1,
                            "retryable_failures": 0,
                            "deadlock_recoveries": 0,
                            "deadlock_reopens": 0,
                            "not_before": "",
                            "last_update": "",
                            "last_summary": "",
                            "last_error": "",
                            "owner": "codex",
                        },
                        "codex-backlog-refresh": {
                            "status": "done",
                            "attempts": 1,
                            "retryable_failures": 0,
                            "deadlock_recoveries": 0,
                            "deadlock_reopens": 0,
                            "not_before": "",
                            "last_update": "",
                            "last_summary": "",
                            "last_error": "",
                            "owner": "codex",
                        },
                    }
                ),
                encoding="utf-8",
            )
            lane = {
                "id": "lane-a",
                "tasks_file": str(tasks_file),
                "state_file": str(state_file),
                "dependency_state_file": "",
            }
            payload = idle_guard._recycle_backlog_tasks(lane, delay_sec=0, max_recycles=5)
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["reason"], "backlog_recycled")
            self.assertIn("codex-backlog-refresh", payload["recycled"])

            state_payload = idle_guard.runner_module.load_state(
                state_file,
                idle_guard.runner_module.load_tasks(tasks_file),
            )
            self.assertEqual(state_payload["codex-backlog-refresh"]["status"], idle_guard.runner_module.STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
