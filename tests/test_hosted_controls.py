import pathlib
import tempfile
import unittest
import importlib.util
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "check_hosted_controls.py"
SPEC = importlib.util.spec_from_file_location("check_hosted_controls", MODULE_PATH)
assert SPEC and SPEC.loader
check_hosted_controls = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_hosted_controls
SPEC.loader.exec_module(check_hosted_controls)


class HostedControlsTests(unittest.TestCase):
    def test_badge_urls_from_readme(self):
        with tempfile.TemporaryDirectory() as td:
            readme = pathlib.Path(td) / "README.md"
            readme.write_text("[![CI](https://example.com/badge.svg)](https://example.com)\n", encoding="utf-8")
            urls = check_hosted_controls.badge_urls_from_readme(readme)
            self.assertEqual(urls, ["https://example.com/badge.svg"])

    def test_branch_protection_errors_for_404(self):
        with mock.patch.object(
            check_hosted_controls,
            "_gh_api_json",
            return_value=(False, None, "gh: Branch not protected (HTTP 404)"),
        ):
            errs = check_hosted_controls.branch_protection_errors("owner/repo", "main")
            self.assertTrue(any("not protected" in e for e in errs))

    def test_branch_protection_errors_for_private_plan_403(self):
        with mock.patch.object(
            check_hosted_controls,
            "_gh_api_json",
            return_value=(False, None, "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)"),
        ):
            errs = check_hosted_controls.branch_protection_errors("owner/repo", "main")
            self.assertTrue(any("private repos" in e for e in errs))

    def test_branch_protection_ok_payload(self):
        payload = {
            "required_status_checks": {"contexts": ["CI"]},
            "required_pull_request_reviews": {
                "required_approving_review_count": 1,
                "require_code_owner_reviews": True,
            },
            "enforce_admins": {"enabled": True},
            "required_linear_history": {"enabled": True},
            "required_conversation_resolution": {"enabled": True},
        }
        with mock.patch.object(check_hosted_controls, "_gh_api_json", return_value=(True, payload, "")):
            self.assertEqual(check_hosted_controls.branch_protection_errors("owner/repo", "main"), [])

    def test_badge_errors_non_image(self):
        with tempfile.TemporaryDirectory() as td:
            readme = pathlib.Path(td) / "README.md"
            readme.write_text("[![CI](https://example.com/badge.svg)](https://example.com)\n", encoding="utf-8")
            with mock.patch.object(
                check_hosted_controls,
                "_badge_url_error",
                return_value="https://example.com/badge.svg returned non-image content-type 'text/html'",
            ):
                errs = check_hosted_controls.badge_errors(readme)
                self.assertEqual(len(errs), 1)

    def test_is_repo_private_true(self):
        with mock.patch.object(
            check_hosted_controls,
            "_gh_api_json",
            return_value=(True, {"private": True}, ""),
        ):
            ok, private, err = check_hosted_controls.is_repo_private("owner/repo")
            self.assertTrue(ok)
            self.assertTrue(private)
            self.assertEqual(err, "")

    def test_is_repo_private_failure(self):
        with mock.patch.object(
            check_hosted_controls,
            "_gh_api_json",
            return_value=(False, None, "boom"),
        ):
            ok, private, err = check_hosted_controls.is_repo_private("owner/repo")
            self.assertFalse(ok)
            self.assertFalse(private)
            self.assertIn("owner/repo", err)


if __name__ == "__main__":
    unittest.main()
