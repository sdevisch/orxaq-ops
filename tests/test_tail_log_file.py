"""Tests for dashboard.tail_log_file (Issue #9)."""

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.dashboard import tail_log_file


class TailLogFileTests(unittest.TestCase):
    """Issue #9: Optimize dashboard todo log tail ingestion for large routing logs."""

    def test_small_file_returns_all_lines(self):
        """When the file is smaller than max_bytes, every line is returned."""
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "small.log"
            p.write_text("line1\nline2\nline3\n", encoding="utf-8")
            lines = tail_log_file(p, max_bytes=65536)
            self.assertEqual(lines, ["line1", "line2", "line3"])

    def test_large_file_bounded_read(self):
        """For a file larger than max_bytes only the tail is returned."""
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "large.log"
            # Write ~200 KiB of log data
            with open(p, "w", encoding="utf-8") as fh:
                for i in range(2000):
                    fh.write(f"[{i:04d}] some log message padding here\n")

            file_size = p.stat().st_size
            self.assertGreater(file_size, 65536, "fixture must exceed default max_bytes")

            lines = tail_log_file(p, max_bytes=65536)
            # Should contain final lines but NOT the first line
            self.assertGreater(len(lines), 0)
            self.assertIn("[1999]", lines[-1])
            # Ensure bounded: returned text should be <= max_bytes + one line overhead
            total_chars = sum(len(ln) for ln in lines) + len(lines)  # approx
            self.assertLessEqual(total_chars, 65536 + 200)

    def test_empty_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "empty.log"
            p.write_text("", encoding="utf-8")
            lines = tail_log_file(p)
            self.assertEqual(lines, [])

    def test_missing_file_returns_empty_list(self):
        lines = tail_log_file(pathlib.Path("/tmp/nonexistent_orxaq_test_log.log"))
        self.assertEqual(lines, [])

    def test_custom_max_bytes(self):
        """Passing a smaller max_bytes still works correctly."""
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "medium.log"
            with open(p, "w", encoding="utf-8") as fh:
                for i in range(500):
                    fh.write(f"line-{i:04d}\n")

            lines = tail_log_file(p, max_bytes=256)
            self.assertGreater(len(lines), 0)
            # Last line should be the very last written
            self.assertEqual(lines[-1], "line-0499")

    def test_single_line_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "single.log"
            p.write_text("only line\n", encoding="utf-8")
            lines = tail_log_file(p)
            self.assertEqual(lines, ["only line"])

    def test_no_trailing_newline(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "notail.log"
            p.write_text("a\nb\nc", encoding="utf-8")
            lines = tail_log_file(p)
            self.assertEqual(lines, ["a", "b", "c"])

    def test_directory_path_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            lines = tail_log_file(pathlib.Path(td))
            self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
