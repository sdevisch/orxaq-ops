import importlib.util
import json
import os
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "provider_cost_ingest.py"
FIXTURES = ROOT / "tests" / "fixtures" / "provider_cost_ingest"

module_spec = importlib.util.spec_from_file_location("provider_cost_ingest", SCRIPT_PATH)
assert module_spec is not None
assert module_spec.loader is not None
provider_cost_ingest = importlib.util.module_from_spec(module_spec)
sys.modules.setdefault("provider_cost_ingest", provider_cost_ingest)
module_spec.loader.exec_module(provider_cost_ingest)


class ProviderCostIngestTests(unittest.TestCase):
    def _fixture_payload(self, filename: str) -> dict:
        return json.loads((FIXTURES / filename).read_text(encoding="utf-8"))

    def _spec_by_provider(self, provider: str):
        for item in provider_cost_ingest.SPECS:
            if item.provider == provider:
                return item
        raise AssertionError(f"provider spec missing: {provider}")

    def _env_for_spec(self, spec):
        values = {spec.url_env: "https://example.test/v1/usage"}
        values[spec.key_envs[0]] = "secret"
        return values

    def test_ingest_provider_no_data_is_ok(self):
        provider_spec = provider_cost_ingest.ProviderSpec(
            provider="openai",
            url_env="TEST_PROVIDER_URL",
            key_envs=("TEST_PROVIDER_KEY",),
            auth_mode="bearer",
        )
        with mock.patch.dict(
            os.environ,
            {
                "TEST_PROVIDER_URL": "https://example.test/v1/usage",
                "TEST_PROVIDER_KEY": "secret",
            },
            clear=False,
        ), mock.patch.object(provider_cost_ingest, "_request_json", return_value={"data": []}):
            records, status = provider_cost_ingest.ingest_provider(
                provider_spec,
                start_ts=1700000000,
                end_ts=1700003600,
                page_size=50,
                max_pages=2,
                timeout_sec=5,
                retries=0,
                backoff_sec=1,
            )

        self.assertEqual(records, [])
        self.assertTrue(status["ok"])
        self.assertEqual(status["status"], "no_data")
        self.assertEqual(status["records"], 0)
        self.assertEqual(status["pages"], 1)
        self.assertEqual(status["errors"], [])

    def test_ingest_provider_openai_fixture_contract(self):
        provider_spec = self._spec_by_provider("openai")
        payload = self._fixture_payload("openai_usage_response.json")
        with mock.patch.dict(
            os.environ,
            self._env_for_spec(provider_spec),
            clear=False,
        ), mock.patch.object(provider_cost_ingest, "_request_json", return_value=payload):
            records, status = provider_cost_ingest.ingest_provider(
                provider_spec,
                start_ts=1700000000,
                end_ts=1700003600,
                page_size=50,
                max_pages=1,
                timeout_sec=5,
                retries=0,
                backoff_sec=1,
            )
        self.assertTrue(status["ok"])
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["records"], 1)
        self.assertEqual(records[0]["provider"], "openai")
        self.assertEqual(records[0]["model"], "gpt-4.1-mini")
        self.assertEqual(records[0]["source_of_truth"], "authoritative_provider_api")
        self.assertGreater(records[0]["total_tokens"], 0)
        self.assertGreater(records[0]["total_cost_usd"], 0.0)

    def test_ingest_provider_anthropic_fixture_contract(self):
        provider_spec = self._spec_by_provider("anthropic")
        payload = self._fixture_payload("anthropic_usage_response.json")
        with mock.patch.dict(
            os.environ,
            self._env_for_spec(provider_spec),
            clear=False,
        ), mock.patch.object(provider_cost_ingest, "_request_json", return_value=payload):
            records, status = provider_cost_ingest.ingest_provider(
                provider_spec,
                start_ts=1700000000,
                end_ts=1700003600,
                page_size=50,
                max_pages=1,
                timeout_sec=5,
                retries=0,
                backoff_sec=1,
            )
        self.assertTrue(status["ok"])
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["records"], 1)
        self.assertEqual(records[0]["provider"], "anthropic")
        self.assertEqual(records[0]["model"], "claude-3-7-sonnet-latest")
        self.assertEqual(records[0]["source_of_truth"], "authoritative_provider_api")
        self.assertEqual(records[0]["currency"], "USD")
        self.assertGreater(records[0]["total_tokens"], 0)
        self.assertGreater(records[0]["total_cost_usd"], 0.0)

    def test_ingest_provider_gemini_fixture_contract(self):
        provider_spec = self._spec_by_provider("gemini")
        payload = self._fixture_payload("gemini_usage_response.json")
        with mock.patch.dict(
            os.environ,
            self._env_for_spec(provider_spec),
            clear=False,
        ), mock.patch.object(provider_cost_ingest, "_request_json", return_value=payload):
            records, status = provider_cost_ingest.ingest_provider(
                provider_spec,
                start_ts=1700000000,
                end_ts=1700003600,
                page_size=50,
                max_pages=1,
                timeout_sec=5,
                retries=0,
                backoff_sec=1,
            )
        self.assertTrue(status["ok"])
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["records"], 1)
        self.assertEqual(records[0]["provider"], "gemini")
        self.assertEqual(records[0]["model"], "gemini-2.5-flash")
        self.assertEqual(records[0]["source_of_truth"], "authoritative_provider_api")
        self.assertEqual(records[0]["currency"], "USD")
        self.assertGreater(records[0]["total_tokens"], 0)
        self.assertGreater(records[0]["total_cost_usd"], 0.0)

    def test_ingest_provider_request_error_is_failed_without_records(self):
        provider_spec = provider_cost_ingest.ProviderSpec(
            provider="openai",
            url_env="TEST_PROVIDER_URL",
            key_envs=("TEST_PROVIDER_KEY",),
            auth_mode="bearer",
        )
        with mock.patch.dict(
            os.environ,
            {
                "TEST_PROVIDER_URL": "https://example.test/v1/usage",
                "TEST_PROVIDER_KEY": "secret",
            },
            clear=False,
        ), mock.patch.object(provider_cost_ingest, "_request_json", side_effect=RuntimeError("boom")):
            records, status = provider_cost_ingest.ingest_provider(
                provider_spec,
                start_ts=1700000000,
                end_ts=1700003600,
                page_size=50,
                max_pages=1,
                timeout_sec=5,
                retries=0,
                backoff_sec=1,
            )

        self.assertEqual(records, [])
        self.assertFalse(status["ok"])
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["records"], 0)
        self.assertEqual(status["pages"], 0)
        self.assertEqual(status["errors"], ["boom"])

    def test_extract_helpers_and_build_url(self):
        self.assertEqual(provider_cost_ingest._extract_items([{"a": 1}, "x"]), [{"a": 1}])
        payload = {"billing": {"items": [{"b": 2}]}, "next_cursor": "c1"}
        self.assertEqual(provider_cost_ingest._extract_items(payload), [{"b": 2}])
        self.assertEqual(provider_cost_ingest._extract_next_cursor(payload), "c1")
        url = provider_cost_ingest._build_url(
            "https://example.test/usage?x=1",
            start_ts=1,
            end_ts=2,
            cursor="abc",
            limit=50,
            api_key="k",
            auth_mode="query-key",
        )
        self.assertIn("cursor=abc", url)
        self.assertIn("key=k", url)

    def test_resolve_api_key_and_request_json_retry(self):
        spec = provider_cost_ingest.ProviderSpec(
            provider="x",
            url_env="X_URL",
            key_envs=("X_KEY", "Y_KEY"),
            auth_mode="bearer",
        )
        with mock.patch.dict(os.environ, {"Y_KEY": "fallback"}, clear=False):
            self.assertEqual(provider_cost_ingest._resolve_api_key(spec), "fallback")

        failing = RuntimeError("bad")
        with mock.patch.object(provider_cost_ingest, "urlopen", side_effect=failing), mock.patch.object(
            provider_cost_ingest.time, "sleep", return_value=None
        ):
            with self.assertRaises(RuntimeError):
                provider_cost_ingest._request_json(
                    "https://example.test",
                    api_key="k",
                    auth_mode="bearer",
                    timeout_sec=1,
                    retries=1,
                    backoff_sec=1,
                    extra_headers=(),
                )


if __name__ == "__main__":
    unittest.main()
