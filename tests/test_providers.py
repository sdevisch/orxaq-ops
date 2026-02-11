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

from orxaq_autonomy.providers import run_providers_check


class _Resp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ProvidersTests(unittest.TestCase):
    def _write_config(self, root: pathlib.Path, payload: dict) -> pathlib.Path:
        config = root / "config.json"
        config.write_text(json.dumps(payload), encoding="utf-8")
        return config

    def test_providers_check_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(
                root,
                {
                    "providers": [
                        {
                            "name": "lm",
                            "kind": "openai_compat",
                            "lane": "L0",
                            "base_url": "http://localhost:1234",
                            "required": True,
                            "models_path": "/v1/models",
                        }
                    ]
                },
            )
            with mock.patch("orxaq_autonomy.providers.request.urlopen", return_value=_Resp('{"data": []}')):
                report = run_providers_check(
                    root=str(root),
                    config_path=str(config),
                    output_path=str(root / "providers.json"),
                    timeout_sec=1,
                )
            self.assertTrue(report["summary"]["all_required_up"])
            self.assertEqual(report["summary"]["required_down"], 0)

    def test_missing_required_key_marks_down(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(
                root,
                {
                    "providers": [
                        {
                            "name": "oa",
                            "kind": "openai",
                            "lane": "L1",
                            "base_url": "https://api.openai.com",
                            "required": True,
                            "api_key_env": "OPENAI_API_KEY",
                        }
                    ]
                },
            )
            with mock.patch.dict("os.environ", {}, clear=False):
                report = run_providers_check(
                    root=str(root),
                    config_path=str(config),
                    output_path=str(root / "providers.json"),
                    timeout_sec=1,
                )
            self.assertFalse(report["summary"]["all_required_up"])
            self.assertEqual(report["summary"]["required_down"], 1)

    def test_invalid_json_response_marks_down(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(
                root,
                {
                    "providers": [
                        {
                            "name": "lm",
                            "kind": "openai_compat",
                            "lane": "L0",
                            "base_url": "http://localhost:1234",
                            "required": True,
                            "models_path": "/v1/models",
                        }
                    ]
                },
            )
            with mock.patch("orxaq_autonomy.providers.request.urlopen", return_value=_Resp("not-json")):
                report = run_providers_check(
                    root=str(root),
                    config_path=str(config),
                    output_path=str(root / "providers.json"),
                    timeout_sec=1,
                )
            self.assertEqual(report["summary"]["required_down"], 1)


if __name__ == "__main__":
    unittest.main()
