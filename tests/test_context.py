import pathlib
import sys
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.context import summarize_filetypes_from_git_ls_files


def _cp(stdout: str = "", returncode: int = 0):
    return mock.Mock(stdout=stdout, returncode=returncode)


def test_summarize_filetypes_git_failure(tmp_path):
    with mock.patch("subprocess.run", return_value=_cp(returncode=1)):
        assert summarize_filetypes_from_git_ls_files(tmp_path) == "File-type profile unavailable."


def test_summarize_filetypes_empty_stdout(tmp_path):
    with mock.patch("subprocess.run", return_value=_cp(stdout="\n")):
        assert summarize_filetypes_from_git_ls_files(tmp_path) == "File-type profile unavailable."


def test_summarize_filetypes_counts_and_no_ext(tmp_path):
    stdout = "a.py\nb.py\nREADME\nnested/c.md\n"
    with mock.patch("subprocess.run", return_value=_cp(stdout=stdout)):
        summary = summarize_filetypes_from_git_ls_files(tmp_path, limit=3)
    assert summary.startswith("Top file types:")
    assert "py:2" in summary
    assert "(no_ext):1" in summary
    assert "md:1" in summary
