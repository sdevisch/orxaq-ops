import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.context import write_default_skill_protocol
from orxaq_autonomy.protocols import MCPContextBundle, SkillProtocolSpec, load_mcp_context, load_skill_protocol


class ProtocolTests(unittest.TestCase):
    def test_load_skill_protocol_defaults(self):
        protocol = load_skill_protocol(None)
        self.assertEqual(protocol.name, "orxaq-autonomy")
        self.assertEqual(protocol.version, "2")
        self.assertTrue(protocol.required_behaviors)
        self.assertIn("issue-first-workflow", protocol.required_behaviors)
        self.assertIn("request-cross-model-review", protocol.required_behaviors)
        self.assertIn("attach-review-evidence", protocol.required_behaviors)
        self.assertIn("resolve-conflicts-in-pr", protocol.required_behaviors)

    def test_load_skill_protocol_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "skill.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "custom-skill",
                        "version": "2",
                        "description": "x",
                        "required_behaviors": ["a", "b"],
                        "filetype_policy": "safe",
                    }
                ),
                encoding="utf-8",
            )
            protocol = load_skill_protocol(path)
            self.assertEqual(protocol.name, "custom-skill")
            self.assertEqual(protocol.version, "2")
            self.assertEqual(protocol.required_behaviors, ["a", "b"])

    def test_load_skill_protocol_non_object_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "skill.json"
            path.write_text('["x"]', encoding="utf-8")
            with self.assertRaises(ValueError):
                SkillProtocolSpec.from_json_file(path)

    def test_load_mcp_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "mcp.json"
            path.write_text(
                json.dumps(
                    {
                        "resources": [
                            {"id": "1", "text": "alpha"},
                            {"id": "2", "content": "beta"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            bundle = load_mcp_context(path)
            self.assertIsNotNone(bundle)
            rendered = bundle.render_context()
            self.assertIn("alpha", rendered)
            self.assertIn("beta", rendered)

    def test_load_mcp_context_edge_cases(self):
        self.assertIsNone(load_mcp_context(None))
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "missing.json"
            self.assertIsNone(load_mcp_context(missing))

            path = pathlib.Path(tmp) / "mcp.json"
            path.write_text(
                json.dumps({"resources": ["bad", {"text": "  "}, {"text": "ok"}, {"content": "z"}]}),
                encoding="utf-8",
            )
            bundle = load_mcp_context(path, max_snippets=1, max_chars=1)
            assert bundle is not None
            self.assertEqual(bundle.snippets, ["o"])
            self.assertIn("MCP context", bundle.render_context())

    def test_render_context_empty_snippets(self):
        bundle = MCPContextBundle(source="x", snippets=[])
        self.assertEqual(bundle.render_context(), "")

    def test_write_default_skill_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "config" / "skill_protocol.json"
            write_default_skill_protocol(out)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["name"], "orxaq-autonomy")
            self.assertEqual(payload["version"], "2")
            self.assertIn("required_behaviors", payload)
            self.assertIn("issue-first-workflow", payload["required_behaviors"])
            self.assertIn("branch-from-issue", payload["required_behaviors"])
            self.assertIn("commit-and-push-regularly", payload["required_behaviors"])
            self.assertIn("attach-review-evidence", payload["required_behaviors"])


if __name__ == "__main__":
    unittest.main()
