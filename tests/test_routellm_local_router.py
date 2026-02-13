"""Tests for the RouteLLM local router HTTP shim (issue #58)."""

import importlib
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

root = pathlib.Path(__file__).resolve().parents[1]
scripts_dir = root / "scripts"

# Import the router module directly
spec = importlib.util.spec_from_file_location(
    "routellm_local_router", scripts_dir / "routellm_local_router.py"
)
router_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router_mod)


class TestDeterministicRoute(unittest.TestCase):
    def test_critical_routes_to_strong(self):
        result = router_mod._deterministic_route(
            "security audit of production", "strong-model", "weak-model"
        )
        self.assertEqual(result["model"], "strong-model")
        self.assertIn(result["complexity"], ("high", "critical"))

    def test_low_routes_to_weak(self):
        result = router_mod._deterministic_route(
            "hello world", "strong-model", "weak-model"
        )
        self.assertEqual(result["model"], "weak-model")
        self.assertEqual(result["complexity"], "low")

    def test_implement_routes_to_weak(self):
        result = router_mod._deterministic_route(
            "implement a caching layer", "strong-model", "weak-model"
        )
        self.assertEqual(result["model"], "weak-model")
        self.assertEqual(result["complexity"], "medium")

    def test_design_routes_to_strong(self):
        result = router_mod._deterministic_route(
            "architect a new microservice", "strong-model", "weak-model"
        )
        self.assertEqual(result["model"], "strong-model")

    def test_result_has_required_fields(self):
        result = router_mod._deterministic_route("test", "s", "w")
        self.assertIn("model", result)
        self.assertIn("source", result)
        self.assertIn("complexity", result)
        self.assertIn("timestamp", result)
        self.assertEqual(result["source"], "deterministic_fallback")


class TestLoadPolicy(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        result = router_mod._load_policy("/nonexistent/path.json")
        self.assertEqual(result, {})

    def test_none_returns_empty_dict(self):
        result = router_mod._load_policy(None)
        self.assertEqual(result, {})

    def test_valid_json_loaded(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"strong_model": "test-model"}, f)
            f.flush()
            result = router_mod._load_policy(f.name)
        self.assertEqual(result["strong_model"], "test-model")

    def test_invalid_json_returns_empty_dict(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("not json {{{")
            f.flush()
            result = router_mod._load_policy(f.name)
        self.assertEqual(result, {})


class TestRouterHTTPServer(unittest.TestCase):
    """Integration tests for the HTTP router server."""

    @classmethod
    def setUpClass(cls):
        """Start the router server in a background thread."""
        import socket

        # Find a free port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        cls.base_url = f"http://127.0.0.1:{cls.port}"

        # Configure handler
        router_mod.RouterHandler.profile = "test"
        router_mod.RouterHandler.strong_model = "test-strong"
        router_mod.RouterHandler.weak_model = "test-weak"
        router_mod.RouterHandler.policy = {"test": True}

        from http.server import HTTPServer

        cls.server = HTTPServer(("127.0.0.1", cls.port), router_mod.RouterHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_health_endpoint(self):
        resp = urllib.request.urlopen(f"{self.base_url}/health")
        data = json.loads(resp.read())
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["profile"], "test")

    def test_status_endpoint(self):
        resp = urllib.request.urlopen(f"{self.base_url}/status")
        data = json.loads(resp.read())
        self.assertEqual(data["profile"], "test")
        self.assertEqual(data["strong_model"], "test-strong")
        self.assertEqual(data["weak_model"], "test-weak")

    def test_route_endpoint(self):
        payload = json.dumps({"description": "security audit"}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/route",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        self.assertEqual(data["model"], "test-strong")
        self.assertEqual(data["source"], "deterministic_fallback")

    def test_route_low_complexity(self):
        payload = json.dumps({"description": "hello world"}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/route",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        self.assertEqual(data["model"], "test-weak")

    def test_route_missing_description(self):
        payload = json.dumps({}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/route",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            self.fail("Expected HTTP 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_404_for_unknown_path(self):
        try:
            urllib.request.urlopen(f"{self.base_url}/nonexistent")
            self.fail("Expected HTTP 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
