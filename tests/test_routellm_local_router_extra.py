import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
import sys

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import routellm_local_router as local_router


class LocalRouteLLMRouterExtraTests(unittest.TestCase):
    def _policy(self):
        return {
            "providers": {
                "codex": {
                    "enabled": True,
                    "fallback_model": "m1",
                    "respect_requested_model": True,
                    "allowed_models": ["m1", "m2"],
                }
            },
            "model_catalog": {
                "m1": {"input_per_million": 1.0, "output_per_million": 2.0, "speed_tps": 50, "quality_score": 10},
                "m2": {"input_per_million": 2.0, "output_per_million": 3.0, "speed_tps": 80, "quality_score": 90},
            },
        }

    def test_load_policy_requires_object(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "policy.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                local_router._load_policy(path)

    def test_scalar_helpers(self):
        self.assertEqual(local_router._as_float("x", 1.2), 1.2)
        self.assertEqual(local_router._as_int("x", 7), 7)
        self.assertTrue(local_router._as_bool("yes", False))
        self.assertFalse(local_router._as_bool("off", True))

    def test_normalize_models_and_catalog(self):
        models = local_router._normalize_models("m1; m2, m1")
        self.assertEqual(models, ["m1", "m2"])
        catalog = local_router._normalize_model_catalog({"m1": {"input_per_million": "1"}})
        self.assertIn("m1", catalog)
        self.assertEqual(catalog["m1"]["input_per_million"], 1.0)

    def test_canonical_allowed_model(self):
        self.assertEqual(local_router._canonical_allowed_model("M1", ["m1", "m2"]), "m1")
        self.assertIsNone(local_router._canonical_allowed_model("m3", ["m1", "m2"]))

    def test_route_model_no_allowed_models_falls_back(self):
        policy = {"providers": {"codex": {"enabled": True, "allowed_models": [], "fallback_model": "f"}}}
        decision = local_router.route_model(policy, "fast", {"provider": "codex", "requested_model": "r"})
        self.assertEqual(decision["strategy"], "fallback")
        self.assertTrue(decision["fallback_used"])

    def test_route_model_respects_requested_model(self):
        decision = local_router.route_model(
            self._policy(),
            "fast",
            {"provider": "codex", "requested_model": "m2", "prompt_difficulty_score": 10, "prompt_tokens_est": 100},
        )
        self.assertEqual(decision["strategy"], "requested_model_allowed")
        self.assertEqual(decision["model"], "m2")

    def test_route_model_uses_objective_override(self):
        decision = local_router.route_model(
            self._policy(),
            "fast",
            {
                "provider": "codex",
                "requested_model": "",
                "optimization_target": "quality",
                "prompt_difficulty_score": 10,
                "prompt_tokens_est": 100,
            },
        )
        self.assertEqual(decision["objective"], "quality")


if __name__ == "__main__":
    unittest.main()
