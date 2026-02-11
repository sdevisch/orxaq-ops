"""Local-first dashboard for autonomy run reports and evidence artifacts."""

from __future__ import annotations

import html
import json
import mimetypes
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


def collect_dashboard_index(artifacts_root: Path) -> dict[str, Any]:
    root = artifacts_root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    health_json = _rel_paths([*root.glob("**/health.json")], root)
    health_md = _rel_paths([*root.glob("**/health.md")], root)
    run_reports = _rel_paths([*root.glob("**/W*_run.json")], root)
    run_summaries = _rel_paths([*root.glob("**/W*_summary.md")], root)

    evidence_dirs = _rel_paths([path for path in root.glob("rpa_evidence/*/*") if path.is_dir()], root)
    evidence_files = _rel_paths(
        [path for path in root.glob("rpa_evidence/**/*") if path.is_file()][:200],
        root,
    )

    return {
        "generated_at_utc": _utc_now_iso(),
        "artifacts_root": str(root),
        "health_json": health_json,
        "health_md": health_md,
        "run_reports": run_reports,
        "run_summaries": run_summaries,
        "evidence_dirs": evidence_dirs,
        "evidence_files": evidence_files,
    }


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
        return f"<section><h2>{html.escape(title)}</h2><p class='empty'>None found.</p></section>"

    items = []
    for row in rows:
        quoted = urllib_parse.quote(row, safe="/")
        label = html.escape(row)
        items.append(f"<li><a href='/file/{quoted}'>{label}</a></li>")
    return f"<section><h2>{html.escape(title)}</h2><ul>{''.join(items)}</ul></section>"


def render_dashboard_html(index_payload: dict[str, Any]) -> str:
    sections = [
        _render_file_links("Health JSON", list(index_payload.get("health_json", []))),
        _render_file_links("Health Markdown", list(index_payload.get("health_md", []))),
        _render_file_links("Run Reports", list(index_payload.get("run_reports", []))),
        _render_file_links("Run Summaries", list(index_payload.get("run_summaries", []))),
        _render_file_links("Evidence Directories", list(index_payload.get("evidence_dirs", []))),
        _render_file_links("Evidence Files", list(index_payload.get("evidence_files", []))),
    ]
    generated = html.escape(str(index_payload.get("generated_at_utc", "")))
    artifacts_root = html.escape(str(index_payload.get("artifacts_root", "")))
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
    }}
    * {{ box-sizing: border-box; }}
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
    @media (max-width: 700px) {{
      main {{ padding: 14px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Orxaq Autonomy Dashboard</h1>
      <p>Generated: {generated}</p>
      <p>Artifacts root: <code>{artifacts_root}</code></p>
      <p>API index: <a href="/api/index">/api/index</a></p>
    </header>
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
