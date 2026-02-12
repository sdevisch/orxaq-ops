import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.ide import generate_workspace, open_in_ide, resolve_ide_command


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
        with mock.patch("orxaq_autonomy.ide.Path.exists", return_value=False), mock.patch(
            "orxaq_autonomy.ide.shutil.which", side_effect=lambda x: "/bin/code" if x == "code" else None
        ):
            cmd = resolve_ide_command("vscode")
            self.assertTrue(cmd is not None)
            self.assertIn("code", cmd)

    def test_resolve_ide_command_prefers_absolute_path_when_present(self):
        with mock.patch("orxaq_autonomy.ide.Path.exists", return_value=True):
            cmd = resolve_ide_command("vscode")
        self.assertTrue(cmd is not None)
        self.assertTrue(cmd.startswith("/Applications/"))

    def test_resolve_ide_command_returns_none_for_unknown_ide(self):
        self.assertIsNone(resolve_ide_command("unknown"))

    def test_open_in_ide_raises_for_missing_command(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            with mock.patch("orxaq_autonomy.ide.resolve_ide_command", return_value=None):
                with self.assertRaises(RuntimeError):
                    open_in_ide(ide="vscode", root=root)

    def test_open_in_ide_uses_workspace_for_vscode(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ws = root / "x.code-workspace"
            ws.write_text("{}", encoding="utf-8")
            proc = mock.Mock(pid=1234)
            with mock.patch("orxaq_autonomy.ide.resolve_ide_command", return_value="/bin/code"), mock.patch(
                "orxaq_autonomy.ide.subprocess.Popen", return_value=proc
            ) as popen:
                message = open_in_ide(ide="vscode", root=root, workspace_file=ws)
            popen.assert_called_once()
            args = popen.call_args.args[0]
            self.assertEqual(args, ["/bin/code", str(ws)])
            self.assertIn("pid=1234", message)

    def test_open_in_ide_uses_root_for_non_workspace_ide(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ws = root / "x.code-workspace"
            ws.write_text("{}", encoding="utf-8")
            proc = mock.Mock(pid=5678)
            with mock.patch("orxaq_autonomy.ide.resolve_ide_command", return_value="/bin/charm"), mock.patch(
                "orxaq_autonomy.ide.subprocess.Popen", return_value=proc
            ) as popen:
                open_in_ide(ide="pycharm", root=root, workspace_file=ws)
            args = popen.call_args.args[0]
            self.assertEqual(args, ["/bin/charm", str(root)])


if __name__ == "__main__":
    unittest.main()
