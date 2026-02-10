import importlib
import pathlib
import sys
import tempfile
import unittest


def load_runner_module():
    root = pathlib.Path(__file__).resolve().parents[1]
    src = root / "src"
    src_path = str(src)
    sys.path[:] = [entry for entry in sys.path if entry != src_path]
    sys.path.insert(0, src_path)
    importlib.invalidate_caches()
    for module_name in list(sys.modules):
        if module_name == "orxaq_autonomy" or module_name.startswith("orxaq_autonomy."):
            sys.modules.pop(module_name, None)
    return importlib.import_module("orxaq_autonomy.runner")


runner = load_runner_module()


class DashboardTodoActivityTests(unittest.TestCase):
    def test_format_top_todo_activity_returns_accessible_fallback(self):
        self.assertEqual(runner.format_top_todo_activity([]), ["No follow-up actions reported."])

    def test_format_top_todo_activity_normalizes_and_limits(self):
        actions = [
            "  tighten contrast on todo widget  ",
            "",
            "tighten contrast on todo widget",
            "increase heading size",
            "add aria labels",
            "improve spacing",
            "raise line-height",
        ]
        got = runner.format_top_todo_activity(actions, limit=3)
        self.assertEqual(
            got,
            [
                "1. tighten contrast on todo widget",
                "2. increase heading size",
                "3. add aria labels",
                "2 more actions not shown.",
            ],
        )

    def test_format_top_todo_activity_strips_existing_list_prefixes(self):
        actions = [
            " - tighten contrast on todo widget ",
            "1. tighten contrast on todo widget",
            "* add aria labels",
            "2) add aria labels",
            " improve   spacing\nin widget ",
        ]
        got = runner.format_top_todo_activity(actions)
        self.assertEqual(
            got,
            [
                "1. tighten contrast on todo widget",
                "2. add aria labels",
                "3. improve spacing in widget",
            ],
        )

    def test_format_top_todo_activity_strips_markdown_checklist_prefixes(self):
        actions = [
            "- [ ] tighten contrast on todo widget",
            "* [x] add aria labels",
            "- [X] improve spacing",
        ]
        got = runner.format_top_todo_activity(actions)
        self.assertEqual(
            got,
            [
                "1. tighten contrast on todo widget",
                "2. add aria labels",
                "3. improve spacing",
            ],
        )

    def test_format_top_todo_activity_uses_singular_overflow_copy(self):
        got = runner.format_top_todo_activity(["fix contrast", "improve heading"], limit=1)
        self.assertEqual(got, ["1. fix contrast", "1 more action not shown."])

    def test_format_top_todo_activity_coerces_non_positive_limit(self):
        got = runner.format_top_todo_activity(["fix contrast", "improve heading", "add aria labels"], limit=0)
        self.assertEqual(got, ["1. fix contrast", "2 more actions not shown."])

    def test_format_top_todo_activity_dedupes_case_insensitively(self):
        got = runner.format_top_todo_activity(["Fix contrast", "fix contrast", "FIX CONTRAST"])
        self.assertEqual(got, ["1. Fix contrast"])

    def test_format_top_todo_activity_prefers_structured_action_text(self):
        got = runner.format_top_todo_activity(
            [
                {"action": "tighten contrast on todo widget"},
                {"title": "add aria labels"},
                {"note": "missing supported keys"},
                ("improve", "spacing"),
            ]
        )
        self.assertEqual(
            got,
            [
                "1. tighten contrast on todo widget",
                "2. add aria labels",
                "3. improve spacing",
            ],
        )

    def test_format_top_todo_activity_handles_nested_structured_actions(self):
        got = runner.format_top_todo_activity(
            [
                {"action": {"summary": "tighten contrast on todo widget"}},
                {"task": ["improve", "heading", "spacing"]},
            ]
        )
        self.assertEqual(
            got,
            [
                "1. tighten contrast on todo widget",
                "2. improve heading spacing",
            ],
        )

    def test_normalize_outcome_preserves_structured_next_actions_for_dashboard(self):
        normalized = runner.normalize_outcome(
            {
                "status": "done",
                "summary": "ok",
                "next_actions": [{"action": "tighten contrast on todo widget"}, ("improve", "spacing")],
            }
        )
        got = runner.format_top_todo_activity(normalized["next_actions"])
        self.assertEqual(got, ["1. tighten contrast on todo widget", "2. improve spacing"])

    def test_format_top_todo_activity_handles_single_string_payload(self):
        got = runner.format_top_todo_activity(" improve heading contrast ")
        self.assertEqual(got, ["1. improve heading contrast"])

    def test_format_top_todo_activity_truncates_overlong_items(self):
        long_action = "improve heading contrast " * 8
        got = runner.format_top_todo_activity([long_action], limit=1)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("1. "))
        self.assertTrue(got[0].endswith("..."))
        self.assertLessEqual(len(got[0]), 123)

    def test_format_top_todo_activity_truncates_at_word_boundary(self):
        long_action = "alpha beta " * 20
        got = runner.format_top_todo_activity([long_action], limit=1)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].endswith("..."))
        self.assertNotIn("bet...", got[0])

    def test_summarize_run_writes_top_todo_activity_section(self):
        task = runner.Task(
            id="dashboard-a11y",
            owner="codex",
            priority=1,
            title="Improve dashboard todo readability",
            description="desc",
            depends_on=[],
            acceptance=[],
        )
        outcome = {
            "status": "done",
            "summary": "ok",
            "commit": "abc123",
            "blocker": "",
            "next_actions": ["improve heading contrast"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner.summarize_run(task=task, repo=pathlib.Path(tmp), outcome=outcome, report_dir=pathlib.Path(tmp))
            reports = list(pathlib.Path(tmp).glob("dashboard-a11y_*.md"))
            self.assertEqual(len(reports), 1)
            text = reports[0].read_text(encoding="utf-8")
            self.assertIn("## Top Todo Activity", text)
            self.assertIn("1. improve heading contrast", text)
            self.assertNotIn("- 1. improve heading contrast", text)

    def test_summarize_run_writes_fallback_todo_activity_as_bullet(self):
        task = runner.Task(
            id="dashboard-a11y-empty",
            owner="codex",
            priority=1,
            title="Improve dashboard todo readability",
            description="desc",
            depends_on=[],
            acceptance=[],
        )
        outcome = {
            "status": "partial",
            "summary": "no follow-up actions",
            "commit": "",
            "blocker": "",
            "next_actions": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner.summarize_run(task=task, repo=pathlib.Path(tmp), outcome=outcome, report_dir=pathlib.Path(tmp))
            reports = list(pathlib.Path(tmp).glob("dashboard-a11y-empty_*.md"))
            self.assertEqual(len(reports), 1)
            text = reports[0].read_text(encoding="utf-8")
            self.assertIn("## Top Todo Activity", text)
            self.assertIn("- No follow-up actions reported.", text)


if __name__ == "__main__":
    unittest.main()
