import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.rpa_scheduler import RPAJob, SchedulerPolicy, load_scheduler_config, run_rpa_schedule


class RPASchedulerTests(unittest.TestCase):
    def test_concurrency_cap_is_enforced(self):
        policy = SchedulerPolicy(
            max_concurrent_browsers=2,
            per_domain_interval_sec=0.0,
            failure_backoff_base_sec=0.0,
            failure_backoff_max_sec=0.0,
            max_retries=0,
        )
        jobs = [
            RPAJob(id=f"job-{idx}", domain=f"d{idx}.example.com", command=["echo", "ok"])
            for idx in range(6)
        ]
        active = 0
        max_seen = 0
        lock = threading.Lock()

        def run_job(_job: RPAJob):
            nonlocal active, max_seen
            with lock:
                active += 1
                max_seen = max(max_seen, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return True, ""

        report = run_rpa_schedule(policy=policy, jobs=jobs, run_job=run_job)
        self.assertTrue(report["ok"])
        self.assertEqual(report["jobs_succeeded"], len(jobs))
        self.assertLessEqual(max_seen, 2)
        self.assertLessEqual(report["max_concurrency_seen"], 2)

    def test_per_domain_rate_limit_is_enforced(self):
        policy = SchedulerPolicy(
            max_concurrent_browsers=2,
            per_domain_interval_sec=0.03,
            failure_backoff_base_sec=0.0,
            failure_backoff_max_sec=0.0,
            max_retries=0,
        )
        jobs = [
            RPAJob(id="job-a", domain="example.com", command=["echo", "ok"]),
            RPAJob(id="job-b", domain="example.com", command=["echo", "ok"]),
        ]
        starts: list[float] = []
        lock = threading.Lock()

        def run_job(_job: RPAJob):
            with lock:
                starts.append(time.monotonic())
            return True, ""

        report = run_rpa_schedule(policy=policy, jobs=jobs, run_job=run_job)
        self.assertTrue(report["ok"])
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 0.02)

    def test_failure_backoff_and_retries(self):
        policy = SchedulerPolicy(
            max_concurrent_browsers=1,
            per_domain_interval_sec=0.0,
            failure_backoff_base_sec=0.01,
            failure_backoff_max_sec=0.02,
            max_retries=2,
        )
        jobs = [RPAJob(id="job-a", domain="example.com", command=["echo", "ok"])]
        attempt_times: list[float] = []

        def run_job(_job: RPAJob):
            attempt_times.append(time.monotonic())
            if len(attempt_times) < 3:
                return False, "transient"
            return True, ""

        report = run_rpa_schedule(policy=policy, jobs=jobs, run_job=run_job)
        self.assertTrue(report["ok"])
        self.assertEqual(report["attempts_total"], 3)
        self.assertEqual(report["jobs_succeeded"], 1)
        self.assertGreaterEqual(attempt_times[1] - attempt_times[0], 0.009)
        self.assertGreaterEqual(attempt_times[2] - attempt_times[1], 0.018)

    def test_load_scheduler_config_builds_orxaq_commands(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = root / "schedule.json"
            payload = {
                "run_id": "run-1",
                "python_executable": "python3",
                "orxaq_cli_path": "./orxaq_cli.py",
                "evidence_root": "./artifacts/rpa_evidence",
                "policy": {"max_concurrent_browsers": 1, "max_retries": 0},
                "jobs": [
                    {
                        "id": "job-a",
                        "url": "https://example.com",
                        "domain": "example.com",
                    }
                ],
            }
            config.write_text(json.dumps(payload), encoding="utf-8")
            policy, jobs = load_scheduler_config(str(root), str(config))
            self.assertEqual(policy.max_concurrent_browsers, 1)
            self.assertEqual(len(jobs), 1)
            self.assertIn("rpa-screenshot", jobs[0].command)
            self.assertIn("--allow-domain", jobs[0].command)


if __name__ == "__main__":
    unittest.main()
