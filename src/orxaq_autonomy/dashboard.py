"""Local-first dashboard for autonomy run reports and evidence artifacts."""

from __future__ import annotations

import html
import json
import mimetypes
import os
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import parse as urllib_parse

TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".txt",
    ".log",
    ".yaml",
    ".yml",
    ".html",
    ".csv",
}




def tail_log_file(path: Path, max_bytes: int = 65536) -> list[str]:
    """Read the tail of a log file efficiently, bounded by *max_bytes*.

    Uses an os.SEEK_END approach to avoid reading the entire file when it
    is large.  Returns a list of decoded text lines (most-recent last).

    Edge cases:
    - Missing file -> empty list
    - Empty file -> empty list
    - File smaller than *max_bytes* -> full content returned
    - File larger than *max_bytes* -> last *max_bytes* bytes read, first
      (possibly partial) line discarded for correctness
    """
    resolved = Path(path)
    if not resolved.exists() or not resolved.is_file():
        return []

    try:
        file_size = resolved.stat().st_size
    except OSError:
        return []

    if file_size == 0:
        return []

    try:
        with open(resolved, "rb") as fh:
            if file_size <= max_bytes:
                raw = fh.read()
            else:
                fh.seek(-max_bytes, os.SEEK_END)
                raw = fh.read(max_bytes)
    except OSError:
        return []

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    # When we seeked into the middle of the file the first "line" is likely
    # a partial fragment -- drop it for correctness.
    if file_size > max_bytes and lines:
        lines = lines[1:]

    return lines


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel_paths(paths: list[Path], root: Path) -> list[str]:
    out: list[str] = []
    for path in paths:
        try:
            out.append(path.resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return sorted(out)


def _safe_glob(root: Path, pattern: str) -> list[Path]:
    """Glob with fallback to empty list on permission or filesystem errors."""
    try:
        return list(root.glob(pattern))
    except (OSError, ValueError):
        return []


def _detect_stale_health(root: Path, stale_threshold_sec: int = 600) -> dict[str, Any]:
    """Return stale-data annotation for the most recent health.json artifact.

    If no health.json exists or it was last modified more than *stale_threshold_sec*
    ago, the ``stale`` flag is set to ``True`` with explanatory detail.
    """
    candidates = _safe_glob(root, "**/health.json")
    if not candidates:
        return {"stale": True, "reason": "no_health_artifact", "age_sec": -1}
    # Pick most recently modified
    newest = max(candidates, key=lambda p: p.stat().st_mtime, default=None)
    if newest is None:
        return {"stale": True, "reason": "no_health_artifact", "age_sec": -1}
    try:
        age_sec = int(datetime.now(timezone.utc).timestamp() - newest.stat().st_mtime)
    except OSError:
        return {"stale": True, "reason": "stat_failed", "age_sec": -1}
    return {
        "stale": age_sec > stale_threshold_sec,
        "reason": "age_exceeded" if age_sec > stale_threshold_sec else "ok",
        "age_sec": age_sec,
    }


def collect_dashboard_index(artifacts_root: Path) -> dict[str, Any]:
    root = artifacts_root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []

    health_json = _rel_paths(_safe_glob(root, "**/health.json"), root)
    health_md = _rel_paths(_safe_glob(root, "**/health.md"), root)
    run_reports = _rel_paths(_safe_glob(root, "**/W*_run.json"), root)
    run_summaries = _rel_paths(_safe_glob(root, "**/W*_summary.md"), root)
    pr_review_snapshots = _rel_paths(_safe_glob(root, "**/pr_review_snapshot.json"), root)

    try:
        evidence_dirs = _rel_paths(
            [path for path in root.glob("rpa_evidence/*/*") if path.is_dir()], root
        )
    except (OSError, ValueError) as exc:
        evidence_dirs = []
        errors.append(f"evidence_dirs: {exc}")

    try:
        evidence_files = _rel_paths(
            [path for path in root.glob("rpa_evidence/**/*") if path.is_file()][:200],
            root,
        )
    except (OSError, ValueError) as exc:
        evidence_files = []
        errors.append(f"evidence_files: {exc}")

    staleness = _detect_stale_health(root)

    payload: dict[str, Any] = {
        "generated_at_utc": _utc_now_iso(),
        "artifacts_root": str(root),
        "health_json": health_json,
        "health_md": health_md,
        "run_reports": run_reports,
        "run_summaries": run_summaries,
        "pr_review_snapshots": pr_review_snapshots,
        "evidence_dirs": evidence_dirs,
        "evidence_files": evidence_files,
        "staleness": staleness,
    }
    if errors:
        payload["errors"] = errors
    return payload


def resolve_artifact_path(artifacts_root: Path, raw_relative_path: str) -> Path | None:
    root = artifacts_root.resolve()
    candidate = (root / Path(raw_relative_path)).resolve()
    if candidate == root:
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _render_file_links(title: str, rows: list[str]) -> str:
    if not rows:
        return (
            f"<section aria-label='{html.escape(title)}'>"
            f"<h2>{html.escape(title)}</h2>"
            f"<p class='empty'>None found.</p></section>"
        )

    count = len(rows)
    items = []
    for row in rows:
        quoted = urllib_parse.quote(row, safe="/")
        label = html.escape(row)
        items.append(f"<li><a href='/file/{quoted}'>{label}</a></li>")
    return (
        f"<section aria-label='{html.escape(title)}'>"
        f"<h2>{html.escape(title)} <span class='badge' aria-label='{count} items'>{count}</span></h2>"
        f"<ul role='list'>{''.join(items)}</ul></section>"
    )


def _render_stale_banner(staleness: dict[str, Any]) -> str:
    """Render an accessible stale-data warning banner if health data is stale."""
    if not staleness.get("stale", False):
        return ""
    reason = html.escape(str(staleness.get("reason", "unknown")))
    age = staleness.get("age_sec", -1)
    age_text = f"{age}s ago" if age >= 0 else "unknown age"
    return (
        "<div role='alert' aria-live='polite' class='stale-banner'>"
        f"<strong>Stale data</strong>: health artifact is outdated ({reason}, {html.escape(age_text)}). "
        "Results below may not reflect current state."
        "</div>"
    )


def _render_error_banner(errors: list[str]) -> str:
    """Render a visible error banner for partial data failures."""
    if not errors:
        return ""
    items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    return (
        "<div role='alert' aria-live='assertive' class='error-banner'>"
        "<strong>Partial data</strong>: some artifact scans failed."
        f"<ul>{items}</ul></div>"
    )


def aggregate_distributed_todos(
    state_payloads: list[dict[str, Any]],
    *,
    stale_threshold_sec: int = 3600,
) -> dict[str, Any]:
    """Aggregate todo/task state across multiple distributed sources.

    Each *state_payloads* entry is a task-state dict (task_id -> task_data).
    Returns a summary with consistent covered/uncovered/total metrics,
    staleness annotations, and fallback indicators.

    Parameters
    ----------
    state_payloads:
        List of dicts mapping task_id -> task_data.
    stale_threshold_sec:
        If the newest ``last_update`` across all tasks is older than this
        many seconds from now, the result is marked stale.  Defaults to 3600.
    """
    # Detect fallback: no valid sources at all
    valid_sources = [p for p in state_payloads if isinstance(p, dict)]
    fallback = len(valid_sources) == 0

    merged: dict[str, dict[str, Any]] = {}
    for payload in valid_sources:
        for task_id, task_data in payload.items():
            if not isinstance(task_data, dict):
                # Non-dict entries are skipped for aggregation
                continue
            existing = merged.get(str(task_id))
            if existing is None:
                merged[str(task_id)] = dict(task_data)
            else:
                # Later update wins if it has a newer last_update
                new_update = str(task_data.get("last_update", "")).strip()
                old_update = str(existing.get("last_update", "")).strip()
                if new_update > old_update:
                    merged[str(task_id)] = dict(task_data)

    # Also treat empty merged results as fallback
    if not merged:
        fallback = True

    status_counts: dict[str, int] = {}
    for task_data in merged.values():
        status = str(task_data.get("status", "unknown")).strip().lower()
        status_counts[status] = status_counts.get(status, 0) + 1

    covered = status_counts.get("done", 0)
    uncovered = sum(v for k, v in status_counts.items() if k != "done")
    total = covered + uncovered

    # Compute staleness from the newest last_update across all tasks
    fetched_at = datetime.now(timezone.utc).isoformat()
    newest_update: str | None = None
    for task_data in merged.values():
        ts = str(task_data.get("last_update", "")).strip()
        if ts and (newest_update is None or ts > newest_update):
            newest_update = ts

    stale = False
    stale_reason = "ok"
    if not merged:
        stale = True
        stale_reason = "no_tasks"
    elif newest_update:
        try:
            newest_dt = datetime.fromisoformat(newest_update)
            if newest_dt.tzinfo is None:
                newest_dt = newest_dt.replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - newest_dt).total_seconds()
            if age_sec > stale_threshold_sec:
                stale = True
                stale_reason = "age_exceeded"
        except (ValueError, TypeError):
            stale = True
            stale_reason = "unparseable_timestamp"
    else:
        stale = True
        stale_reason = "no_timestamps"

    return {
        "tasks": merged,
        "status_counts": status_counts,
        "covered": covered,
        "uncovered": uncovered,
        "total": total,
        "stale": stale,
        "stale_reason": stale_reason,
        "newest_update": newest_update,
        "fetched_at": fetched_at,
        "fallback": fallback,
    }


