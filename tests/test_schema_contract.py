import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _walk_objects(node, path, depth):
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    if node_type == "object" and isinstance(node.get("properties"), dict):
        yield path, depth, node
    props = node.get("properties")
    if isinstance(props, dict):
        for key, value in props.items():
            yield from _walk_objects(value, path + ("properties", key), depth + 1)
    items = node.get("items")
    if isinstance(items, dict):
        yield from _walk_objects(items, path + ("items",), depth + 1)


class CodexSchemaContractTests(unittest.TestCase):
    def test_top_level_required_includes_all_properties(self):
        schema_path = ROOT / "config" / "codex_result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(set(schema.get("required", [])), set(schema.get("properties", {}).keys()))

    def test_usage_object_has_required_all_properties(self):
        schema_path = ROOT / "config" / "codex_result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        usage = schema["properties"]["usage"]
        self.assertIsInstance(usage.get("required"), list)
        self.assertEqual(set(usage["required"]), set(usage["properties"].keys()))

    def test_nested_objects_with_strict_properties_define_required(self):
        schema_path = ROOT / "config" / "codex_result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        violations = []
        for path, depth, obj in _walk_objects(schema, tuple(), 0):
            if depth == 0:
                # Top-level object allows optional fields (for example `usage`).
                continue
            if obj.get("additionalProperties", True) is not False:
                continue
            props = obj.get("properties", {})
            required = obj.get("required", [])
            if not isinstance(required, list):
                violations.append((path, "missing_required_list"))
                continue
            missing = sorted(set(props.keys()) - set(required))
            if missing:
                violations.append((path, f"missing_required_keys={missing}"))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
