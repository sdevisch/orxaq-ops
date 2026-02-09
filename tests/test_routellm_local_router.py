import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import routellm_local_router as local_router


class LocalRouteLLMRouterTests(unittest.TestCase):
    def _policy(self):
        return {
            "version": 2,
            "enabled": True,
            "providers": {
                "codex": {
                    "enabled": True,
                    "fallback_model": "model-fast-cheap",
                    "respect_requested_model": False,
                    "default_objective": "cost_speed",
                    "allowed_models": [
                        "model-fast-cheap",
                        "model-balanced",
                        "model-quality",
                    ],
                }
            },
            "model_catalog": {
                "model-fast-cheap": {
                    "input_per_million": 0.1,
                    "output_per_million": 0.2,
                    "speed_tps": 250,
                    "quality_score": 50,
                    "max_context_tokens": 16000,
                    "local": True,
                },
                "model-balanced": {
                    "input_per_million": 0.5,
                    "output_per_million": 1.0,
                    "speed_tps": 140,
                    "quality_score": 72,
                    "max_context_tokens": 32000,
                    "local": True,
                },
                "model-quality": {
                    "input_per_million": 2.0,
                    "output_per_million": 4.0,
                    "speed_tps": 70,
                    "quality_score": 95,
                    "max_context_tokens": 128000,
                    "local": False,
                },
            },
        }

    def test_route_model_prefers_cost_speed_for_fast_profile(self):
        decision = local_router.route_model(
            self._policy(),
            "fast",
            {
                "provider": "codex",
                "prompt_difficulty_score": 20,
                "prompt_tokens_est": 1200,
            },
        )
        self.assertEqual(decision["model"], "model-fast-cheap")
        self.assertEqual(decision["objective"], "cost_speed")
        self.assertTrue(decision["strategy"].startswith("intelligent_"))
        self.assertGreater(decision["estimated_speed_tps"], 0)

    def test_route_model_prefers_quality_for_hard_prompt_on_strong_profile(self):
        decision = local_router.route_model(
            self._policy(),
            "strong",
            {
                "provider": "codex",
                "prompt_difficulty_score": 95,
                "prompt_tokens_est": 12000,
                "prompt": "Perform architecture and security hardening",
            },
        )
        self.assertEqual(decision["objective"], "quality")
        self.assertEqual(decision["model"], "model-quality")
        self.assertGreaterEqual(decision["estimated_cost_per_million"], 1.0)

    def test_route_model_returns_provider_disabled_fallback(self):
        policy = self._policy()
        policy["providers"]["codex"]["enabled"] = False
        decision = local_router.route_model(
            policy,
            "fast",
            {
                "provider": "codex",
                "prompt_difficulty_score": 10,
                "prompt_tokens_est": 100,
            },
        )
        self.assertEqual(decision["strategy"], "provider_disabled_fallback")
        self.assertTrue(decision["fallback_used"])
        self.assertEqual(decision["model"], "model-fast-cheap")

    def test_route_model_provider_disabled_enforces_allowed_fallback_when_requested_not_allowed(self):
        policy = self._policy()
        policy["providers"]["codex"]["enabled"] = False
        policy["providers"]["codex"]["fallback_model"] = "outside-policy-model"
        decision = local_router.route_model(
            policy,
            "fast",
            {
                "provider": "codex",
                "requested_model": "rogue-model",
                "prompt_difficulty_score": 10,
                "prompt_tokens_est": 100,
            },
        )
        self.assertEqual(decision["model"], "model-fast-cheap")
        self.assertFalse(decision["requested_model_allowed"])
        self.assertIn("model-fast-cheap", decision["allowed_models"])


if __name__ == "__main__":
    unittest.main()