def render_todo_activity_widget(
    state: dict[str, Any],
    *,
    max_items: int = 10,
) -> str:
    """Render an accessible todo activity widget for the dashboard.

    The widget shows recent task activity sorted by last_update, with
    clear status indicators and screen-reader-friendly markup.
    """
    if not isinstance(state, dict) or not state:
        return (
            "<section aria-label='Task Activity'>"
            "<h2>Task Activity</h2>"
            "<p class='empty'>No tasks found.</p></section>"
        )

    _STATUS_LABELS: dict[str, tuple[str, str]] = {
        "done": ("Done", "status-done"),
        "in_progress": ("In Progress", "status-progress"),
        "pending": ("Pending", "status-pending"),
        "blocked": ("Blocked", "status-blocked"),
    }

    # Sort by last_update descending (most recent first)
    items: list[tuple[str, dict[str, Any]]] = []
    for task_id, task_data in state.items():
        if not isinstance(task_data, dict):
            continue
        items.append((str(task_id), task_data))
    items.sort(
        key=lambda x: str(x[1].get("last_update", "")),
        reverse=True,
    )
    items = items[:max_items]

    rows = []
    for task_id, task_data in items:
        status_raw = str(task_data.get("status", "unknown")).strip().lower()
        label, css_class = _STATUS_LABELS.get(status_raw, (status_raw.title(), "status-unknown"))
        last_update = html.escape(str(task_data.get("last_update", "")).strip() or "n/a")
        summary = html.escape(str(task_data.get("last_summary", "")).strip()[:80] or "")
        task_label = html.escape(task_id)
        rows.append(
            f"<tr>"
            f"<td><code>{task_label}</code></td>"
            f"<td><span class='status-badge {css_class}' role='status' "
            f"aria-label='Status: {html.escape(label)}'>{html.escape(label)}</span></td>"
            f"<td><time datetime='{last_update}'>{last_update}</time></td>"
            f"<td>{summary}</td>"
            f"</tr>"
        )

    # Compute summary counts (only dict entries are valid tasks)
    counts: dict[str, int] = {}
    for _, td in state.items():
        if not isinstance(td, dict):
            continue
        s = str(td.get("status", "unknown")).strip().lower()
        counts[s] = counts.get(s, 0) + 1

    covered = counts.get("done", 0)
    total = sum(counts.values())
    uncovered = total - covered

    summary_text = (
        f"<p class='todo-summary' aria-live='polite'>"
        f"<strong>{covered}</strong> done, "
        f"<strong>{uncovered}</strong> remaining, "
        f"<strong>{total}</strong> total"
        f"</p>"
    )

    return (
        "<section aria-label='Task Activity'>"
        "<h2>Task Activity</h2>"
        f"{summary_text}"
        "<table class='activity-table' role='table' aria-label='Recent task activity'>"
        "<thead><tr>"
        "<th scope='col'>Task</th>"
        "<th scope='col'>Status</th>"
        "<th scope='col'>Updated</th>"
        "<th scope='col'>Summary</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></section>"
    )


