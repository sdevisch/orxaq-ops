"""Production swarm command dashboard — data layer and HTTP handler.

Reads live artifacts from the autonomy runtime (health, lanes, heartbeats,
conversations, response metrics, git state) and serves them as JSON APIs
alongside an interactive HTML frontend.

Zero external dependencies beyond the Python standard library.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import parse as urllib_parse

from . import dashboard_actions


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | list[Any]:
    """Read a JSON file, returning {} on any error."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, (dict, list)) else {}


def _read_json_dict(path: Path) -> dict[str, Any]:
    result = _read_json(path)
    return result if isinstance(result, dict) else {}


def _read_ndjson(path: Path, *, tail: int = 200) -> list[dict[str, Any]]:
    """Read last *tail* lines of an NDJSON file."""
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if tail > 0:
        lines = lines[-tail:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    return out


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ── Lane Discovery ───────────────────────────────────────────────────────────

def discover_lanes(artifacts_dir: Path) -> list[dict[str, Any]]:
    """Discover all lanes from artifacts/autonomy/lanes/ directory."""
    lanes_dir = artifacts_dir / "autonomy" / "lanes"
    if not lanes_dir.is_dir():
        return []

    lanes: list[dict[str, Any]] = []
    for lane_dir in sorted(lanes_dir.iterdir()):
        if not lane_dir.is_dir():
            continue
        lane = _read_lane(lane_dir)
        if lane:
            lanes.append(lane)
    return lanes


def _read_lane(lane_dir: Path) -> dict[str, Any] | None:
    """Read a single lane's state from its directory."""
    lane_id = lane_dir.name
    config = _read_json_dict(lane_dir / "lane.json")
    state = _read_json_dict(lane_dir / "state.json")
    heartbeat = _read_json_dict(lane_dir / "heartbeat.json")
    metrics_summary = _read_json_dict(lane_dir / "response_metrics_summary.json")

    owner = str(config.get("owner", "unknown"))

    # Derive status from state.json tasks
    task_counts = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0}
    tasks: list[dict[str, Any]] = []
    for task_id, task_data in state.items():
        if not isinstance(task_data, dict):
            continue
        status = str(task_data.get("status", "unknown")).lower()
        if status in task_counts:
            task_counts[status] += 1
        tasks.append({
            "task_id": task_id,
            "status": status,
            "attempts": _safe_int(task_data.get("attempts", 0)),
            "deadlock_recoveries": _safe_int(task_data.get("deadlock_recoveries", 0)),
            "last_update": str(task_data.get("last_update", "")),
            "last_summary": str(task_data.get("last_summary", ""))[:200],
            "last_error": str(task_data.get("last_error", ""))[:200],
            "owner": str(task_data.get("owner", owner)),
        })

    # Determine overall lane status
    if task_counts["blocked"] > 0:
        lane_status = "blocked"
    elif task_counts["in_progress"] > 0:
        lane_status = "in_progress"
    elif task_counts["pending"] > 0:
        lane_status = "pending"
    elif task_counts["done"] > 0:
        lane_status = "done"
    else:
        lane_status = "idle"

    return {
        "lane_id": lane_id,
        "owner": owner,
        "status": lane_status,
        "task_counts": task_counts,
        "tasks": tasks,
        "heartbeat": {
            "cycle": _safe_int(heartbeat.get("cycle", 0)),
            "phase": str(heartbeat.get("phase", "")),
            "pid": _safe_int(heartbeat.get("pid", 0)),
            "timestamp": str(heartbeat.get("timestamp", "")),
            "message": str(heartbeat.get("message", ""))[:200],
        },
        "metrics": {
            "responses_total": _safe_int(metrics_summary.get("responses_total", 0)),
            "cost_usd_total": _safe_float(metrics_summary.get("cost_usd_total", 0)),
            "tokens_total": _safe_int(metrics_summary.get("tokens_total", 0)),
            "first_time_pass_rate": _safe_float(metrics_summary.get("first_time_pass_rate", 0)),
            "acceptance_pass_rate": _safe_float(metrics_summary.get("acceptance_pass_rate", 0)),
            "latency_sec_avg": _safe_float(
                (metrics_summary.get("by_owner", {}) or {}).get(owner, {}).get("latency_sec_avg", 0)
            ),
        },
        "config": {
            "execution_profile": str(config.get("execution_profile", "")),
            "continuous": bool(config.get("continuous", False)),
            "max_cycles": _safe_int(config.get("max_cycles", 0)),
            "max_attempts": _safe_int(config.get("max_attempts", 0)),
            "started_at": str(config.get("started_at", "")),
        },
    }


