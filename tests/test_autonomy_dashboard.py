import io
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import dashboard


class _CountingBytesIO(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        return super().read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class DashboardTodoIngestionPerfTests(unittest.TestCase):
    def test_normalize_tail_slice_reuses_lf_only_payload(self):
        payload = b"first\nsecond\nthird"
        normalized = dashboard._normalize_tail_slice(payload)
        self.assertIs(normalized, payload)

    def test_normalize_tail_slice_removes_cr_from_crlf_lines(self):
        payload = b"first\r\nsecond\r\nthird\r"
        normalized = dashboard._normalize_tail_slice(payload)
        self.assertEqual(normalized, b"first\nsecond\nthird")

    def test_normalize_tail_slice_preserves_embedded_carriage_returns(self):
        payload = b"first\rkeep\r\nsecond\r\nthird\r"
        normalized = dashboard._normalize_tail_slice(payload)
        self.assertEqual(normalized, b"first\rkeep\nsecond\nthird")

    def test_rstrip_newline_bytes_reuses_payload_without_trailing_newline(self):
        payload = b"first\nsecond\nthird"
        stripped = dashboard._rstrip_newline_bytes(payload)
        self.assertIs(stripped, payload)

    def test_rstrip_newline_bytes_removes_trailing_newline_bytes(self):
        payload = b"first\r\nsecond\r\n"
        stripped = dashboard._rstrip_newline_bytes(payload)
        self.assertEqual(stripped, b"first\r\nsecond")

    def test_tail_reader_stops_once_requested_line_count_is_met(self):
        path = pathlib.Path("routing.log")
        tail = (b"x" * (8192 - 4)) + b"a\nb\n"
        data = (b"prefix-without-newlines" * 80) + tail
        handle = _CountingBytesIO(data)

        case = self

        def fake_open(self, mode="r", *args, **kwargs):
            case.assertEqual(self, path)
            case.assertEqual(mode, "rb")
            handle.seek(0)
            return handle

        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch("pathlib.Path.open", new=fake_open):
            result = dashboard.tail_routing_activity(path, lines=2)

        self.assertTrue(result.endswith("\nb"))
        self.assertEqual(handle.read_calls, 1)

    def test_tail_reader_ignores_trailing_newline_without_empty_line(self):
        path = pathlib.Path("routing.log")
        handle = _CountingBytesIO(b"first\r\nsecond\r\nthird\r\n")

        case = self

        def fake_open(self, mode="r", *args, **kwargs):
            case.assertEqual(self, path)
            case.assertEqual(mode, "rb")
            handle.seek(0)
            return handle

        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch("pathlib.Path.open", new=fake_open):
            result = dashboard.tail_routing_activity(path, lines=2)

        self.assertEqual(result, "second\nthird")

    def test_tail_reader_drops_leading_empty_line_when_boundary_newline_at_start(self):
        path = pathlib.Path("routing.log")
        handle = _CountingBytesIO(b"\nfirst\nsecond\n")

        case = self

        def fake_open(self, mode="r", *args, **kwargs):
            case.assertEqual(self, path)
            case.assertEqual(mode, "rb")
            handle.seek(0)
            return handle

        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch("pathlib.Path.open", new=fake_open):
            result = dashboard.tail_routing_activity(path, lines=2)

        self.assertEqual(result, "first\nsecond")

    def test_tail_reader_stops_for_non_terminated_file_when_enough_lines_collected(self):
        path = pathlib.Path("routing.log")
        tail = (b"x" * (8192 - 3)) + b"a\nb"
        data = (b"prefix-without-newlines" * 80) + tail
        handle = _CountingBytesIO(data)

        case = self

        def fake_open(self, mode="r", *args, **kwargs):
            case.assertEqual(self, path)
            case.assertEqual(mode, "rb")
            handle.seek(0)
            return handle

        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch("pathlib.Path.open", new=fake_open):
            result = dashboard.tail_routing_activity(path, lines=2)

        self.assertTrue(result.endswith("\nb"))
        self.assertEqual(handle.read_calls, 1)

    def test_tail_reader_returns_full_large_non_terminated_single_line(self):
        path = pathlib.Path("routing.log")
        data = b"x" * 9000
        handle = _CountingBytesIO(data)

        case = self

        def fake_open(self, mode="r", *args, **kwargs):
            case.assertEqual(self, path)
            case.assertEqual(mode, "rb")
            handle.seek(0)
            return handle

        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch("pathlib.Path.open", new=fake_open):
            result = dashboard.tail_routing_activity(path, lines=1)

        self.assertEqual(result, "x" * len(data))
        self.assertEqual(handle.read_calls, 2)

    def test_tail_reader_returns_full_large_terminated_single_line(self):
        path = pathlib.Path("routing.log")
        payload = b"x" * 9000 + b"\n"
        handle = _CountingBytesIO(payload)

        case = self

        def fake_open(self, mode="r", *args, **kwargs):
            case.assertEqual(self, path)
            case.assertEqual(mode, "rb")
            handle.seek(0)
            return handle

        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch("pathlib.Path.open", new=fake_open):
            result = dashboard.tail_routing_activity(path, lines=1)

        self.assertEqual(result, "x" * (len(payload) - 1))
        self.assertEqual(handle.read_calls, 2)


if __name__ == "__main__":
    unittest.main()
