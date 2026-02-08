import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.ide import generate_workspace, resolve_ide_command


class IdeTests(unittest.TestCase):
    def test_generate_workspace_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            out = root / "test.code-workspace"
            created = generate_workspace(root, root / "impl", root / "test", out)
            self.assertEqual(created, out)
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertIn("orxaq", text)

    def test_resolve_ide_command_prefers_available_binary(self):
        with mock.patch("orxaq_autonomy.ide.shutil.which", side_effect=lambda x: "/bin/code" if x == "code" else None):
            cmd = resolve_ide_command("vscode")
            self.assertTrue(cmd is not None)
            self.assertIn("code", cmd)


if __name__ == "__main__":
    unittest.main()
