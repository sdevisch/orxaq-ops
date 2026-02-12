import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.profile import profile_apply


class ProfileTests(unittest.TestCase):
    def test_profile_apply(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "profiles").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            (root / "profiles" / "local.yaml").write_text('{"name":"local"}', encoding="utf-8")
            dst = profile_apply(root=root, name="local")
            self.assertTrue(dst.exists())
            self.assertIn("local", dst.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