def render_lane_status_section(lane_data: list[dict[str, Any]]) -> str:
    """Render lane-level status with owner identity in HTML.

    Each entry in *lane_data* should have at minimum ``lane`` and ``status`` keys.
    Optional keys: ``owner``, ``updated_at``, ``detail``.
    Returns a self-contained HTML ``<section>`` element.
    """
    if not lane_data:
        return (
            "<section aria-label='Lane Status'>"
            "<h2>Lane Status</h2>"
            "<p class='empty'>No lane data available.</p></section>"
        )

    rows: list[str] = []
    for entry in lane_data:
        if not isinstance(entry, dict):
            continue
        lane = html.escape(str(entry.get("lane", "unknown")))
        status = html.escape(str(entry.get("status", "unknown")))
        owner = html.escape(str(entry.get("owner", "unassigned")))
        updated = html.escape(str(entry.get("updated_at", "")))
        detail = html.escape(str(entry.get("detail", ""))[:120])
        rows.append(
            f"<tr>"
            f"<td><code>{lane}</code></td>"
            f"<td><span class='status-badge' role='status' "
            f"aria-label='Status: {status}'>{status}</span></td>"
            f"<td>{owner}</td>"
            f"<td><time datetime='{updated}'>{updated or 'n/a'}</time></td>"
            f"<td>{detail}</td>"
            f"</tr>"
        )

    count = len(rows)
    return (
        "<section aria-label='Lane Status'>"
        f"<h2>Lane Status <span class='badge' aria-label='{count} lanes'>{count}</span></h2>"
        "<table class='activity-table' role='table' aria-label='Lane status overview'>"
        "<thead><tr>"
        "<th scope='col'>Lane</th>"
        "<th scope='col'>Status</th>"
        "<th scope='col'>Owner</th>"
        "<th scope='col'>Updated</th>"
        "<th scope='col'>Detail</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></section>"
    )


