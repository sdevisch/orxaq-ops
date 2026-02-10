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
            {"live_covered": 5, "live_uncovered": 0, "live_coverage_total": 5},
        )

    def test_total_is_always_equal_to_sum(self):
        payload = {"live_covered": 1, "live_uncovered": 2, "live_coverage_total": 99}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 1, "live_uncovered": 2, "live_coverage_total": 3},
        )

    def test_normalization_is_stable_on_repeated_calls(self):
        payload = {"live_covered": "2", "live_uncovered": "3", "live_coverage_total": 0}
        first = normalize_todo_coverage_metrics(payload)
        second = normalize_todo_coverage_metrics(first)
        self.assertEqual(first, second)

    def test_none_or_nondict_payloads_return_zeroed_metrics(self):
        self.assertEqual(
            normalize_todo_coverage_metrics(None),
            {"live_covered": 0, "live_uncovered": 0, "live_coverage_total": 0},
        )
        self.assertEqual(
            normalize_todo_coverage_metrics("bad-input"),
            {"live_covered": 0, "live_uncovered": 0, "live_coverage_total": 0},
        )

    def test_invalid_components_do_not_trust_reported_total(self):
        payload = {"live_covered": "NaN", "live_uncovered": None, "live_coverage_total": 12}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 0, "live_uncovered": 0, "live_coverage_total": 0},
        )

    def test_blank_string_components_do_not_trust_reported_total(self):
        payload = {"live_covered": "", "live_uncovered": " ", "live_coverage_total": 42}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 0, "live_uncovered": 0, "live_coverage_total": 0},
        )

    def test_reported_total_alone_does_not_create_coverage(self):
        payload = {"live_coverage_total": 42}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 0, "live_uncovered": 0, "live_coverage_total": 0},
        )

    def test_boolean_components_are_treated_as_invalid(self):
        payload = {"live_covered": True, "live_uncovered": False, "live_coverage_total": 99}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 0, "live_uncovered": 0, "live_coverage_total": 0},
        )

    def test_float_components_are_treated_as_invalid(self):
        payload = {"live_covered": 1.9, "live_uncovered": 2.1, "live_coverage_total": 99}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 0, "live_uncovered": 0, "live_coverage_total": 0},
        )

    def test_float_like_string_components_are_treated_as_invalid(self):
        payload = {"live_covered": "1.9", "live_uncovered": "2.1", "live_coverage_total": 99}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 0, "live_uncovered": 0, "live_coverage_total": 0},
        )

    def test_input_payload_is_not_mutated(self):
        payload = {"live_covered": 2, "live_uncovered": 3, "live_coverage_total": 0}
        _ = normalize_todo_coverage_metrics(payload)
        self.assertEqual(
            payload,
            {"live_covered": 2, "live_uncovered": 3, "live_coverage_total": 0},
        )

    def test_signed_or_spaced_strings_remain_deterministic(self):
        payload = {"live_covered": " +4 ", "live_uncovered": " 2 ", "live_coverage_total": 999}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 4, "live_uncovered": 2, "live_coverage_total": 6},
        )

    def test_int_like_objects_are_rejected_for_determinism(self):
        class IntLike:
            def __int__(self):
                return 9

        payload = {"live_covered": IntLike(), "live_uncovered": "2", "live_coverage_total": 999}
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 0, "live_uncovered": 2, "live_coverage_total": 2},
        )

    def test_normalized_metric_values_are_plain_ints(self):
        payload = {"live_covered": "1", "live_uncovered": "2", "live_coverage_total": "99"}
        normalized = normalize_todo_coverage_metrics(payload)
        self.assertEqual(
            normalized,
            {"live_covered": 1, "live_uncovered": 2, "live_coverage_total": 3},
        )
        self.assertTrue(all(type(value) is int for value in normalized.values()))

    def test_int_subclass_inputs_are_normalized_to_plain_ints(self):
        class IntSubclass(int):
            pass

        payload = {"live_covered": IntSubclass(3), "live_uncovered": IntSubclass(4)}
        normalized = normalize_todo_coverage_metrics(payload)
        self.assertEqual(
            normalized,
            {"live_covered": 3, "live_uncovered": 4, "live_coverage_total": 7},
        )
        self.assertTrue(all(type(value) is int for value in normalized.values()))

    def test_extra_payload_fields_do_not_affect_normalized_totals(self):
        payload = {
            "live_covered": 2,
            "live_uncovered": 1,
            "live_coverage_total": 999,
            "live_coverage_ratio": "0.66",
            "meta": {"source": "dashboard"},
        }
        self.assertEqual(
            normalize_todo_coverage_metrics(payload),
            {"live_covered": 2, "live_uncovered": 1, "live_coverage_total": 3},
        )


if __name__ == "__main__":
    unittest.main()
