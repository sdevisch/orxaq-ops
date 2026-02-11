import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import router


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RouterTests(unittest.TestCase):
    def _write_config(self, root: pathlib.Path) -> pathlib.Path:
        config = {
            "schema_version": "llm-router.v1",
            "router": {"strategy": "first_healthy", "fallback_order": ["L0", "L1"]},
            "lanes": {"L0": ["local"], "L1": ["remote"]},
            "providers": [
                {"name": "local", "kind": "openai_compat", "base_url": "http://127.0.0.1:1234/v1", "required": False},
                {"name": "remote", "kind": "openai_compat", "base_url": "https://example.invalid/v1", "required": True},
            ],
        }
        path = root / "config" / "router.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_run_router_check_reports_up_provider(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(root)
            output = root / "artifacts" / "router_check.json"
            with mock.patch(
                "orxaq_autonomy.router.urllib_request.urlopen",
                return_value=_FakeResponse({"data": [{"id": "m1"}]}),
            ):
                report = router.run_router_check(
                    root=str(root),
                    config_path=str(config),
                    output_path=str(output),
                    lane="L0",
                    timeout_sec=5,
                )
            self.assertTrue(output.exists())
            self.assertEqual(report["summary"]["provider_total"], 1)
            self.assertEqual(report["summary"]["provider_up"], 1)
            self.assertTrue(report["summary"]["overall_ok"])

    def test_run_router_check_marks_required_down(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(root)
            output = root / "artifacts" / "router_check.json"
            with mock.patch(
                "orxaq_autonomy.router.urllib_request.urlopen",
                side_effect=OSError("network unreachable"),
            ):
                report = router.run_router_check(
                    root=str(root),
                    config_path=str(config),
                    output_path=str(output),
                    lane="L1",
                    timeout_sec=5,
                )
            self.assertEqual(report["summary"]["provider_total"], 1)
            self.assertEqual(report["summary"]["required_down"], 1)
            self.assertFalse(report["summary"]["overall_ok"])


if __name__ == "__main__":
    unittest.main()
