"""Local GUI dashboard for autonomy monitoring."""

from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .manager import ManagerConfig, health_snapshot, monitor_snapshot, status_snapshot, tail_logs


def _dashboard_html(refresh_sec: int) -> str:
    refresh_ms = max(1000, int(refresh_sec) * 1000)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Orxaq Autonomy Monitor</title>
  <style>
    :root {{
      --bg-0: #f5f7fb;
      --bg-1: #e6eefc;
      --bg-2: #d6f2ec;
      --panel: rgba(255, 255, 255, 0.86);
      --ink: #0f172a;
      --muted: #475569;
      --ok: #0f9f5f;
      --warn: #c77d00;
      --bad: #cc2f2f;
      --info: #1f6feb;
      --ring: rgba(31, 111, 235, 0.18);
      --border: rgba(15, 23, 42, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 600px at -20% -20%, var(--bg-2), transparent 60%),
        radial-gradient(1000px 540px at 120% -10%, var(--bg-1), transparent 58%),
        linear-gradient(165deg, var(--bg-0), #fefefe);
      padding: 22px;
    }}
    .wrap {{
      width: min(1240px, 100%);
      margin: 0 auto;
      display: grid;
      gap: 16px;
      animation: enter .45s ease-out;
    }}
    @keyframes enter {{
      from {{ opacity: 0; transform: translateY(8px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .top {{
      background: var(--panel);
      border: 1px solid var(--border);
      backdrop-filter: blur(8px);
      border-radius: 16px;
      padding: 18px 18px 16px;
      box-shadow: 0 10px 34px rgba(15, 23, 42, 0.08);
      display: grid;
      gap: 12px;
    }}
    .title {{
      margin: 0;
      font-size: clamp(1.2rem, 2vw, 1.65rem);
      letter-spacing: .01em;
      font-weight: 750;
    }}
    .meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 11px;
      font-size: .84rem;
      border: 1px solid var(--border);
      background: #fff;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    button {{
      appearance: none;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--ink);
      border-radius: 10px;
      font-size: .86rem;
      font-weight: 620;
      padding: 7px 11px;
      cursor: pointer;
    }}
    button:hover {{ border-color: var(--info); box-shadow: 0 0 0 4px var(--ring); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 14px;
    }}
    .card {{
      grid-column: span 12;
      background: var(--panel);
      border: 1px solid var(--border);
      backdrop-filter: blur(8px);
      border-radius: 14px;
      box-shadow: 0 10px 30px rgba(15, 23, 42, 0.07);
      padding: 14px;
      display: grid;
      gap: 9px;
    }}
    .card h2 {{
      margin: 0;
      font-size: .98rem;
      letter-spacing: .01em;
      font-weight: 700;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0,1fr));
      gap: 8px;
    }}
    .stat {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 9px;
      background: #ffffff;
    }}
    .k {{ font-size: .78rem; color: var(--muted); }}
    .v {{ font-size: 1.1rem; font-weight: 740; }}
    .logline {{
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #0b1324;
      color: #ddf0ff;
      padding: 10px;
      font-family: "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
      font-size: .82rem;
      white-space: pre-wrap;
      word-break: break-word;
      min-height: 56px;
    }}
    .repo {{
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #fff;
      padding: 9px;
      display: grid;
      gap: 4px;
    }}
    .repo .name {{ font-weight: 680; font-size: .84rem; }}
    .repo .line {{ color: var(--muted); font-size: .82rem; }}
    .bar {{
      height: 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      overflow: hidden;
      background: #fff;
      display: grid;
      grid-template-columns: var(--done,0%) var(--in_progress,0%) var(--pending,0%) var(--blocked,0%);
    }}
    .seg-done {{ background: var(--ok); }}
    .seg-progress {{ background: var(--info); }}
    .seg-pending {{ background: #94a3b8; }}
    .seg-blocked {{ background: var(--bad); }}
    .warn {{ color: var(--warn); font-weight: 700; }}
    .bad {{ color: var(--bad); font-weight: 700; }}
    .ok {{ color: var(--ok); font-weight: 700; }}
    .mono {{ font-family: "SFMono-Regular", Menlo, Monaco, Consolas, monospace; font-size: .81rem; }}
    @media (min-width: 940px) {{
      .card.span-6 {{ grid-column: span 6; }}
      .card.span-4 {{ grid-column: span 4; }}
      .card.span-8 {{ grid-column: span 8; }}
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="top">
      <h1 class="title">Orxaq Autonomy Monitor</h1>
      <div class="meta" id="meta"></div>
      <div class="controls">
        <button id="refresh">Refresh now</button>
        <button id="pause">Pause</button>
        <span id="interval" class="pill">refresh: {refresh_sec}s</span>
        <span id="updated" class="pill">updated: --</span>
      </div>
    </section>

    <section class="grid">
      <article class="card span-8">
        <h2>Task Progress</h2>
        <div id="taskBar" class="bar" style="--done:0%;--in_progress:0%;--pending:0%;--blocked:0%;">
          <div class="seg-done"></div><div class="seg-progress"></div><div class="seg-pending"></div><div class="seg-blocked"></div>
        </div>
        <div class="stats">
          <div class="stat"><div class="k">Done</div><div id="done" class="v">0</div></div>
          <div class="stat"><div class="k">In progress</div><div id="in_progress" class="v">0</div></div>
          <div class="stat"><div class="k">Pending</div><div id="pending" class="v">0</div></div>
          <div class="stat"><div class="k">Blocked</div><div id="blocked" class="v">0</div></div>
          <div class="stat"><div class="k">Unknown</div><div id="unknown" class="v">0</div></div>
        </div>
        <div id="activeTasks" class="mono">active_tasks: none</div>
      </article>

      <article class="card span-4">
        <h2>Runtime</h2>
        <div id="runtimeState" class="mono">loading...</div>
        <div id="heartbeatState" class="mono"></div>
      </article>

      <article class="card span-6">
        <h2>Repository: Implementation</h2>
        <div id="repoImpl" class="repo"></div>
      </article>

      <article class="card span-6">
        <h2>Repository: Tests</h2>
        <div id="repoTest" class="repo"></div>
      </article>

      <article class="card span-12">
        <h2>Latest Log Line</h2>
        <div id="latestLog" class="logline"></div>
      </article>
    </section>
  </main>

  <script>
    const REFRESH_MS = {refresh_ms};
    let paused = false;
    let timer = null;

    function byId(id) {{ return document.getElementById(id); }}
    function pct(part, total) {{ return total > 0 ? Math.round((part / total) * 100) : 0; }}
    function yn(v) {{ return v ? "yes" : "no"; }}

    function repoMarkup(repo) {{
      if (!repo) return '<div class="line bad">unavailable</div>';
      if (!repo.ok) {{
        return `<div class="line bad">${{repo.error || 'unknown error'}}</div><div class="line mono">${{repo.path || ''}}</div>`;
      }}
      return [
        `<div class="name mono">${{repo.path || ''}}</div>`,
        `<div class="line">branch: <span class="mono">${{repo.branch || ''}}</span></div>`,
        `<div class="line">head: <span class="mono">${{repo.head || ''}}</span></div>`,
        `<div class="line">dirty: <span class="${{repo.dirty ? 'warn' : 'ok'}}">${{yn(repo.dirty)}}</span> · changed files: <span class="mono">${{repo.changed_files ?? 0}}</span></div>`,
      ].join('');
    }}

    function render(snapshot) {{
      const status = snapshot.status || {{}};
      const progress = snapshot.progress || {{}};
      const counts = progress.counts || {{}};
      const done = counts.done || 0;
      const inProgress = counts.in_progress || 0;
      const pending = counts.pending || 0;
      const blocked = counts.blocked || 0;
      const unknown = counts.unknown || 0;
      const total = done + inProgress + pending + blocked + unknown;

      byId("done").textContent = done;
      byId("in_progress").textContent = inProgress;
      byId("pending").textContent = pending;
      byId("blocked").textContent = blocked;
      byId("unknown").textContent = unknown;
      byId("activeTasks").textContent = `active_tasks: ${{(progress.active_tasks || []).join(", ") || "none"}}`;

      byId("taskBar").style.setProperty("--done", pct(done, total) + "%");
      byId("taskBar").style.setProperty("--in_progress", pct(inProgress, total) + "%");
      byId("taskBar").style.setProperty("--pending", pct(pending, total) + "%");
      byId("taskBar").style.setProperty("--blocked", pct(blocked, total) + "%");

      const runnerState = status.runner_running ? '<span class="ok">running</span>' : '<span class="bad">stopped</span>';
      const supervisorState = status.supervisor_running ? '<span class="ok">running</span>' : '<span class="bad">stopped</span>';
      byId("runtimeState").innerHTML = `supervisor: ${{supervisorState}} · runner: ${{runnerState}}`;
      byId("heartbeatState").innerHTML = `heartbeat_age: <span class="mono">${{status.heartbeat_age_sec ?? -1}}s</span> · stale_threshold: <span class="mono">${{status.heartbeat_stale_threshold_sec ?? -1}}s</span>`;

      byId("repoImpl").innerHTML = repoMarkup((snapshot.repos || {{}}).implementation);
      byId("repoTest").innerHTML = repoMarkup((snapshot.repos || {{}}).tests);
      byId("latestLog").textContent = snapshot.latest_log_line || "(no log line yet)";
      byId("updated").textContent = `updated: ${{new Date().toLocaleTimeString()}}`;

      byId("meta").innerHTML = [
        `<span class="pill">runner pid: ${{status.runner_pid ?? "-"}}</span>`,
        `<span class="pill">supervisor pid: ${{status.supervisor_pid ?? "-"}}</span>`,
        `<span class="pill mono">monitor file: ${{snapshot.monitor_file || "-"}}</span>`,
      ].join("");
    }}

    async function refresh() {{
      try {{
        const r = await fetch('/api/monitor', {{ cache: 'no-store' }});
        const payload = await r.json();
        render(payload);
      }} catch (err) {{
        byId("latestLog").textContent = `monitor fetch failed: ${{err}}`;
      }}
    }}

    function schedule() {{
      if (timer) clearInterval(timer);
      timer = setInterval(() => {{ if (!paused) refresh(); }}, REFRESH_MS);
    }}

    byId("refresh").addEventListener("click", refresh);
    byId("pause").addEventListener("click", () => {{
      paused = !paused;
      byId("pause").textContent = paused ? "Resume" : "Pause";
    }});

    refresh();
    schedule();
  </script>
</body>
</html>"""


def start_dashboard(
    config: ManagerConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    refresh_sec: int = 5,
    open_browser: bool = True,
    port_scan: int = 20,
) -> int:
    html = _dashboard_html(refresh_sec)
    snapshot_provider: Callable[[], dict] = lambda: _safe_monitor_snapshot(config)

    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
            body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_text(self, body: str, status: int = HTTPStatus.OK) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path in {"/", "/index.html"}:
                    self._send_html(html)
                    return
                if parsed.path == "/api/monitor":
                    self._send_json(snapshot_provider())
                    return
                if parsed.path == "/api/status":
                    self._send_json(status_snapshot(config))
                    return
                if parsed.path == "/api/health":
                    self._send_json(health_snapshot(config))
                    return
                if parsed.path == "/api/logs":
                    query = parse_qs(parsed.query)
                    raw_lines = query.get("lines", ["80"])[0]
                    try:
                        lines = max(1, min(500, int(raw_lines)))
                    except ValueError:
                        lines = 80
                    self._send_text(tail_logs(config, lines=lines, latest_run_only=True))
                    return
                self._send_text("Not found\n", status=HTTPStatus.NOT_FOUND)
            except Exception as err:  # pragma: no cover - defensive server guard
                self._send_json({"ok": False, "error": str(err)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    server, bound_port = _bind_server_with_port_scan(host, int(port), max(1, int(port_scan)), DashboardHandler)
    server.daemon_threads = True
    url = f"http://{host}:{bound_port}/"
    print(f"dashboard_url={url}", flush=True)

    browser_thread: threading.Thread | None = None
    if open_browser:
        browser_thread = threading.Thread(target=lambda: webbrowser.open(url), daemon=True)
        browser_thread.start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        server.server_close()
        if browser_thread and browser_thread.is_alive():
            browser_thread.join(timeout=0.5)

    return 0


def _safe_monitor_snapshot(config: ManagerConfig) -> dict:
    try:
        return monitor_snapshot(config)
    except Exception as err:
        return {
            "timestamp": "",
            "latest_log_line": f"monitor snapshot error: {err}",
            "status": {
                "supervisor_running": False,
                "runner_running": False,
                "heartbeat_age_sec": -1,
                "heartbeat_stale_threshold_sec": 0,
                "runner_pid": None,
                "supervisor_pid": None,
            },
            "progress": {
                "counts": {"done": 0, "in_progress": 0, "pending": 0, "blocked": 0, "unknown": 1},
                "active_tasks": [],
                "blocked_tasks": [],
            },
            "repos": {
                "implementation": {"ok": False, "error": str(err)},
                "tests": {"ok": False, "error": str(err)},
            },
            "monitor_file": "",
        }


def _bind_server_with_port_scan(
    host: str,
    base_port: int,
    max_scan: int,
    handler_cls: type[BaseHTTPRequestHandler],
) -> tuple[ThreadingHTTPServer, int]:
    last_error: Exception | None = None
    for offset in range(max_scan):
        port = base_port + offset
        try:
            return ThreadingHTTPServer((host, port), handler_cls), port
        except OSError as err:
            last_error = err
            continue
    raise RuntimeError(f"Unable to bind dashboard server on {host}:{base_port}-{base_port + max_scan - 1}: {last_error}")
