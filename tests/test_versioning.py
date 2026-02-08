import pathlib
import tempfile
import unittest

from orxaq_autonomy.versioning import (
    bump_version,
    load_project_version,
    validate_release_tag,
    validate_semver,
)


class VersioningTests(unittest.TestCase):
    def test_validate_semver_accepts_core_and_prerelease(self):
        self.assertEqual(validate_semver("0.1.1"), [])
        self.assertEqual(validate_semver("1.2.3rc1"), [])

    def test_validate_semver_rejects_non_semver(self):
        self.assertTrue(validate_semver("1.2"))

    def test_validate_release_tag(self):
        self.assertEqual(validate_release_tag("1.2.3", "v1.2.3"), [])
        self.assertTrue(validate_release_tag("1.2.3", "v1.2.4"))

    def test_bump_version(self):
        self.assertEqual(bump_version("1.2.3", "patch"), "1.2.4")
        self.assertEqual(bump_version("1.2.3", "minor"), "1.3.0")
        self.assertEqual(bump_version("1.2.3", "major"), "2.0.0")

    def test_load_project_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            pyproject = pathlib.Path(tmp) / "pyproject.toml"
            pyproject.write_text('[project]\nname="x"\nversion="0.1.1"\n', encoding="utf-8")
            self.assertEqual(load_project_version(pyproject), "0.1.1")


if __name__ == "__main__":
    unittest.main()