def render_conversation_events_section(
    events: list[dict[str, Any]],
    *,
    max_events: int = 20,
) -> str:
    """Render recent conversation/collaboration events in HTML.

    Each entry in *events* should have ``timestamp`` and ``message`` keys.
    Optional keys: ``actor``, ``event_type``.
    Returns a self-contained HTML ``<section>`` element.
    """
    if not events:
        return (
            "<section aria-label='Conversation Events'>"
            "<h2>Conversation Events</h2>"
            "<p class='empty'>No conversation events recorded.</p></section>"
        )

    # Sort by timestamp descending, limit
    sorted_events = sorted(
        [e for e in events if isinstance(e, dict)],
        key=lambda e: str(e.get("timestamp", "")),
        reverse=True,
    )[:max_events]

    rows: list[str] = []
    for event in sorted_events:
        ts = html.escape(str(event.get("timestamp", "")))
        actor = html.escape(str(event.get("actor", "system")))
        event_type = html.escape(str(event.get("event_type", "info")))
        message = html.escape(str(event.get("message", ""))[:200])
        rows.append(
            f"<tr>"
            f"<td><time datetime='{ts}'>{ts or 'n/a'}</time></td>"
            f"<td>{actor}</td>"
            f"<td><span class='event-type'>{event_type}</span></td>"
            f"<td>{message}</td>"
            f"</tr>"
        )

    count = len(sorted_events)
    return (
        "<section aria-label='Conversation Events'>"
        f"<h2>Conversation Events <span class='badge' aria-label='{count} events'>{count}</span></h2>"
        "<table class='activity-table' role='table' aria-label='Recent conversation events'>"
        "<thead><tr>"
        "<th scope='col'>Time</th>"
        "<th scope='col'>Actor</th>"
        "<th scope='col'>Type</th>"
        "<th scope='col'>Message</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></section>"
    )


