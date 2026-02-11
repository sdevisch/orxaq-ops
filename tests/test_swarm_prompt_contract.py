import pathlib
import unittest


class SwarmPromptContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(__file__).resolve().parents[1]
        self.shared = self.root / "config" / "prompts" / "swarm_shared_git_contract.md"
        self.codex = self.root / "config" / "prompts" / "swarm_codex_session_prompt.md"
        self.gemini = self.root / "config" / "prompts" / "swarm_gemini_session_prompt.md"
        self.claude = self.root / "config" / "prompts" / "swarm_claude_session_prompt.md"
        self.objective = self.root / "config" / "objective.md"

    def test_prompt_files_exist(self):
        for path in (self.shared, self.codex, self.gemini, self.claude, self.objective):
            self.assertTrue(path.exists(), f"missing prompt file: {path}")

    def test_shared_contract_contains_required_git_cycle_rules(self):
        text = self.shared.read_text(encoding="utf-8").lower()
        required = [
            "issue-linked branch",
            "unit tests before each commit",
            "commit every validated logical change",
            "push contiguous commit blocks",
            "open or update the pull request",
            "higher-level review to-do",
            "review status",
            "review score",
            "urgent fix",
            "branch is gone",
        ]
        for needle in required:
            self.assertIn(needle, text)

    def test_role_prompts_reference_shared_contract(self):
        rel = "config/prompts/swarm_shared_git_contract.md"
        for path in (self.codex, self.gemini, self.claude):
            text = path.read_text(encoding="utf-8")
            self.assertIn(rel, text, f"{path} must reference shared contract")

    def test_objective_includes_machine_readable_markers(self):
        text = self.objective.read_text(encoding="utf-8").lower()
        for marker in (
            "branch=<branch-name>",
            "tests_pre_commit=<commands>",
            "pr_url=<https://github.com/.../pull/...>",
            "higher_level_review_todo=<todo-id-or-path>",
            "review_status=<passed|failed>",
            "review_score=<0-100>",
            "urgent_fix=<yes|no>",
            "merge_effective=<branch_gone|pending>",
        ):
            self.assertIn(marker.lower(), text)


if __name__ == "__main__":
    unittest.main()
