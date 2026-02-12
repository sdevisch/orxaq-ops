import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.event_mesh import (
    EventMeshConfig,
    dispatch_events,
    ensure_event_mesh_layout,
    event_mesh_status,
    export_events_for_coordination,
    import_events_from_coordination,
    publish_event,
    read_event_log,
    write_node_manifest,
)


class EventMeshTests(unittest.TestCase):
    def test_publish_and_dispatch_generates_followups(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = EventMeshConfig.from_root(root)
            ensure_event_mesh_layout(cfg)

            publish_event(
                cfg,
                topic="scheduling",
                event_type="task.enqueued",
                payload={
                    "task_id": "task-1",
                    "routing_options": [
                        {"lane_id": "lane-a", "provider": "codex", "model": "gpt-5", "score": 0.9, "healthy": True},
                        {"lane_id": "lane-b", "provider": "gemini", "model": "gemini-2.5", "score": 0.4, "healthy": True},
                    ],
                },
            )
            result = dispatch_events(cfg, max_events=20)
            self.assertTrue(result["ok"])
            self.assertGreaterEqual(result["followup_events"], 2)

            events = read_event_log(cfg.events_file)
            event_types = {str(item.get("event_type", "")) for item in events}
            self.assertIn("task.scheduled", event_types)
            self.assertIn("route.requested", event_types)
            self.assertIn("route.selected", event_types)

    def test_export_and_import_via_coordination_outbox(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = EventMeshConfig.from_root(root)
            ensure_event_mesh_layout(cfg)

            publish_event(cfg, topic="monitoring", event_type="heartbeat.changed", payload={"state": "ok"})
            exported = export_events_for_coordination(cfg, max_events=10)
            self.assertTrue(exported["ok"])
            self.assertEqual(exported["exported_events"], 1)

            # Simulate a second node writing to the shared GitHub-ledger outbox.
            remote_dir = cfg.coordination_outbox_dir / "node-remote"
            remote_dir.mkdir(parents=True, exist_ok=True)
            remote_event = {
                "event_id": "evt_remote_1",
                "timestamp": "2026-02-10T00:00:00+00:00",
                "topic": "routing",
                "event_type": "route.requested",
                "node_id": "node-remote",
                "source": "remote",
                "causation_id": "",
                "payload": {"options": []},
            }
            (remote_dir / "evt_remote_1.json").write_text(json.dumps(remote_event), encoding="utf-8")

            imported = import_events_from_coordination(cfg, max_events=20)
            self.assertTrue(imported["ok"])
            self.assertGreaterEqual(imported["imported_events"], 1)
            all_events = read_event_log(cfg.events_file)
            self.assertTrue(any(str(item.get("event_id", "")) == "evt_remote_1" for item in all_events))

    def test_manifest_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = EventMeshConfig.from_root(root)
            manifest = write_node_manifest(cfg, capabilities=["monitoring", "routing"])
            self.assertEqual(manifest["node_id"], cfg.node_id)
            node_file = cfg.coordination_nodes_dir / f"{cfg.node_id}.json"
            self.assertTrue(node_file.exists())

            status = event_mesh_status(cfg)
            self.assertTrue(status["ok"])
            self.assertEqual(status["node_id"], cfg.node_id)
            self.assertEqual(status["latest_leader_epoch"], -1)
            self.assertEqual(status["latest_command"], {})

    def test_status_reports_latest_command_and_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = EventMeshConfig.from_root(root)
            ensure_event_mesh_layout(cfg)
            command_log = root / "artifacts" / "autonomy" / "event_mesh" / "commands.ndjson"
            command_log.parent.mkdir(parents=True, exist_ok=True)
            command_log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-02-10T00:00:00+00:00",
                                "command_id": "cmd-1",
                                "action": "scale_up",
                                "lane_id": "lane-a",
                                "outcome": "applied",
                                "reason": "started_lane",
                                "leader_epoch": 3,
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-02-10T00:00:01+00:00",
                                "command_id": "cmd-2",
                                "action": "scale_down",
                                "lane_id": "lane-b",
                                "outcome": "rejected_leader_fence",
                                "reason": "leader_fence_epoch_mismatch",
                                "leader_epoch": 4,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            status = event_mesh_status(cfg)
            self.assertEqual(status["latest_leader_epoch"], 4)
            self.assertEqual(status["latest_command"]["command_id"], "cmd-2")
            self.assertEqual(status["command_outcomes"]["applied"], 1)
            self.assertEqual(status["command_outcomes"]["rejected_leader_fence"], 1)

    def test_dispatch_produces_scaling_decision_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = EventMeshConfig.from_root(root)
            ensure_event_mesh_layout(cfg)

            publish_event(
                cfg,
                topic="scheduling",
                event_type="lanes.ensure.summary",
                payload={
                    "requested_lane": "all_enabled",
                    "started_count": 0,
                    "restarted_count": 0,
                    "scaled_up_count": 0,
                    "scaled_down_count": 0,
                    "failed_count": 0,
                    "parallel_groups_at_limit": 2,
                },
            )
            dispatch_events(cfg, max_events=20)
            events = read_event_log(cfg.events_file)
            scaling = [
                item
                for item in events
                if str(item.get("topic", "")).strip() == "scaling"
                and str(item.get("event_type", "")).strip() == "decision.made"
            ]
            self.assertEqual(len(scaling), 1)
            self.assertEqual(scaling[0]["payload"]["action"], "scale_up")

    def test_dispatch_produces_scale_down_on_lane_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = EventMeshConfig.from_root(root)
            ensure_event_mesh_layout(cfg)

            publish_event(
                cfg,
                topic="monitoring",
                event_type="lane.ensure_failed",
                payload={"lane_id": "lane-a", "error": "boom"},
            )
            dispatch_events(cfg, max_events=20)
            events = read_event_log(cfg.events_file)
            scaling = [
                item
                for item in events
                if str(item.get("topic", "")).strip() == "scaling"
                and str(item.get("event_type", "")).strip() == "decision.made"
            ]
            self.assertEqual(len(scaling), 1)
            self.assertEqual(scaling[0]["payload"]["action"], "scale_down")

    def test_dispatch_decision_made_emits_command_requested(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = EventMeshConfig.from_root(root)
            ensure_event_mesh_layout(cfg)

            publish_event(
                cfg,
                topic="scaling",
                event_type="decision.made",
                payload={
                    "action": "scale_down",
                    "reason": "failures_present",
                    "target_delta": -1,
                    "requested_lane": "lane-a",
                    "leader_epoch": 7,
                    "decision_table_version": "scaling_v2",
                    "execution_dag_id": "dag-lane-a",
                    "causal_hypothesis_id": "hypothesis-1",
                },
            )
            dispatch_events(cfg, max_events=20)
            events = read_event_log(cfg.events_file)
            commands = [
                item
                for item in events
                if str(item.get("topic", "")).strip() == "scaling"
                and str(item.get("event_type", "")).strip() == "command.requested"
            ]
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]["payload"]["action"], "scale_down")
            self.assertEqual(commands[0]["payload"]["target_lane"], "lane-a")
            self.assertEqual(commands[0]["payload"]["leader_epoch"], 7)
            self.assertEqual(commands[0]["payload"]["decision_table_version"], "scaling_v2")
            self.assertEqual(commands[0]["payload"]["execution_dag_id"], "dag-lane-a")
            self.assertEqual(commands[0]["payload"]["causal_hypothesis_id"], "hypothesis-1")
            self.assertIsInstance(commands[0]["payload"].get("decision_trace", {}), dict)
            self.assertIsInstance(commands[0]["payload"].get("causal_gate", {}), dict)
            self.assertTrue(str(commands[0]["payload"]["command_id"]).startswith("cmd_"))
            self.assertTrue(str(commands[0]["payload"]["issued_at_utc"]).strip())

    def test_dispatch_summary_propagates_leader_epoch_into_decision(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = EventMeshConfig.from_root(root)
            ensure_event_mesh_layout(cfg)

            publish_event(
                cfg,
                topic="scheduling",
                event_type="lanes.ensure.summary",
                payload={
                    "requested_lane": "all_enabled",
                    "started_count": 0,
                    "restarted_count": 0,
                    "scaled_up_count": 0,
                    "scaled_down_count": 0,
                    "failed_count": 0,
                    "parallel_groups_at_limit": 1,
                },
            )
            dispatch_events(cfg, max_events=20)
            events = read_event_log(cfg.events_file)
            decision_requested = [
                item
                for item in events
                if str(item.get("topic", "")).strip() == "scaling"
                and str(item.get("event_type", "")).strip() == "decision.requested"
            ]
            self.assertEqual(len(decision_requested), 1)
            payload = decision_requested[0].get("payload", {})
            self.assertGreaterEqual(int(payload.get("leader_epoch", 0)), 1)
            self.assertEqual(str(payload.get("decision_table_version", "")), "scaling_v1")

    def test_dispatch_enforced_causal_gate_blocks_disruptive_scale_down(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            with mock.patch.dict(os.environ, {"ORXAQ_AUTONOMY_CAUSAL_GATE_MODE": "enforced"}, clear=False):
                cfg = EventMeshConfig.from_root(root)
                ensure_event_mesh_layout(cfg)
                publish_event(
                    cfg,
                    topic="scaling",
                    event_type="decision.made",
                    payload={
                        "action": "scale_down",
                        "reason": "failures_present",
                        "target_delta": -1,
                        "requested_lane": "all_enabled",
                    },
                )
                dispatch_events(cfg, max_events=20)
                events = read_event_log(cfg.events_file)
                commands = [
                    item
                    for item in events
                    if str(item.get("topic", "")).strip() == "scaling"
                    and str(item.get("event_type", "")).strip() == "command.requested"
                ]
                self.assertEqual(commands, [])

    def test_dispatch_decision_made_skips_command_on_follower_node(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            lease_file = root / "state" / "event_mesh" / "leader_lease.json"
            lease_file.parent.mkdir(parents=True, exist_ok=True)
            lease_file.write_text(
                json.dumps(
                    {
                        "leader_id": "node-a",
                        "epoch": 4,
                        "lease_expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
                        "ttl_sec": 45,
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"ORXAQ_AUTONOMY_NODE_ID": "node-b"}, clear=False):
                cfg = EventMeshConfig.from_root(root)
                ensure_event_mesh_layout(cfg)
                publish_event(
                    cfg,
                    topic="scaling",
                    event_type="decision.made",
                    payload={
                        "action": "scale_down",
                        "reason": "failures_present",
                        "target_delta": -1,
                        "requested_lane": "lane-a",
                        "leader_epoch": 4,
                    },
                )
                dispatch_events(cfg, max_events=20)
                events = read_event_log(cfg.events_file)
                commands = [
                    item
                    for item in events
                    if str(item.get("topic", "")).strip() == "scaling"
                    and str(item.get("event_type", "")).strip() == "command.requested"
                ]
                self.assertEqual(commands, [])