# ── Event Collection ─────────────────────────────────────────────────────────

def collect_events(artifacts_dir: Path, *, tail: int = 100) -> list[dict[str, Any]]:
    """Collect recent conversation events across all lanes."""
    lanes_dir = artifacts_dir / "autonomy" / "lanes"
    if not lanes_dir.is_dir():
        return []

    all_events: list[dict[str, Any]] = []
    for lane_dir in lanes_dir.iterdir():
        if not lane_dir.is_dir():
            continue
        conv_file = lane_dir / "conversations.ndjson"
        if not conv_file.exists():
            continue
        events = _read_ndjson(conv_file, tail=50)
        for evt in events:
            evt["lane_id"] = lane_dir.name
        all_events.extend(events)

    # Sort by timestamp descending
    all_events.sort(key=lambda e: str(e.get("timestamp", "")), reverse=True)
    return all_events[:tail]


# ── Health Snapshot ──────────────────────────────────────────────────────────

def collect_health(artifacts_dir: Path) -> dict[str, Any]:
    """Read the current health snapshot from artifacts."""
    health = _read_json_dict(artifacts_dir / "autonomy" / "health.json")
    dashboard_health = _read_json_dict(artifacts_dir / "autonomy" / "dashboard_health.json")
    return {
        "health": health,
        "collaboration": dashboard_health,
        "timestamp": _utc_now_iso(),
    }


# ── Metrics Aggregation ─────────────────────────────────────────────────────

def collect_metrics(artifacts_dir: Path) -> dict[str, Any]:
    """Collect aggregated response metrics from all lanes."""
    metrics_file = artifacts_dir / "autonomy" / "response_metrics_summary.json"
    global_metrics = _read_json_dict(metrics_file)

    # Also aggregate per-lane
    lanes_dir = artifacts_dir / "autonomy" / "lanes"
    per_lane: dict[str, Any] = {}
    if lanes_dir.is_dir():
        for lane_dir in lanes_dir.iterdir():
            if not lane_dir.is_dir():
                continue
            lane_metrics = _read_json_dict(lane_dir / "response_metrics_summary.json")
            if lane_metrics:
                per_lane[lane_dir.name] = {
                    "responses_total": _safe_int(lane_metrics.get("responses_total", 0)),
                    "cost_usd_total": _safe_float(lane_metrics.get("cost_usd_total", 0)),
                    "tokens_total": _safe_int(lane_metrics.get("tokens_total", 0)),
                    "first_time_pass_rate": _safe_float(lane_metrics.get("first_time_pass_rate", 0)),
                    "acceptance_pass_rate": _safe_float(lane_metrics.get("acceptance_pass_rate", 0)),
                }

    return {
        "global": global_metrics,
        "per_lane": per_lane,
        "timestamp": _utc_now_iso(),
    }


# ── Git State ────────────────────────────────────────────────────────────────

