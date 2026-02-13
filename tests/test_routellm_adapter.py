"""Tests for Issue #18: RouteLLM adapter with deterministic fallback."""

import os
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.routellm_adapter import (
    RouteLLMAdapter,
    RouteLLMConfig,
    RoutingResult,
    _classify_complexity_simple,
    deterministic_route,
    routellm_route,
)


class RouteLLMConfigTests(unittest.TestCase):
    def test_default_config(self):
        cfg = RouteLLMConfig()
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.kill_switch)
        self.assertEqual(cfg.router_model, "mf")
        self.assertAlmostEqual(cfg.threshold, 0.5)

    def test_from_env(self):
        env = {
            "ORXAQ_ROUTELLM_ENABLED": "true",
            "ORXAQ_ROUTELLM_ROUTER_MODEL": "causal_llm",
            "ORXAQ_ROUTELLM_STRONG_MODEL": "gpt-4o",
            "ORXAQ_ROUTELLM_WEAK_MODEL": "gpt-4o-mini",
            "ORXAQ_ROUTELLM_THRESHOLD": "0.7",
            "ORXAQ_ROUTELLM_TIMEOUT_SEC": "10",
            "ORXAQ_ROUTELLM_KILL_SWITCH": "false",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = RouteLLMConfig.from_env()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.router_model, "causal_llm")
        self.assertEqual(cfg.strong_model, "gpt-4o")
        self.assertAlmostEqual(cfg.threshold, 0.7)
        self.assertFalse(cfg.kill_switch)

    def test_from_dict(self):
        cfg = RouteLLMConfig.from_dict({
            "enabled": True,
            "router_model": "mf",
            "threshold": 0.3,
        })
        self.assertTrue(cfg.enabled)
        self.assertAlmostEqual(cfg.threshold, 0.3)

    def test_to_dict_roundtrip(self):
        cfg = RouteLLMConfig(enabled=True, threshold=0.8)
        d = cfg.to_dict()
        cfg2 = RouteLLMConfig.from_dict(d)
        self.assertEqual(cfg.enabled, cfg2.enabled)
        self.assertAlmostEqual(cfg.threshold, cfg2.threshold)


class ClassifyComplexityTests(unittest.TestCase):
    def test_critical_keywords(self):
        self.assertEqual(_classify_complexity_simple("run a security audit on auth"), "critical")
        self.assertEqual(_classify_complexity_simple("production deploy pipeline"), "critical")

    def test_high_keywords(self):
        self.assertEqual(_classify_complexity_simple("architect the event system"), "high")
        self.assertEqual(_classify_complexity_simple("debug memory leak"), "high")

    def test_medium_keywords(self):
        self.assertEqual(_classify_complexity_simple("implement caching layer"), "medium")
        self.assertEqual(_classify_complexity_simple("write unit tests"), "medium")

    def test_low_default(self):
        self.assertEqual(_classify_complexity_simple("update readme"), "low")


class DeterministicRouteTests(unittest.TestCase):
    def test_critical_gets_strong_model(self):
        cfg = RouteLLMConfig(strong_model="strong-model", weak_model="weak-model")
        result = deterministic_route("security audit", cfg)
        self.assertEqual(result.source, "deterministic_fallback")
        self.assertEqual(result.selected_model, "strong-model")
        self.assertIn("critical", result.reason)

    def test_high_gets_strong_model(self):
        cfg = RouteLLMConfig(strong_model="strong", weak_model="weak")
        result = deterministic_route("architect event system", cfg)
        self.assertEqual(result.selected_model, "strong")

    def test_medium_gets_weak_model(self):
        cfg = RouteLLMConfig(strong_model="strong", weak_model="weak")
        result = deterministic_route("implement a cache", cfg)
        self.assertEqual(result.selected_model, "weak")

    def test_low_gets_weak_model(self):
        cfg = RouteLLMConfig(strong_model="strong", weak_model="weak")
        result = deterministic_route("update config file", cfg)
        self.assertEqual(result.selected_model, "weak")

    def test_result_has_timestamp(self):
        result = deterministic_route("do something", RouteLLMConfig())
        self.assertTrue(result.timestamp)


class RouteLLMRouteTests(unittest.TestCase):
    def test_falls_back_when_library_unavailable(self):
        with mock.patch(
            "orxaq_autonomy.routellm_adapter._routellm_available", return_value=False
        ):
            result = routellm_route("implement feature", RouteLLMConfig())
        self.assertEqual(result.source, "deterministic_fallback")
        self.assertIn("not installed", result.error)

    def test_falls_back_on_exception(self):
        # The Controller import happens lazily inside routellm_route, so we
        # mock the import mechanism to raise an error when routellm.controller
        # is imported.
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def failing_import(name, *args, **kwargs):
            if "routellm" in name:
                raise ImportError("simulated routellm import failure")
            return original_import(name, *args, **kwargs)

        with mock.patch(
            "orxaq_autonomy.routellm_adapter._routellm_available", return_value=True
        ), mock.patch("builtins.__import__", side_effect=failing_import):
            result = routellm_route("implement feature", RouteLLMConfig())
        self.assertEqual(result.source, "deterministic_fallback")
        self.assertIn("failed", result.error)


class RouteLLMAdapterTests(unittest.TestCase):
    def test_disabled_uses_deterministic(self):
        adapter = RouteLLMAdapter(config=RouteLLMConfig(enabled=False))
        self.assertFalse(adapter.is_active())
        result = adapter.route("implement feature")
        self.assertEqual(result.source, "deterministic_fallback")
        self.assertIn("disabled", result.reason)

    def test_kill_switch_forces_deterministic(self):
        adapter = RouteLLMAdapter(
            config=RouteLLMConfig(enabled=True, kill_switch=True)
        )
        self.assertFalse(adapter.is_active())
        result = adapter.route("architect system")
        self.assertEqual(result.source, "deterministic_fallback")
        self.assertIn("kill_switch", result.reason)

    def test_decisions_are_recorded(self):
        adapter = RouteLLMAdapter(config=RouteLLMConfig(enabled=False))
        adapter.route("task 1")
        adapter.route("task 2")
        self.assertEqual(len(adapter.decisions), 2)

    def test_status_includes_config(self):
        adapter = RouteLLMAdapter(config=RouteLLMConfig(enabled=False))
        status = adapter.status()
        self.assertFalse(status["active"])
        self.assertIn("config", status)
        self.assertIn("decisions_count", status)


if __name__ == "__main__":
    unittest.main()
