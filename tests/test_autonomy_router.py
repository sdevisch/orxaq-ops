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

    def _write_profile(self, root: pathlib.Path, name: str, payload: dict) -> pathlib.Path:
        profile_dir = root / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        path = profile_dir / f"{name}.yaml"
        path.write_text(json.dumps(payload), encoding="utf-8")
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

    def test_apply_router_profile_writes_active_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(root)
            self._write_profile(
                root,
                "local",
                {
                    "schema_version": "router-profile.v1",
                    "required_providers": ["local"],
                    "router": {"fallback_order": ["L0", "L1"]},
                    "lanes": {"L0": ["local"], "L1": ["remote"]},
                },
            )
            output = root / "config" / "router.active.yaml"
            payload = router.apply_router_profile(
                root=str(root),
                profile_name="local",
                base_config_path=str(config),
                profiles_dir=str(root / "profiles"),
                output_path=str(output),
            )
            self.assertTrue(payload["ok"])
            self.assertTrue(output.exists())
            active = json.loads(output.read_text(encoding="utf-8"))
            required = {row["name"]: row.get("required") for row in active["providers"]}
            self.assertTrue(required["local"])
            self.assertFalse(required["remote"])

    def test_run_router_check_with_profile_uses_required_from_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(root)
            self._write_profile(
                root,
                "travel",
                {
                    "schema_version": "router-profile.v1",
                    "required_providers": ["remote"],
                    "router": {"fallback_order": ["L1"]},
                    "lanes": {"L1": ["remote"]},
                },
            )
            output = root / "artifacts" / "router_check.json"
            with mock.patch(
                "orxaq_autonomy.router.urllib_request.urlopen",
                side_effect=OSError("network unreachable"),
            ):
                report = router.run_router_check(
                    root=str(root),
                    config_path=str(config),
                    output_path=str(output),
                    profile="travel",
                    profiles_dir=str(root / "profiles"),
                    active_config_output=str(root / "config" / "router.active.yaml"),
                    lane="L1",
                    timeout_sec=5,
                )
            self.assertEqual(report["summary"]["required_down"], 1)
            self.assertFalse(report["summary"]["overall_ok"])


    def test_run_lanes_status_all_up(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(root)
            output = root / "artifacts" / "lanes_status.json"
            with mock.patch(
                "orxaq_autonomy.router.urllib_request.urlopen",
                return_value=_FakeResponse({"data": [{"id": "m1"}]}),
            ):
                report = router.run_lanes_status(
                    root=str(root),
                    config_path=str(config),
                    output_path=str(output),
                    timeout_sec=5,
                )
            self.assertTrue(output.exists())
            self.assertEqual(report["summary"]["total_lanes"], 2)
            self.assertEqual(report["summary"]["healthy_lanes"], 2)
            self.assertTrue(report["summary"]["all_healthy"])
            self.assertTrue(report["lanes"]["L0"]["healthy"])
            self.assertTrue(report["lanes"]["L1"]["healthy"])

    def test_run_lanes_status_one_lane_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config = self._write_config(root)
            output = root / "artifacts" / "lanes_status.json"

            def selective_urlopen(req, **kwargs):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "127.0.0.1" in url:
                    raise OSError("connection refused")
                return _FakeResponse({"data": [{"id": "m1"}]})

            with mock.patch(
                "orxaq_autonomy.router.urllib_request.urlopen",
                side_effect=selective_urlopen,
            ):
                report = router.run_lanes_status(
                    root=str(root),
                    config_path=str(config),
                    output_path=str(output),
                    timeout_sec=5,
                )
            self.assertEqual(report["summary"]["healthy_lanes"], 1)
            self.assertEqual(report["summary"]["degraded_lanes"], 1)
            self.assertFalse(report["summary"]["all_healthy"])
            self.assertFalse(report["lanes"]["L0"]["healthy"])
            self.assertTrue(report["lanes"]["L1"]["healthy"])

    def test_run_lanes_status_missing_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report = router.run_lanes_status(
                root=str(root),
                config_path="./nonexistent.yaml",
                output_path=str(root / "out.json"),
            )
            self.assertFalse(report["ok"])
            self.assertIn("not found", report["error"])

    def test_run_lanes_status_no_lanes_defined(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "providers": [
                    {"name": "local", "kind": "openai_compat", "base_url": "http://127.0.0.1:1234/v1"},
                ],
            }), encoding="utf-8")
            output = root / "out.json"
            with mock.patch(
                "orxaq_autonomy.router.urllib_request.urlopen",
                return_value=_FakeResponse({"data": []}),
            ):
                report = router.run_lanes_status(
                    root=str(root),
                    config_path=str(config_path),
                    output_path=str(output),
                )
            self.assertEqual(report["summary"]["total_lanes"], 0)
            self.assertFalse(report["summary"]["all_healthy"])


if __name__ == "__main__":
    unittest.main()
