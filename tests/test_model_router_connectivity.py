import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "model_router_connectivity.py"

module_spec = importlib.util.spec_from_file_location("model_router_connectivity", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
model_router_connectivity = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("model_router_connectivity", model_router_connectivity)
module_spec.loader.exec_module(model_router_connectivity)


class _MockResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ModelRouterConnectivityTests(unittest.TestCase):
    def test_load_router_config_parses_litellm_model_list(self):
        endpoints = model_router_connectivity.load_router_config(
            ROOT / "config" / "litellm_swarm_router.json"
        )
        ids = {item.endpoint_id for item in endpoints}
        self.assertTrue({"openai-primary", "gemini-primary", "claude-primary"}.issubset(ids))
        self.assertTrue({"lmstudio-node-1", "lmstudio-node-2", "lmstudio-node-3"}.issubset(ids))
        self.assertGreaterEqual(len(endpoints), 6)

    def test_probe_endpoint_success_with_mocked_urlopen(self):
        endpoint = model_router_connectivity.EndpointConfig(
            endpoint_id="mock-openai",
            provider="openai",
            api_base="https://api.example.com",
            healthcheck_path="/v1/models",
            auth_mode="none",
            api_key_env="",
            required=False,
            model_names=["openai/gpt-test"],
        )
        with mock.patch.object(
            model_router_connectivity,
            "urlopen",
            return_value=_MockResponse({"data": [{"id": "m1"}]}, status=200),
        ):
            row = model_router_connectivity.probe_endpoint(endpoint, timeout_sec=1)
        self.assertTrue(row["ok"])
        self.assertEqual(row["status_code"], 200)
        self.assertEqual(row["model_count"], 1)
        self.assertEqual(row["id"], "mock-openai")

    def test_probe_endpoint_http_error_is_reported(self):
        endpoint = model_router_connectivity.EndpointConfig(
            endpoint_id="mock-failure",
            provider="openai",
            api_base="https://api.example.com",
            healthcheck_path="/v1/models",
            auth_mode="none",
            api_key_env="",
            required=False,
            model_names=["openai/gpt-test"],
        )
        error = HTTPError(
            "https://api.example.com/v1/models",
            503,
            "service unavailable",
            hdrs=None,
            fp=io.BytesIO(b"downstream unavailable"),
        )
        with mock.patch.object(model_router_connectivity, "urlopen", side_effect=error):
            row = model_router_connectivity.probe_endpoint(endpoint, timeout_sec=1)
        self.assertFalse(row["ok"])
        self.assertEqual(row["status_code"], 503)
        self.assertIn("http_503", row["error"])

    def test_main_writes_connectivity_report_with_mocked_network(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            config = tmp / "router.json"
            output = tmp / "connectivity.json"
            config.write_text(
                json.dumps(
                    {
                        "model_list": [
                            {
                                "model_name": "openai/gpt-test",
                                "litellm_params": {
                                    "provider": "openai",
                                    "api_base": "https://api.example.com",
                                    "endpoint_id": "openai-primary",
                                    "healthcheck_path": "/v1/models",
                                },
                            },
                            {
                                "model_name": "local/lmstudio-1",
                                "litellm_params": {
                                    "provider": "openai_compatible",
                                    "api_base": "http://127.0.0.1:1234",
                                    "endpoint_id": "lmstudio-node-1",
                                    "healthcheck_path": "/v1/models",
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            side_effects = [
                _MockResponse({"data": [{"id": "m1"}]}, status=200),
                HTTPError(
                    "http://127.0.0.1:1234/v1/models",
                    500,
                    "error",
                    hdrs=None,
                    fp=io.BytesIO(b"offline"),
                ),
            ]
            with mock.patch.object(model_router_connectivity, "urlopen", side_effect=side_effects):
                rc = model_router_connectivity.main(
                    [
                        "--config",
                        str(config),
                        "--output",
                        str(output),
                        "--timeout-sec",
                        "1",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["endpoint_total"], 2)
            self.assertEqual(payload["endpoint_healthy"], 1)
            self.assertEqual(payload["endpoint_unhealthy"], 1)


if __name__ == "__main__":
    unittest.main()
