import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.dashboard import normalize_todo_coverage_metrics


class DashboardTodoMetricsTests(unittest.TestCase):
    def test_total_is_derived_when_missing(self):
        payload = {"live_covered": 3, "live_uncovered": 2}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 3, "live_uncovered": 2, "live_coverage_total": 5},
        )

    def test_total_is_corrected_when_inconsistent(self):
        payload = {"live_covered": 4, "live_uncovered": 3, "live_coverage_total": 2}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 4, "live_uncovered": 3, "live_coverage_total": 7},
        )

    def test_inputs_are_normalized_deterministically(self):
        payload = {
            "live_covered": "5",
            "live_uncovered": -9,
            "live_coverage_total": "8",
        }
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 5, "live_uncovered": 0, "live_coverage_total": 8},
        )


if __name__ == "__main__":
    unittest.main()