def render_dashboard_html(index_payload: dict[str, Any]) -> str:
    sections = [
        _render_file_links("Health JSON", list(index_payload.get("health_json", []))),
        _render_file_links("Health Markdown", list(index_payload.get("health_md", []))),
        _render_file_links("Run Reports", list(index_payload.get("run_reports", []))),
        _render_file_links("Run Summaries", list(index_payload.get("run_summaries", []))),
        _render_file_links("PR Review Snapshots", list(index_payload.get("pr_review_snapshots", []))),
        _render_file_links("Evidence Directories", list(index_payload.get("evidence_dirs", []))),
        _render_file_links("Evidence Files", list(index_payload.get("evidence_files", []))),
    ]
    generated = html.escape(str(index_payload.get("generated_at_utc", "")))
    artifacts_root = html.escape(str(index_payload.get("artifacts_root", "")))

    staleness = index_payload.get("staleness", {})
    staleness_dict = staleness if isinstance(staleness, dict) else {}
    stale_banner = _render_stale_banner(staleness_dict)

    errors = index_payload.get("errors", [])
    errors_list = errors if isinstance(errors, list) else []
    error_banner = _render_error_banner(errors_list)

    # Render todo activity widget from task_state if present
    task_state = index_payload.get("task_state", {})
    task_state_dict = task_state if isinstance(task_state, dict) else {}
    todo_activity = render_todo_activity_widget(task_state_dict)

    # Render lane status section if payload contains lane_status
    lane_status_html = ""
    if "lane_status" in index_payload:
        try:
            lane_data = index_payload["lane_status"]
            lane_status_html = render_lane_status_section(
                lane_data if isinstance(lane_data, list) else []
            )
        except Exception:
            lane_status_html = (
                "<section aria-label='Lane Status'>"
                "<h2>Lane Status</h2>"
                "<p class='empty'>Error rendering lane status.</p></section>"
            )

    # Render conversation events section if payload contains conversation_events
    conversation_events_html = ""
    if "conversation_events" in index_payload:
        try:
            events = index_payload["conversation_events"]
            conversation_events_html = render_conversation_events_section(
                events if isinstance(events, list) else []
            )
        except Exception:
            conversation_events_html = (
                "<section aria-label='Conversation Events'>"
                "<h2>Conversation Events</h2>"
                "<p class='empty'>Error rendering conversation events.</p></section>"
            )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Orxaq Autonomy Dashboard</title>
  <style>
    :root {{
      --bg: #f3f2ee;
      --panel: #fffef8;
      --ink: #13232f;
      --muted: #5c6a72;
      --accent: #bf4f24;
      --line: #ddd6c8;
      --warn-bg: #fff3cd;
      --warn-border: #e0a800;
      --error-bg: #f8d7da;
      --error-border: #c62828;
    }}
    * {{ box-sizing: border-box; }}
    .skip-link {{
      position: absolute;
      top: -40px;
      left: 0;
      background: var(--accent);
      color: #fff;
      padding: 8px;
      z-index: 100;
    }}
    .skip-link:focus {{
      top: 0;
    }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at 10% 0%, #ffe8d6 0%, var(--bg) 42%);
    }}
    main {{
      max-width: 980px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      gap: 16px;
    }}
    header {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-left: 6px solid var(--accent);
      padding: 18px;
    }}
    h1 {{ margin: 0 0 8px 0; font-size: 1.5rem; }}
    p {{ margin: 6px 0; color: var(--muted); }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 14px 16px;
    }}
    h2 {{ margin: 0 0 10px 0; font-size: 1rem; }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ margin: 4px 0; line-height: 1.3; }}
    a {{ color: #0e4a72; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .empty {{ color: var(--muted); margin: 0; }}
    .stale-banner {{
      background: var(--warn-bg);
      border: 1px solid var(--warn-border);
      border-left: 6px solid var(--warn-border);
      padding: 12px 16px;
      line-height: 1.5;
    }}
    .error-banner {{
      background: var(--error-bg);
      border: 1px solid var(--error-border);
      border-left: 6px solid var(--error-border);
      padding: 12px 16px;
      line-height: 1.5;
    }}
    .error-banner ul {{ margin-top: 6px; }}
    .badge {{
      display: inline-block;
      background: var(--accent);
      color: #fff;
      font-size: 0.75rem;
      padding: 1px 7px;
      border-radius: 10px;
      vertical-align: middle;
      margin-left: 4px;
    }}
    .todo-summary {{
      font-size: 0.95rem;
      color: var(--ink);
      margin: 0 0 12px 0;
    }}
    .activity-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.9rem;
    }}
    .activity-table th, .activity-table td {{
      text-align: left;
      padding: 6px 10px;
      border-bottom: 1px solid var(--line);
    }}
    .activity-table th {{
      color: var(--muted);
      font-weight: 600;
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}
    .status-badge {{
      display: inline-block;
      font-size: 0.8rem;
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 600;
    }}
    .status-done {{ background: #d4edda; color: #155724; }}
    .status-progress {{ background: #cce5ff; color: #004085; }}
    .status-pending {{ background: #e2e3e5; color: #383d41; }}
    .status-blocked {{ background: #f8d7da; color: #721c24; }}
    .status-unknown {{ background: #e2e3e5; color: #6c757d; }}
    @media (max-width: 700px) {{
      main {{ padding: 14px; }}
      .activity-table {{ font-size: 0.8rem; }}
    }}
  </style>
</head>
<body>
  <a href="#main-content" class="skip-link">Skip to main content</a>
  <main id="main-content" role="main" aria-label="Dashboard content">
    <header role="banner">
      <h1>Orxaq Autonomy Dashboard</h1>
      <p>Generated: <time datetime="{generated}">{generated}</time></p>
      <p>Artifacts root: <code>{artifacts_root}</code></p>
      <p>API index: <a href="/api/index">/api/index</a></p>
    </header>
    {stale_banner}
    {error_banner}
    {todo_activity}
    {lane_status_html}
    {conversation_events_html}
    {''.join(sections)}
  </main>
</body>
</html>
"""


def make_dashboard_handler(artifacts_root: Path) -> type[BaseHTTPRequestHandler]:
    root = artifacts_root.resolve()

    class DashboardHandler(BaseHTTPRequestHandler):
        _artifacts_root = root

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
            self._send_bytes(status, text.encode("utf-8"), content_type)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            path = parsed.path

            if path == "/":
                payload = collect_dashboard_index(self._artifacts_root)
                self._send_text(HTTPStatus.OK, render_dashboard_html(payload), "text/html; charset=utf-8")
                return

            if path == "/api/index":
                payload = collect_dashboard_index(self._artifacts_root)
                self._send_text(
                    HTTPStatus.OK,
                    json.dumps(payload, sort_keys=True, indent=2) + "\n",
                    "application/json; charset=utf-8",
                )
                return

            if path == "/api/todos":
                # Parse optional stale_threshold_sec from query string
                qs = urllib_parse.parse_qs(parsed.query)
                try:
                    threshold = int(qs.get("stale_threshold_sec", ["3600"])[0])
                except (ValueError, IndexError):
                    threshold = 3600
                # Collect task state from distributed sources (currently
                # the dashboard index's task_state; extensible to remote).
                index_payload = collect_dashboard_index(self._artifacts_root)
                task_state = index_payload.get("task_state", {})
                sources: list[dict[str, Any]] = [task_state] if isinstance(task_state, dict) and task_state else []
                result = aggregate_distributed_todos(sources, stale_threshold_sec=threshold)
                self._send_text(
                    HTTPStatus.OK,
                    json.dumps(result, sort_keys=True, indent=2) + "\n",
                    "application/json; charset=utf-8",
                )
                return

            if path.startswith("/file/"):
                rel = urllib_parse.unquote(path[len("/file/") :])
                target = resolve_artifact_path(self._artifacts_root, rel)
                if target is None:
                    self._send_text(HTTPStatus.FORBIDDEN, "Forbidden path\n")
                    return
                if not target.exists() or not target.is_file():
                    self._send_text(HTTPStatus.NOT_FOUND, "Artifact not found\n")
                    return

                suffix = target.suffix.lower()
                if suffix in TEXT_SUFFIXES:
                    content = target.read_text(encoding="utf-8", errors="replace")
                    body = (
                        "<!doctype html><html><head><meta charset='utf-8' />"
                        "<meta name='viewport' content='width=device-width, initial-scale=1' />"
                        "<title>Artifact Viewer</title>"
                        "<style>body{font-family:ui-monospace,monospace;margin:0;padding:16px;background:#f8f8f8;}"
                        "pre{white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #ddd;"
                        "padding:12px;border-radius:6px;}</style></head><body>"
                        f"<h1>{html.escape(rel)}</h1><pre>{html.escape(content)}</pre></body></html>"
                    )
                    self._send_text(HTTPStatus.OK, body, "text/html; charset=utf-8")
                    return

                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send_bytes(HTTPStatus.OK, target.read_bytes(), content_type)
                return

            self._send_text(HTTPStatus.NOT_FOUND, "Not found\n")

    return DashboardHandler


def run_dashboard_server(
    *,
    artifacts_root: Path,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    resolved_root = artifacts_root.resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    handler = make_dashboard_handler(resolved_root)
    server = ThreadingHTTPServer((host, int(port)), handler)
    url = f"http://{host}:{server.server_port}/"
    print(f"dashboard serving {resolved_root} at {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