def collect_git_state(repo_dir: Path) -> dict[str, Any]:
    """Collect git repository state (branches, recent commits)."""
    result: dict[str, Any] = {"branches": [], "recent_commits": [], "branch_count": 0}

    try:
        branch_out = subprocess.run(
            ["git", "-C", str(repo_dir), "branch", "-a", "--format=%(refname:short)"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if branch_out.returncode == 0:
            branches = [b.strip() for b in branch_out.stdout.strip().splitlines() if b.strip()]
            result["branch_count"] = len(branches)
            result["branches"] = branches[:50]  # cap at 50
    except Exception:
        pass

    try:
        log_out = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "--oneline", "--all", "-20",
             "--format=%H|%h|%s|%an|%ar|%aI"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if log_out.returncode == 0:
            for line in log_out.stdout.strip().splitlines():
                parts = line.split("|", 5)
                if len(parts) >= 6:
                    result["recent_commits"].append({
                        "hash": parts[0], "short_hash": parts[1],
                        "message": parts[2], "author": parts[3],
                        "relative_time": parts[4], "timestamp": parts[5],
                    })
    except Exception:
        pass

    try:
        current_branch = subprocess.run(
            ["git", "-C", str(repo_dir), "branch", "--show-current"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if current_branch.returncode == 0:
            result["current_branch"] = current_branch.stdout.strip()
    except Exception:
        pass

    return result


# ── Connectivity ─────────────────────────────────────────────────────────────

def collect_connectivity(artifacts_dir: Path) -> dict[str, Any]:
    """Read model connectivity status."""
    conn = _read_json_dict(artifacts_dir / "model_connectivity.json")
    if not conn:
        conn = _read_json_dict(artifacts_dir.parent / "artifacts" / "model_connectivity.json")
    return conn


# ── Checkpoints ──────────────────────────────────────────────────────────────

def collect_checkpoints(artifacts_dir: Path) -> list[dict[str, Any]]:
    """Read recent checkpoint files."""
    cp_dir = artifacts_dir / "checkpoints"
    if not cp_dir.is_dir():
        return []
    files = sorted(cp_dir.glob("*.json"), key=lambda p: p.name, reverse=True)[:5]
    result: list[dict[str, Any]] = []
    for f in files:
        data = _read_json_dict(f)
        if data:
            result.append({"filename": f.name, "run_id": data.get("run_id", ""), "cycle": data.get("cycle", 0)})
    return result


# ── Swarm Cycle Report (19 health criteria) ─────────────────────────────────

def collect_report(artifacts_dir: Path) -> dict[str, Any]:
    """Read the comprehensive swarm cycle report with health criteria."""
    report = _read_json_dict(artifacts_dir / "autonomy" / "swarm_cycle_report.json")
    full_report = _read_json_dict(artifacts_dir / "autonomy" / "full_autonomy_report.json")
    return {
        "cycle_report": report,
        "full_report": full_report,
        "timestamp": _utc_now_iso(),
    }


# ── Cost Series ─────────────────────────────────────────────────────────────

def collect_cost_series(artifacts_dir: Path) -> dict[str, Any]:
    """Read cost time-series data for trend analysis."""
    summary = _read_json_dict(artifacts_dir / "autonomy" / "provider_costs" / "summary.json")
    return {
        "hourly_series": summary.get("cost_series_hourly_24h", []),
        "windows": summary.get("cost_windows_usd", {}),
        "by_provider": summary.get("provider_cost_30d", {}),
        "by_model": summary.get("model_cost_30d", {}),
        "data_freshness": summary.get("data_freshness", {}),
        "timestamp": _utc_now_iso(),
    }


# ── Privilege & Security ────────────────────────────────────────────────────

def collect_privileges(artifacts_dir: Path) -> dict[str, Any]:
    """Read current privilege state and breakglass events."""
    priv = _read_json_dict(artifacts_dir / "autonomy" / "session_autonomy_privileges.json")
    escalations = _read_ndjson(
        artifacts_dir / "autonomy" / "privilege_escalations.ndjson", tail=20)
    return {
        "current": priv,
        "recent_escalations": escalations,
        "timestamp": _utc_now_iso(),
    }


# ── Policy Compliance ───────────────────────────────────────────────────────

def collect_policies(artifacts_dir: Path) -> dict[str, Any]:
    """Read all policy compliance health files."""
    auto_dir = artifacts_dir / "autonomy"
    policies: dict[str, Any] = {}
    policy_files = [
        ("git_delivery", "git_delivery_policy_health.json"),
        ("git_hygiene", "git_hygiene_remediation.json"),
        ("api_interop", "api_interop_policy_health.json"),
        ("backend_upgrade", "backend_upgrade_policy_health.json"),
        ("pr_tier", "pr_tier_policy_health.json"),
        ("privilege", "privilege_policy_health.json"),
    ]
    for key, filename in policy_files:
        data = _read_json_dict(auto_dir / filename)
        if data:
            policies[key] = data
    return {
        "policies": policies,
        "timestamp": _utc_now_iso(),
    }


# ── Task Backlog ────────────────────────────────────────────────────────────

def collect_task_backlog(artifacts_dir: Path) -> dict[str, Any]:
    """Read the task backlog from config."""
    # Try config/tasks.json relative to artifacts' parent (repo root)
    repo_root = artifacts_dir.parent
    tasks_data = _read_json(repo_root / "config" / "tasks.json")
    tasks = tasks_data if isinstance(tasks_data, list) else []
    # Summarize
    by_owner: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        owner = str(t.get("owner", "unknown"))
        by_owner[owner] = by_owner.get(owner, 0) + 1
        prio = str(t.get("priority", "?"))
        by_priority[prio] = by_priority.get(prio, 0) + 1
    return {
        "tasks": tasks[:100],
        "total": len(tasks),
        "by_owner": by_owner,
        "by_priority": by_priority,
        "timestamp": _utc_now_iso(),
    }


# ── Response Metrics Stream ─────────────────────────────────────────────────

def collect_response_stream(artifacts_dir: Path, *, tail: int = 50) -> list[dict[str, Any]]:
    """Collect recent per-response metrics across all lanes for trend analysis."""
    lanes_dir = artifacts_dir / "autonomy" / "lanes"
    if not lanes_dir.is_dir():
        return []
    all_responses: list[dict[str, Any]] = []
    for lane_dir in lanes_dir.iterdir():
        if not lane_dir.is_dir():
            continue
        metrics_file = lane_dir / "response_metrics.ndjson"
        if not metrics_file.exists():
            continue
        entries = _read_ndjson(metrics_file, tail=30)
        for entry in entries:
            entry["lane_id"] = lane_dir.name
        all_responses.extend(entries)
    all_responses.sort(key=lambda e: str(e.get("timestamp", "")), reverse=True)
    return all_responses[:tail]


# ── Full Snapshot ────────────────────────────────────────────────────────────

def full_snapshot(artifacts_dir: Path, *, repo_dir: Path | None = None) -> dict[str, Any]:
    """Build the complete swarm state snapshot for the dashboard."""
    lanes = discover_lanes(artifacts_dir)
    health = collect_health(artifacts_dir)
    metrics = collect_metrics(artifacts_dir)
    events = collect_events(artifacts_dir, tail=80)
    checkpoints = collect_checkpoints(artifacts_dir)
    connectivity = collect_connectivity(artifacts_dir)

    git = collect_git_state(repo_dir) if repo_dir else {}

    # Derive summary counts
    owner_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    total_cost = 0.0
    total_tokens = 0
    total_responses = 0
    for lane in lanes:
        owner = lane.get("owner", "unknown")
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
        status = lane.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        total_cost += _safe_float(lane.get("metrics", {}).get("cost_usd_total", 0))
        total_tokens += _safe_int(lane.get("metrics", {}).get("tokens_total", 0))
        total_responses += _safe_int(lane.get("metrics", {}).get("responses_total", 0))

    # Alerts from health data
    alerts: list[dict[str, Any]] = []
    blocked_lanes = [l for l in lanes if l.get("status") == "blocked"]
    for bl in blocked_lanes:
        alerts.append({
            "severity": "high",
            "type": "blocked_lane",
            "message": f"Lane {bl['lane_id']} is blocked",
            "lane_id": bl["lane_id"],
        })

    # Stale heartbeats
    for lane in lanes:
        hb_ts = lane.get("heartbeat", {}).get("timestamp", "")
        if hb_ts:
            try:
                parsed = datetime.fromisoformat(hb_ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - parsed).total_seconds()
                lane["heartbeat"]["age_sec"] = int(age)
                if age > 300 and lane.get("status") != "done":
                    alerts.append({
                        "severity": "medium",
                        "type": "stale_heartbeat",
                        "message": f"Lane {lane['lane_id']} heartbeat stale ({int(age)}s)",
                        "lane_id": lane["lane_id"],
                    })
            except Exception:
                lane["heartbeat"]["age_sec"] = -1
        else:
            lane["heartbeat"]["age_sec"] = -1

    # High retry/deadlock lanes
    for lane in lanes:
        for task in lane.get("tasks", []):
            if _safe_int(task.get("deadlock_recoveries", 0)) >= 5:
                alerts.append({
                    "severity": "medium",
                    "type": "deadlock_storm",
                    "message": f"Task {task['task_id']}: {task['deadlock_recoveries']} deadlock recoveries",
                    "lane_id": lane["lane_id"],
                })

    return {
        "timestamp": _utc_now_iso(),
        "summary": {
            "lanes_total": len(lanes),
            "owner_counts": owner_counts,
            "status_counts": status_counts,
            "total_cost_usd": round(total_cost, 4),
            "total_tokens": total_tokens,
            "total_responses": total_responses,
            "alerts_count": len(alerts),
        },
        "lanes": lanes,
        "health": health,
        "metrics": metrics,
        "events": events,
        "alerts": alerts,
        "git": git,
        "connectivity": connectivity,
        "checkpoints": checkpoints,
    }


# ── HTTP Handler ─────────────────────────────────────────────────────────────

_FRONTEND_MAP = {
    "nexus": "dashboard_v2_frontend.html",
    "meridian": "dashboard_v2_meridian.html",
    "prism": "dashboard_v2_prism.html",
    "signal": "dashboard_v2_signal.html",
}


def _html_path(variant: str = "nexus") -> Path:
    """Path to a dashboard HTML file (co-located with this module)."""
    filename = _FRONTEND_MAP.get(variant, _FRONTEND_MAP["nexus"])
    return Path(__file__).parent / filename


def make_v2_handler(
    artifacts_dir: Path,
    *,
    repo_dir: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler that serves the v2 dashboard."""
    resolved_artifacts = artifacts_dir.resolve()
    resolved_repo = repo_dir.resolve() if repo_dir else None

    class V2Handler(BaseHTTPRequestHandler):
        _artifacts = resolved_artifacts
        _repo = resolved_repo

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, text: str, status: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, status: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_frontend(self, variant: str) -> None:
            html_file = _html_path(variant)
            if html_file.exists():
                self._send_html(html_file.read_text(encoding="utf-8"))
            else:
                self._send_html(
                    "<html><body><h1>Dashboard frontend not found</h1>"
                    f"<p>Expected at: {html_file}</p></body></html>",
                    status=500,
                )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            path = parsed.path
            qs = urllib_parse.parse_qs(parsed.query)

            # ── Frontend routes ──
            if path in ("/", "/v2", "/nexus"):
                self._serve_frontend("nexus")
                return
            if path == "/meridian":
                self._serve_frontend("meridian")
                return
            if path == "/prism":
                self._serve_frontend("prism")
                return
            if path == "/signal":
                self._serve_frontend("signal")
                return

            # ── Original API routes ──
            if path == "/api/v2/snapshot":
                data = full_snapshot(self._artifacts, repo_dir=self._repo)
                self._send_json(data)
                return

            if path == "/api/v2/lanes":
                data = discover_lanes(self._artifacts)
                self._send_json(data)
                return

            if path.startswith("/api/v2/lanes/"):
                lane_id = path[len("/api/v2/lanes/"):]
                lanes = discover_lanes(self._artifacts)
                match = next((l for l in lanes if l["lane_id"] == lane_id), None)
                if match:
                    self._send_json(match)
                else:
                    self._send_json({"error": "lane not found"}, status=404)
                return

            if path == "/api/v2/events":
                tail = _safe_int(qs.get("tail", [80])[0], 80)
                data = collect_events(self._artifacts, tail=min(tail, 500))
                self._send_json(data)
                return

            if path == "/api/v2/health":
                data = collect_health(self._artifacts)
                self._send_json(data)
                return

            if path == "/api/v2/metrics":
                data = collect_metrics(self._artifacts)
                self._send_json(data)
                return

            if path == "/api/v2/git":
                if self._repo:
                    data = collect_git_state(self._repo)
                    self._send_json(data)
                else:
                    self._send_json({"error": "repo_dir not configured"}, status=400)
                return

            # ── New enriched API routes ──
            if path == "/api/v2/report":
                data = collect_report(self._artifacts)
                self._send_json(data)
                return

            if path == "/api/v2/cost-series":
                data = collect_cost_series(self._artifacts)
                self._send_json(data)
                return

            if path == "/api/v2/privileges":
                data = collect_privileges(self._artifacts)
                self._send_json(data)
                return

            if path == "/api/v2/policies":
                data = collect_policies(self._artifacts)
                self._send_json(data)
                return

            if path == "/api/v2/task-backlog":
                data = collect_task_backlog(self._artifacts)
                self._send_json(data)
                return

            if path == "/api/v2/response-stream":
                tail = _safe_int(qs.get("tail", [50])[0], 50)
                data = collect_response_stream(self._artifacts, tail=min(tail, 200))
                self._send_json(data)
                return

            # ── Action API (GET) ──
            if path == "/api/v2/actions":
                self._send_json(dashboard_actions.get_action_catalog())
                return

            if path == "/api/v2/actions/audit":
                audit_path = self._artifacts / "autonomy" / "dashboard_actions_audit.ndjson"
                entries = _read_ndjson(audit_path, tail=100)
                self._send_json(entries)
                return

            self._send_text("Not found\n", status=404)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            path = parsed.path

            if not path.startswith("/api/v2/actions/"):
                self._send_json({"error": "not found"}, status=404)
                return

            action_id = path[len("/api/v2/actions/"):]

            # Parse request body
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 65536:
                    self._send_json({"error": "request body too large"}, status=400)
                    return
                raw = self.rfile.read(content_length) if content_length > 0 else b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    self._send_json({"error": "request body must be a JSON object"}, status=400)
                    return
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": f"invalid request body: {exc}"}, status=400)
                return

            result = dashboard_actions.dispatch_action(
                action_id=action_id,
                params=body.get("params", {}),
                confirm_token=body.get("confirm_token", ""),
                dry_run=body.get("dry_run", False),
                artifacts_dir=self._artifacts,
                repo_dir=self._repo,
            )

            if not result.ok and "confirmation" in result.message.lower():
                status = 409
            elif not result.ok and "Unknown action" in result.message:
                status = 404
            elif not result.ok and "Rate limit" in result.message:
                status = 429
            elif not result.ok:
                status = 400
            else:
                status = 200
            self._send_json(result.to_dict(), status=status)

    return V2Handler


def run_v2_server(
    *,
    artifacts_dir: Path,
    repo_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8788,
) -> None:
    """Start the v2 dashboard HTTP server."""
    handler = make_v2_handler(artifacts_dir, repo_dir=repo_dir)
    server = ThreadingHTTPServer((host, int(port)), handler)
    url = f"http://{host}:{server.server_port}/"
    print(f"NEXUS dashboard serving at {url}")
    print(f"  artifacts: {artifacts_dir.resolve()}")
    if repo_dir:
        print(f"  repo: {repo_dir.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
