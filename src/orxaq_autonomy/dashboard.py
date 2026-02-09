"""Local GUI dashboard for autonomy monitoring."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .manager import (
    ManagerConfig,
    conversations_snapshot,
    ensure_lanes_background,
    health_snapshot,
    lane_status_snapshot,
    lane_status_fallback_snapshot,
    monitor_snapshot,
    start_lanes_background,
    status_snapshot,
    stop_lanes_background,
    tail_logs,
)

_COMMIT_COUNT_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, int]] = {}
_COMMIT_TIMELINE_CACHE: dict[tuple[str, tuple[str, ...], int, int], tuple[float, dict[str, Any]]] = {}
_COMMIT_CACHE_TTL_SEC = 20.0
_COLLAB_ACTIVITY_BUCKETS = 12
_COLLAB_ACTIVITY_BUCKET_SEC = 300


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
    .tab-nav {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      padding-top: 2px;
    }}
    .tab-btn {{
      border-radius: 999px;
      font-weight: 700;
      background: #ffffff;
      color: var(--muted);
      border-color: rgba(15, 23, 42, 0.16);
    }}
    .tab-btn.active {{
      color: var(--ink);
      border-color: var(--info);
      background: linear-gradient(180deg, #f5f9ff, #ecf4ff);
      box-shadow: 0 0 0 4px var(--ring);
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
    input {{
      border: 1px solid var(--border);
      border-radius: 9px;
      padding: 6px 9px;
      font-size: .82rem;
      background: #fff;
      color: var(--ink);
      min-width: 0;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 14px;
    }}
    .tab-panel {{
      display: none;
    }}
    .tab-panel.active {{
      display: grid;
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
    .feed {{
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #0b1324;
      color: #ddf0ff;
      padding: 10px;
      font-family: "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
      font-size: .8rem;
      max-height: 320px;
      overflow: auto;
      display: grid;
      gap: 8px;
    }}
    .feed-item {{
      border: 1px solid rgba(221, 240, 255, 0.18);
      border-radius: 8px;
      padding: 8px;
      background: rgba(8, 20, 38, 0.85);
    }}
    .feed-head {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      color: #a3d4ff;
      margin-bottom: 4px;
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
    .diag-list {{
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #fff;
      padding: 9px;
      display: grid;
      gap: 4px;
    }}
    .diag-item {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: baseline;
      color: var(--muted);
      font-size: .82rem;
    }}
    .diag-item .diag-name {{ min-width: 170px; font-weight: 680; color: var(--ink); }}
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
    .inline-controls {{
      display: grid;
      gap: 8px;
    }}
    .fields {{
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .fields .full {{
      grid-column: 1 / -1;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    .transport {{
      display: grid;
      gap: 8px;
    }}
    .transport-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .arrangement {{
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #0a1424;
      padding: 10px;
      display: grid;
      gap: 8px;
    }}
    .arrangement-grid {{
      display: grid;
      gap: 6px;
      max-height: 360px;
      overflow: auto;
    }}
    .track {{
      display: grid;
      grid-template-columns: 170px 1fr;
      gap: 8px;
      align-items: center;
    }}
    .track-head {{
      color: #d6ecff;
      font-family: "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
      font-size: .76rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .track-lane {{
      position: relative;
      min-height: 22px;
      border: 1px solid rgba(221, 240, 255, 0.2);
      border-radius: 6px;
      background: linear-gradient(180deg, rgba(8, 20, 38, 0.95), rgba(8, 20, 38, 0.7));
      overflow: hidden;
    }}
    .clip {{
      position: absolute;
      top: 3px;
      bottom: 3px;
      border-radius: 4px;
      border: 1px solid rgba(255, 255, 255, 0.2);
    }}
    .clip-midi {{
      background: linear-gradient(135deg, #4ecdc4, #2a9d8f);
    }}
    .clip-audio {{
      background: linear-gradient(135deg, #f4a261, #e76f51);
    }}
    .clip-control {{
      background: linear-gradient(135deg, #8ecae6, #219ebc);
    }}
    .mixer {{
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #fff;
      padding: 10px;
      display: grid;
      gap: 6px;
      max-height: 360px;
      overflow: auto;
    }}
    .mixer-strip {{
      display: grid;
      grid-template-columns: 150px 1fr 46px;
      gap: 8px;
      align-items: center;
    }}
    .table-wrap {{
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #fff;
      overflow: auto;
    }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: .82rem;
      min-width: 1180px;
    }}
    .data-table th {{
      text-align: left;
      font-weight: 700;
      padding: 9px;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
      background: #f8fbff;
      white-space: nowrap;
    }}
    .data-table td {{
      padding: 8px 9px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
      white-space: nowrap;
    }}
    .data-table tr:last-child td {{
      border-bottom: none;
    }}
    .live-indicator {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-weight: 650;
    }}
    .live-dot {{
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: #94a3b8;
      box-shadow: 0 0 0 0 rgba(148, 163, 184, 0.35);
    }}
    .live-thinking .live-dot {{
      background: var(--ok);
      box-shadow: 0 0 0 0 rgba(15, 159, 95, 0.4);
      animation: livePulse 1.2s ease-out infinite;
    }}
    .live-active .live-dot {{
      background: var(--info);
      box-shadow: 0 0 0 0 rgba(31, 111, 235, 0.36);
      animation: livePulse 1.4s ease-out infinite;
    }}
    .live-stale .live-dot {{
      background: var(--warn);
    }}
    .live-offline .live-dot {{
      background: var(--bad);
    }}
    @keyframes livePulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(15, 159, 95, 0.42); }}
      100% {{ box-shadow: 0 0 0 9px rgba(15, 159, 95, 0); }}
    }}
    .led-strip {{
      display: inline-grid;
      grid-auto-flow: column;
      grid-auto-columns: 8px;
      gap: 3px;
      align-items: center;
    }}
    .led {{
      width: 8px;
      height: 8px;
      border-radius: 2px;
      background: #cbd5e1;
      opacity: 0.25;
    }}
    .led.on {{
      opacity: 0.95;
      background: linear-gradient(180deg, #95f39f, #17a34a);
      animation: ledWink .9s ease-in-out infinite alternate;
    }}
    .led.live.on {{
      background: linear-gradient(180deg, #9be7ff, #1f6feb);
    }}
    .led.stale.on {{
      background: linear-gradient(180deg, #ffd08a, #c77d00);
    }}
    .led.offline.on {{
      background: linear-gradient(180deg, #f2b6b6, #cc2f2f);
      opacity: 0.5;
    }}
    .sparkline-strip {{
      display: inline-grid;
      grid-auto-flow: column;
      grid-auto-columns: 6px;
      gap: 2px;
      align-items: end;
      min-height: 18px;
      padding: 1px 0;
    }}
    .sparkline-bar {{
      width: 6px;
      height: var(--h, 15%);
      min-height: 3px;
      border-radius: 2px;
      background: #d7e3ef;
      opacity: 0.45;
      transition: height .22s ease;
    }}
    .sparkline-bar.active {{
      opacity: 0.95;
      background: linear-gradient(180deg, #8fdadf, #2c99a4);
      animation: sparkShift 1.05s ease-in-out infinite alternate;
    }}
    .sparkline-strip.sparkline-commit .sparkline-bar.active {{
      background: linear-gradient(180deg, #9bd2ff, #1f6feb);
    }}
    .sparkline-strip.stale .sparkline-bar.active {{
      background: linear-gradient(180deg, #ffd08a, #c77d00);
    }}
    .sparkline-strip.offline .sparkline-bar.active {{
      background: linear-gradient(180deg, #f2b6b6, #cc2f2f);
      opacity: 0.65;
    }}
    @keyframes sparkShift {{
      from {{ filter: brightness(0.94); transform: translateY(0); }}
      to {{ filter: brightness(1.08); transform: translateY(-1px); }}
    }}
    .cell-stack {{
      display: grid;
      gap: 3px;
    }}
    .attention-badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: .72rem;
      font-weight: 700;
      border: 1px solid transparent;
      letter-spacing: 0.01em;
    }}
    .attention-critical {{
      color: #8f1d1d;
      background: #fde2e2;
      border-color: #f4b7b7;
    }}
    .attention-warn {{
      color: #7a4a00;
      background: #fff1d6;
      border-color: #f2d59b;
    }}
    .attention-watch {{
      color: #355f8a;
      background: #e5f0ff;
      border-color: #bfd8ff;
    }}
    .attention-ok {{
      color: #0b6b3d;
      background: #ddf8e7;
      border-color: #addfbe;
    }}
    @keyframes ledWink {{
      from {{ transform: translateY(0); filter: brightness(0.95); }}
      to {{ transform: translateY(-1px); filter: brightness(1.15); }}
    }}
    .meter {{
      height: 11px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #f1f5f9;
      overflow: hidden;
    }}
    .meter-fill {{
      height: 100%;
      background: linear-gradient(90deg, #65d26e, #f5b041 65%, #e74c3c);
    }}
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
      <div class="tab-nav" role="tablist" aria-label="Dashboard sections">
        <button id="tabOverview" class="tab-btn active" data-tab="overview" type="button">Overview</button>
        <button id="tabRouting" class="tab-btn" data-tab="routing" type="button">Routing Monitor</button>
      </div>
    </section>

    <section class="grid tab-panel active" data-tab-panel="overview">
      <article class="card span-12">
        <h2>DAW Session (Logic Mode)</h2>
        <div class="transport">
          <div class="transport-row mono">
            <span id="transportTempo" class="pill">tempo: 120 BPM</span>
            <span id="transportPlayhead" class="pill">playhead: 0.0s</span>
            <span id="transportWindow" class="pill">window: 120s</span>
            <span id="dawSummary" class="pill">tracks: loading...</span>
          </div>
          <div id="arrangementView" class="arrangement">
            <div id="arrangementTracks" class="arrangement-grid"></div>
          </div>
        </div>
      </article>

      <article class="card span-6">
        <h2>Mixer</h2>
        <div id="mixerView" class="mixer"></div>
      </article>

      <article class="card span-6">
        <h2>Prompt MIDI / Response Audio Activity</h2>
        <div id="activitySummary" class="mono">activity: loading...</div>
        <div id="activityEvents" class="feed"></div>
      </article>

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
          <div class="stat"><div class="k">Completed (24h)</div><div id="completed_24h" class="v">0</div></div>
        </div>
        <div id="activeTasks" class="mono">active_tasks: none</div>
        <div id="completed24hSummary" class="mono">completed_last_24h: 0</div>
      </article>

      <article class="card span-4">
        <h2>Runtime</h2>
        <div id="runtimeState" class="mono">loading...</div>
        <div id="heartbeatState" class="mono"></div>
      </article>

      <article class="card span-8">
        <h2>Autonomous PID Watchdog</h2>
        <div id="watchdogSummary" class="mono">watchdog: loading...</div>
        <div class="stats">
          <div class="stat"><div class="k">Processes</div><div id="watchdogProcessTotal" class="v">0</div></div>
          <div class="stat"><div class="k">Healthy</div><div id="watchdogHealthy" class="v">0</div></div>
          <div class="stat"><div class="k">Restarted</div><div id="watchdogRestarted" class="v">0</div></div>
          <div class="stat"><div class="k">Problematic</div><div id="watchdogProblematic" class="v">0</div></div>
          <div class="stat"><div class="k">Runs</div><div id="watchdogRuns" class="v">0</div></div>
        </div>
        <div id="watchdogList" class="repo"></div>
        <div id="watchdogEvents" class="feed"></div>
      </article>

      <article class="card span-12">
        <h2>Collaborative Agent Runtime</h2>
        <div id="collabSummary" class="mono">collaboration runtime: loading...</div>
        <div id="collabActivity" class="mono">live activity: loading...</div>
        <div id="collabAnomaly" class="mono">attention: loading...</div>
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>AI</th>
                <th>Work Title</th>
                <th>PID</th>
                <th>Running</th>
                <th>Latest Health</th>
                <th>Commits (1h)</th>
                <th>Latest Success</th>
                <th>Live Heartbeat</th>
                <th>Signal LEDs</th>
                <th>Attention</th>
              </tr>
            </thead>
            <tbody id="collabTableBody">
              <tr><td colspan="10" class="mono">Loading collaborative lanes...</td></tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="card span-4">
        <h2>Cost &amp; Quality</h2>
        <div id="excitingStat" class="logline">Most exciting stat: loading...</div>
        <div id="metricsSummary" class="mono">metrics: loading...</div>
        <div id="metricsEconomics" class="mono">economics: loading...</div>
        <div id="metricsList" class="repo"></div>
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

      <article class="card span-6">
        <h2>Parallel Lanes</h2>
        <div id="laneSummary" class="mono">lanes: loading...</div>
        <div id="laneOwnerSummary" class="mono">owners: loading...</div>
        <div class="inline-controls">
          <div class="actions">
            <input id="laneTarget" type="text" placeholder="lane id (optional)" />
            <button id="laneStatus">Status</button>
            <button id="laneEnsure">Ensure</button>
            <button id="laneStart">Start</button>
            <button id="laneStop">Stop</button>
          </div>
          <div id="laneActionStatus" class="mono">lane action: idle</div>
        </div>
        <div id="laneList" class="repo"></div>
      </article>

      <article class="card span-6">
        <h2>Conversations</h2>
        <div class="inline-controls">
          <div class="fields">
            <input id="convOwner" type="text" placeholder="owner" />
            <input id="convLane" type="text" placeholder="lane id" />
            <input id="convType" type="text" placeholder="event type" />
            <input id="convTail" type="number" min="0" step="1" placeholder="tail events" />
            <input id="convContains" class="full" type="text" placeholder="contains text" />
          </div>
          <div class="actions">
            <button id="convApply">Apply filters</button>
            <button id="convClear">Clear filters</button>
          </div>
        </div>
        <div id="conversationSummary" class="mono">events: loading...</div>
        <div id="conversationSources" class="mono">source health: loading...</div>
        <div id="conversationFeed" class="feed"></div>
      </article>

      <article class="card span-12">
        <h2>Resilience Diagnostics</h2>
        <div id="resilienceSummary" class="mono">sources: loading...</div>
        <div id="resilienceList" class="diag-list"></div>
      </article>
    </section>

    <section class="grid tab-panel" data-tab-panel="routing">
      <article class="card span-12">
        <h2>Routing Overview</h2>
        <div id="routingSummary" class="mono">routing: loading...</div>
        <div class="stats">
          <div class="stat"><div class="k">Decisions</div><div id="routingDecisions" class="v">0</div></div>
          <div class="stat"><div class="k">RouteLLM</div><div id="routingRoutellmRate" class="v">0%</div></div>
          <div class="stat"><div class="k">Fallbacks</div><div id="routingFallbackRate" class="v">0%</div></div>
          <div class="stat"><div class="k">Router Errors</div><div id="routingRouterErrorRate" class="v">0%</div></div>
          <div class="stat"><div class="k">Avg Router Latency</div><div id="routingLatencyAvg" class="v">0ms</div></div>
          <div class="stat"><div class="k">Enabled Lanes</div><div id="routingEnabledLanes" class="v">0</div></div>
          <div class="stat"><div class="k">Est. Tokens Used</div><div id="routingEstimatedTokens" class="v">0</div></div>
          <div class="stat"><div class="k">Blended Est. $ / 1M</div><div id="routingBlendedCostPerM" class="v">$0.000</div></div>
        </div>
      </article>

      <article class="card span-6">
        <h2>Provider Routing Health</h2>
        <div id="routingProviderSummary" class="mono">providers: loading...</div>
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>Provider</th>
                <th>Responses</th>
                <th>RouteLLM</th>
                <th>Fallback</th>
                <th>Router Errors</th>
                <th>Est. $ / 1M</th>
              </tr>
            </thead>
            <tbody id="routingProviderBody">
              <tr><td colspan="6" class="mono">Loading routing providers...</td></tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="card span-6">
        <h2>Lane Router Configuration</h2>
        <div id="routingLaneSummary" class="mono">lane routing config: loading...</div>
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>Lane</th>
                <th>Health</th>
                <th>RouteLLM</th>
                <th>Router URL</th>
                <th>Decisions (tail)</th>
                <th>Fallbacks</th>
                <th>Errors</th>
              </tr>
            </thead>
            <tbody id="routingLaneBody">
              <tr><td colspan="7" class="mono">Loading lane routing...</td></tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="card span-12">
        <h2>Recent Routing Decisions</h2>
        <div id="routingDecisionSummary" class="mono">routing decisions: loading...</div>
        <div id="routingDecisionFeed" class="feed"></div>
      </article>
    </section>
  </main>

  <script>
    const REFRESH_MS = {refresh_ms};
    const FETCH_TIMEOUT_MS = Math.max(1800, Math.min(12000, Math.floor(REFRESH_MS * 0.8)));
    const ROUTING_DECISION_TAIL = 180;
    let paused = false;
    let timer = null;
    let activeTab = "overview";
    const conversationFilters = {{
      owner: "",
      lane: "",
      event_type: "",
      contains: "",
      tail: 0,
    }};
    let lastSuccessfulMonitor = null;
    let lastSuccessfulLanePayload = null;
    let lastSuccessfulConversationPayload = null;
    let lastSuccessfulDawPayload = null;
    let lastSuccessfulWatchdogPayload = null;
    let lastSuccessfulCollabPayload = null;
    let lastSuccessfulRoutingPayload = null;

    function byId(id) {{ return document.getElementById(id); }}
    function pct(part, total) {{ return total > 0 ? Math.round((part / total) * 100) : 0; }}
    function yn(v) {{ return v ? "yes" : "no"; }}
    function escapeHtml(value) {{
      return String(value || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}
    const USER_TIMEZONE = (() => {{
      try {{
        return Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
      }} catch (_err) {{
        return "local";
      }}
    }})();
    const USER_TIMESTAMP_FORMATTER = new Intl.DateTimeFormat(undefined, {{
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZoneName: "short",
    }});
    function formatTimestamp(value) {{
      const raw = String(value || "").trim();
      if (!raw) return "";
      const parsed = new Date(raw);
      if (!Number.isFinite(parsed.getTime())) return raw;
      return USER_TIMESTAMP_FORMATTER.format(parsed);
    }}
    function formatNowTimestamp() {{
      return USER_TIMESTAMP_FORMATTER.format(new Date());
    }}
    function formatAgeSeconds(value) {{
      const seconds = Number(value);
      if (!Number.isFinite(seconds) || seconds < 0) return "-";
      if (seconds < 60) return `${{Math.round(seconds)}}s`;
      if (seconds < 3600) return `${{Math.round(seconds / 60)}}m`;
      return `${{(seconds / 3600).toFixed(1)}}h`;
    }}
    function formatPercent(numerator, denominator) {{
      const numer = Number(numerator || 0);
      const denom = Number(denominator || 0);
      if (!Number.isFinite(numer) || !Number.isFinite(denom) || denom <= 0) return "0%";
      return `${{Math.round((numer / denom) * 100)}}%`;
    }}
    function formatDuration(value) {{
      const seconds = Number(value);
      if (!Number.isFinite(seconds) || seconds < 0) return "-";
      const d = Math.floor(seconds / 86400);
      const h = Math.floor((seconds % 86400) / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = Math.floor(seconds % 60);
      if (d > 0) return `${{d}}d ${{h}}h`;
      if (h > 0) return `${{h}}h ${{m}}m`;
      if (m > 0) return `${{m}}m ${{s}}s`;
      return `${{s}}s`;
    }}
    function stateBadge(ok) {{ return ok ? '<span class="ok">ok</span>' : '<span class="bad">error</span>'; }}
    function parseTail(value) {{
      const parsed = Number(value || 0);
      if (!Number.isFinite(parsed) || parsed < 0) return 0;
      return Math.floor(parsed);
    }}
    function conversationPath() {{
      const query = new URLSearchParams();
      query.set("lines", "200");
      if (conversationFilters.owner) query.set("owner", conversationFilters.owner);
      if (conversationFilters.lane) query.set("lane", conversationFilters.lane);
      if (conversationFilters.event_type) query.set("event_type", conversationFilters.event_type);
      if (conversationFilters.contains) query.set("contains", conversationFilters.contains);
      if (conversationFilters.tail > 0) query.set("tail", String(conversationFilters.tail));
      return `/api/conversations?${{query.toString()}}`;
    }}
    function laneStatusPath() {{
      const laneTarget = String(byId("laneTarget").value || "").trim();
      const query = new URLSearchParams();
      query.set("include_conversations", "1");
      query.set("conversation_lines", "200");
      if (laneTarget) query.set("lane", laneTarget);
      return `/api/lanes?${{query.toString()}}`;
    }}
    function routingDecisionPath() {{
      const query = new URLSearchParams();
      query.set("lines", "600");
      query.set("include_lanes", "1");
      query.set("event_type", "routing_decision");
      query.set("tail", String(ROUTING_DECISION_TAIL));
      return `/api/conversations?${{query.toString()}}`;
    }}
    function fallbackLanePayloadFromMonitor(monitorPayload, laneTarget, laneEndpointError) {{
      const monitorLanes = (
        monitorPayload &&
        monitorPayload.lanes &&
        typeof monitorPayload.lanes === "object"
      ) ? monitorPayload.lanes : {{}};
      const requestedLaneRaw = String(laneTarget || "").trim();
      const laneItemsRaw = Array.isArray(monitorLanes.lanes)
        ? monitorLanes.lanes.filter((item) => item && typeof item === "object")
        : [];
      const requestedLaneLower = requestedLaneRaw.toLowerCase();
      let resolvedRequestedLane = requestedLaneRaw;
      if (requestedLaneRaw) {{
        const exactMatch = laneItemsRaw.some((lane) => String(lane.id || "").trim() === requestedLaneRaw);
        if (!exactMatch) {{
          const foldedMatches = laneItemsRaw.filter(
            (lane) => String(lane.id || "").trim().toLowerCase() === requestedLaneLower
          );
          if (foldedMatches.length === 1) {{
            resolvedRequestedLane = String(foldedMatches[0].id || "").trim() || requestedLaneRaw;
          }}
        }}
      }}
      const resolvedRequestedLaneLower = resolvedRequestedLane.toLowerCase();
      const laneItems = resolvedRequestedLane
        ? laneItemsRaw.filter((lane) => String(lane.id || "").trim().toLowerCase() === resolvedRequestedLaneLower)
        : laneItemsRaw;

      const healthCounts = {{}};
      const ownerCounts = {{}};
      for (const lane of laneItems) {{
        const health = String(lane.health || "unknown").trim().toLowerCase() || "unknown";
        healthCounts[health] = Number(healthCounts[health] || 0) + 1;
        const owner = String(lane.owner || "unknown").trim() || "unknown";
        if (!ownerCounts[owner]) {{
          ownerCounts[owner] = {{ total: 0, running: 0, healthy: 0, degraded: 0 }};
        }}
        const ownerEntry = ownerCounts[owner];
        ownerEntry.total += 1;
        if (lane.running) ownerEntry.running += 1;
        if (health === "ok" || health === "paused" || health === "idle") {{
          ownerEntry.healthy += 1;
        }} else {{
          ownerEntry.degraded += 1;
        }}
      }}

      const errors = [];
      if (Array.isArray(monitorLanes.errors)) {{
        for (const item of monitorLanes.errors) {{
          const message = String(item || "").trim();
          if (message) errors.push(message);
        }}
      }}
      if (laneEndpointError) {{
        errors.push(`lane endpoint: ${{String(laneEndpointError).trim()}}`);
      }}
      if (resolvedRequestedLane && laneItems.length === 0) {{
        const lanesFile = String(monitorLanes.lanes_file || "").trim();
        if (lanesFile) {{
          errors.push(`Unknown lane id '${{resolvedRequestedLane}}'. Update ${{lanesFile}}.`);
        }} else {{
          errors.push(`Unknown lane id '${{resolvedRequestedLane}}'.`);
        }}
      }}

      return {{
        ...monitorLanes,
        requested_lane: resolvedRequestedLane || "all",
        lanes: laneItems,
        total_count: laneItems.length,
        running_count: laneItems.filter((lane) => Boolean(lane.running)).length,
        health_counts: healthCounts,
        owner_counts: ownerCounts,
        errors,
        ok: false,
        partial: true,
      }};
    }}
    function filterFallbackConversationEvents(events, filters) {{
      const ownerFilter = String((filters && filters.owner) || "").trim().toLowerCase();
      const laneFilter = String((filters && filters.lane) || "").trim().toLowerCase();
      const typeFilter = String((filters && filters.event_type) || "").trim().toLowerCase();
      const containsFilter = String((filters && filters.contains) || "").trim().toLowerCase();
      const tailFilter = parseTail(filters && filters.tail);
      const allEvents = Array.isArray(events) ? events.filter((item) => item && typeof item === "object") : [];
      let filtered = allEvents.filter((item) => {{
        const ownerValue = String(item.owner || "").trim().toLowerCase();
        const laneValue = String(item.lane_id || "").trim().toLowerCase();
        const typeValue = String(item.event_type || "").trim().toLowerCase();
        const haystack = [
          item.timestamp || "",
          item.owner || "",
          item.lane_id || "",
          item.task_id || "",
          item.event_type || "",
          item.content || "",
        ].join(" ").toLowerCase();
        if (ownerFilter && ownerValue !== ownerFilter) return false;
        if (laneFilter && laneValue !== laneFilter) return false;
        if (typeFilter && typeValue !== typeFilter) return false;
        if (containsFilter && !haystack.includes(containsFilter)) return false;
        return true;
      }});
      if (tailFilter > 0) {{
        filtered = filtered.slice(-tailFilter);
      }}
      const ownerCounts = {{}};
      for (const event of filtered) {{
        const owner = String(event.owner || "unknown").trim() || "unknown";
        ownerCounts[owner] = Number(ownerCounts[owner] || 0) + 1;
      }}
      return {{
        events: filtered,
        owner_counts: ownerCounts,
        total_events: filtered.length,
        unfiltered_total_events: allEvents.length,
      }};
    }}
    function filterFallbackConversationSources(sources, filters) {{
      const laneFilter = String((filters && filters.lane) || "").trim();
      const allSources = Array.isArray(sources) ? sources.filter((item) => item && typeof item === "object") : [];
      if (!laneFilter) {{
        return {{
          sources: allSources,
          suppressed_sources: [],
          suppressed_source_count: 0,
          suppressed_source_errors: [],
          suppressed_source_error_count: 0,
        }};
      }}
      const laneFilterLower = laneFilter.toLowerCase();
      const retained = [];
      const suppressed = [];
      for (const source of allSources) {{
        const sourceLane = String(source.lane_id || "").trim();
        if (sourceLane && sourceLane.toLowerCase() !== laneFilterLower) {{
          suppressed.push(source);
          continue;
        }}
        retained.push(source);
      }}
      const laneSourceHealthy = retained.some((source) => {{
        const sourceLane = String(source.lane_id || "").trim();
        return sourceLane.toLowerCase() === laneFilterLower && Boolean(source.ok);
      }});
      let scoped = retained;
      if (laneSourceHealthy) {{
        scoped = [];
        for (const source of retained) {{
          const sourceLane = String(source.lane_id || "").trim();
          if (sourceLane) {{
            scoped.push(source);
            continue;
          }}
          const sourceKind = String(source.resolved_kind || source.kind || "").trim().toLowerCase();
          if (Boolean(source.ok) || sourceKind !== "primary") {{
            scoped.push(source);
            continue;
          }}
          suppressed.push(source);
        }}
      }}
      const suppressedSourceErrors = suppressed
        .map((source) => String(source.error || "").trim())
        .filter((message) => Boolean(message));
      return {{
        sources: scoped,
        suppressed_sources: suppressed,
        suppressed_source_count: suppressed.length,
        suppressed_source_errors: suppressedSourceErrors,
        suppressed_source_error_count: suppressedSourceErrors.length,
      }};
    }}
    function fallbackConversationPayloadFromMonitor(monitorPayload, endpointError) {{
      const monitorRecentEvents = (
        monitorPayload &&
        monitorPayload.conversations &&
        Array.isArray(monitorPayload.conversations.recent_events)
      ) ? monitorPayload.conversations.recent_events : [];
      const monitorLatestEvent = (
        monitorPayload &&
        monitorPayload.conversations &&
        monitorPayload.conversations.latest
      ) ? monitorPayload.conversations.latest : null;
      const fallbackEvents = monitorRecentEvents.length
        ? monitorRecentEvents
        : (monitorLatestEvent ? [monitorLatestEvent] : []);
      const fallbackFilters = {{
        owner: conversationFilters.owner,
        lane: conversationFilters.lane,
        event_type: conversationFilters.event_type,
        contains: conversationFilters.contains,
        tail: conversationFilters.tail,
      }};
      const filteredFallback = filterFallbackConversationEvents(fallbackEvents, fallbackFilters);
      const sourceFallback = filterFallbackConversationSources(
        monitorPayload && monitorPayload.conversations
          ? monitorPayload.conversations.sources
          : [],
        fallbackFilters,
      );
      const errorText = String(endpointError || "conversation endpoint unavailable");
      if (monitorPayload && monitorPayload.conversations) {{
        return {{
          total_events: filteredFallback.total_events,
          owner_counts: filteredFallback.owner_counts,
          events: filteredFallback.events,
          sources: sourceFallback.sources,
          suppressed_sources: sourceFallback.suppressed_sources,
          suppressed_source_count: sourceFallback.suppressed_source_count,
          suppressed_source_errors: sourceFallback.suppressed_source_errors,
          suppressed_source_error_count: sourceFallback.suppressed_source_error_count,
          partial: true,
          ok: false,
          errors: [errorText],
          filters: fallbackFilters,
          unfiltered_total_events: filteredFallback.unfiltered_total_events,
        }};
      }}
      return {{
        total_events: 0,
        owner_counts: {{}},
        events: [],
        sources: [],
        suppressed_sources: [],
        suppressed_source_count: 0,
        suppressed_source_errors: [],
        suppressed_source_error_count: 0,
        partial: true,
        ok: false,
        errors: [errorText],
        filters: fallbackFilters,
        unfiltered_total_events: 0,
      }};
    }}
    function fallbackConversationPayloadFromCache(cachePayload, endpointError) {{
      const cached = (cachePayload && typeof cachePayload === "object") ? cachePayload : {{}};
      const fallbackFilters = {{
        owner: conversationFilters.owner,
        lane: conversationFilters.lane,
        event_type: conversationFilters.event_type,
        contains: conversationFilters.contains,
        tail: conversationFilters.tail,
      }};
      const cachedEvents = Array.isArray(cached.events) ? cached.events : [];
      const filteredFallback = filterFallbackConversationEvents(cachedEvents, fallbackFilters);
      const sourceFallback = filterFallbackConversationSources(
        Array.isArray(cached.sources) ? cached.sources : [],
        fallbackFilters,
      );
      const errors = [String(endpointError || "conversation endpoint unavailable"), "conversation data from stale cache"];
      return {{
        total_events: filteredFallback.total_events,
        owner_counts: filteredFallback.owner_counts,
        events: filteredFallback.events,
        sources: sourceFallback.sources,
        suppressed_sources: sourceFallback.suppressed_sources,
        suppressed_source_count: sourceFallback.suppressed_source_count,
        suppressed_source_errors: sourceFallback.suppressed_source_errors,
        suppressed_source_error_count: sourceFallback.suppressed_source_error_count,
        partial: true,
        ok: false,
        errors,
        filters: fallbackFilters,
        unfiltered_total_events: filteredFallback.unfiltered_total_events,
      }};
    }}
    function syncConversationInputs() {{
      byId("convOwner").value = conversationFilters.owner;
      byId("convLane").value = conversationFilters.lane;
      byId("convType").value = conversationFilters.event_type;
      byId("convContains").value = conversationFilters.contains;
      byId("convTail").value = conversationFilters.tail > 0 ? String(conversationFilters.tail) : "";
    }}
    function setLaneActionStatus(message, isError = false) {{
      const el = byId("laneActionStatus");
      el.textContent = `lane action: ${{message}}`;
      el.className = isError ? "mono bad" : "mono";
    }}
    function setActiveTab(tabName) {{
      const normalized = String(tabName || "").trim().toLowerCase();
      activeTab = normalized === "routing" ? "routing" : "overview";
      document.querySelectorAll("[data-tab]").forEach((button) => {{
        const target = String(button.getAttribute("data-tab") || "").trim().toLowerCase();
        button.classList.toggle("active", target === activeTab);
      }});
      document.querySelectorAll("[data-tab-panel]").forEach((panel) => {{
        const panelName = String(panel.getAttribute("data-tab-panel") || "").trim().toLowerCase();
        panel.classList.toggle("active", panelName === activeTab);
      }});
    }}
    function initTabs() {{
      document.querySelectorAll("[data-tab]").forEach((button) => {{
        button.addEventListener("click", () => {{
          setActiveTab(button.getAttribute("data-tab") || "overview");
        }});
      }});
      setActiveTab(activeTab);
    }}
    window.OrxaqThemeAPI = {{
      applySkin(tokens) {{
        const root = document.documentElement;
        for (const [key, value] of Object.entries(tokens || {{}})) {{
          root.style.setProperty(`--${{key}}`, String(value));
        }}
      }},
      readSkin() {{
        const style = getComputedStyle(document.documentElement);
        return {{
          bg_0: style.getPropertyValue("--bg-0").trim(),
          bg_1: style.getPropertyValue("--bg-1").trim(),
          bg_2: style.getPropertyValue("--bg-2").trim(),
          panel: style.getPropertyValue("--panel").trim(),
          ink: style.getPropertyValue("--ink").trim(),
          muted: style.getPropertyValue("--muted").trim(),
          border: style.getPropertyValue("--border").trim(),
        }};
      }},
    }};

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

    function renderDaw(payload) {{
      const daw = payload || {{}};
      const tracks = Array.isArray(daw.tracks) ? daw.tracks : [];
      const strips = Array.isArray(daw.mixer) ? daw.mixer : [];
      const events = Array.isArray(daw.activity_feed) ? daw.activity_feed : [];
      const windowSec = Number(daw.window_sec || 120);
      const playheadSec = Number(daw.playhead_sec || 0);
      const tempo = Number(daw.tempo_bpm || 120);

      byId("transportTempo").textContent = `tempo: ${{tempo}} BPM`;
      byId("transportPlayhead").textContent = `playhead: ${{playheadSec.toFixed(1)}}s`;
      byId("transportWindow").textContent = `window: ${{windowSec}}s`;
      byId("dawSummary").textContent = `tracks: ${{tracks.length}} · strips: ${{strips.length}}`;

      byId("arrangementTracks").innerHTML = tracks.length
        ? tracks.map((track) => {{
            const clips = Array.isArray(track.clips) ? track.clips : [];
            const clipMarkup = clips.map((clip) => {{
              const start = Math.max(0, Math.min(100, Number(clip.start_pct || 0)));
              const width = Math.max(1.2, Math.min(100 - start, Number(clip.width_pct || 1.2)));
              const css = clip.kind === "midi" ? "clip-midi" : (clip.kind === "audio" ? "clip-audio" : "clip-control");
              const title = `${{clip.label || clip.kind}} · lvl=${{Number(clip.level || 0).toFixed(2)}}`;
              return `<div class="clip ${{css}}" title="${{escapeHtml(title)}}" style="left:${{start}}%;width:${{width}}%;"></div>`;
            }}).join("");
            return [
              '<div class="track">',
              `<div class="track-head">${{escapeHtml(track.name || "track")}}</div>`,
              `<div class="track-lane">${{clipMarkup}}</div>`,
              '</div>',
            ].join("");
          }}).join("")
        : '<div class="track-head">No DAW activity yet.</div>';

      byId("mixerView").innerHTML = strips.length
        ? strips.map((strip) => {{
            const level = Math.max(0, Math.min(1, Number(strip.level || 0)));
            return [
              '<div class="mixer-strip">',
              `<div class="mono">${{escapeHtml(strip.name || "track")}}</div>`,
              `<div class="meter"><div class="meter-fill" style="width:${{Math.round(level * 100)}}%;"></div></div>`,
              `<div class="mono">${{Math.round(level * 100)}}%</div>`,
              '</div>',
            ].join("");
          }}).join("")
        : '<div class="mono">No channels yet.</div>';

      byId("activitySummary").textContent =
        `prompt_midi=${{Number(daw.prompt_midi_events || 0)}} · response_audio=${{Number(daw.response_audio_events || 0)}} · control=${{Number(daw.control_events || 0)}}`;
      byId("activityEvents").innerHTML = events.length
        ? events.map((event) => {{
            return [
              '<div class="feed-item">',
              `<div class="feed-head"><span>${{escapeHtml(formatTimestamp(event.timestamp || ""))}}</span><span>track=${{escapeHtml(event.track || "-")}}</span><span>kind=${{escapeHtml(event.kind || "-")}}</span><span>type=${{escapeHtml(event.event_type || "-")}}</span></div>`,
              `<div>${{escapeHtml(event.label || "")}}</div>`,
              '</div>',
            ].join("");
          }}).join("")
        : '<div class="feed-item">No recent activity.</div>';
    }}

    function renderWatchdog(payload) {{
      const state = (payload && typeof payload === "object") ? payload : {{}};
      const processes = Array.isArray(state.processes) ? state.processes : [];
      const recentEvents = Array.isArray(state.recent_events) ? state.recent_events : [];
      const errors = Array.isArray(state.errors)
        ? state.errors.filter((item) => String(item || "").trim())
        : [];
      const problematic = Array.isArray(state.problematic_ids) ? state.problematic_ids : [];

      const processTotal = Number(state.total_processes || processes.length || 0);
      const healthyCount = Number(state.healthy_count || 0);
      const restartedCount = Number(state.restarted_count || 0);
      const problematicCount = Number(state.problematic_count || problematic.length || 0);
      const runsTotal = Number(state.runs_total || 0);
      const partial = Boolean(state.partial);
      const stateExists = Boolean(state.state_exists);
      const historyExists = Boolean(state.history_exists);
      const errorSuffix = errors.length ? ` · errors=${{errors.length}}` : "";
      const partialSuffix = partial ? " · partial=true" : "";
      const runAt = formatTimestamp(state.last_run_at || "");
      const runAge = formatAgeSeconds(state.last_run_age_sec);
      const stateLabel = stateExists ? "present" : "missing";
      const historyLabel = historyExists ? "present" : "missing";
      const summary = [
        `watchdog_ok=${{yn(Boolean(state.ok))}}`,
        `processes=${{processTotal}}`,
        `healthy=${{healthyCount}}`,
        `restarted=${{restartedCount}}`,
        `problematic=${{problematicCount}}`,
        `runs=${{runsTotal}}`,
        `last_run=${{runAt || "-"}}`,
        `age=${{runAge}}`,
        `state=${{stateLabel}}`,
        `history=${{historyLabel}}`,
      ].join(" · ");

      byId("watchdogSummary").textContent = `${{summary}}${{partialSuffix}}${{errorSuffix}}`;
      byId("watchdogProcessTotal").textContent = String(processTotal);
      byId("watchdogHealthy").textContent = String(healthyCount);
      byId("watchdogRestarted").textContent = String(restartedCount);
      byId("watchdogProblematic").textContent = String(problematicCount);
      byId("watchdogRuns").textContent = String(runsTotal);

      const listRows = [];
      if (errors.length) {{
        for (const error of errors) {{
          listRows.push(`<div class="line bad">error: ${{escapeHtml(String(error || ""))}}</div>`);
        }}
      }}
      if (processes.length) {{
        for (const process of processes) {{
          const item = process || {{}};
          const processId = String(item.id || "unknown");
          const status = String(item.status || "unknown");
          const pid = item.pid ?? "-";
          const checksTotal = Number(item.checks_total || 0);
          const unhealthyChecks = Number(item.unhealthy_checks || 0);
          const restarts = Number(item.restart_attempts || 0);
          const restartSuccesses = Number(item.restart_successes || 0);
          const restartFailures = Number(item.restart_failures || 0);
          const checkedAt = formatTimestamp(item.last_checked_at || "");
          const reason = String(item.reason || item.last_reason || "").trim();
          const statusClass =
            status === "healthy" ? "ok" : (status === "restarted" ? "warn" : "bad");
          listRows.push(
            `<div class="line"><span class="mono">${{escapeHtml(processId)}}</span> status=<span class="${{statusClass}}">${{escapeHtml(status)}}</span> · pid=${{escapeHtml(String(pid))}} · checks=${{checksTotal}} · unhealthy=${{unhealthyChecks}} · restarts=${{restarts}} (ok=${{restartSuccesses}} fail=${{restartFailures}})</div>`
          );
          listRows.push(
            `<div class="line mono">last_checked=${{escapeHtml(checkedAt || "-")}}${{reason ? ` · reason=${{escapeHtml(reason.slice(0, 180))}}` : ""}}</div>`
          );
        }}
      }} else {{
        listRows.push('<div class="line">No watchdog process entries yet.</div>');
      }}
      byId("watchdogList").innerHTML = listRows.join("");

      byId("watchdogEvents").innerHTML = recentEvents.length
        ? recentEvents.map((event) => {{
            const item = event || {{}};
            const eventTime = formatTimestamp(item.time || item.timestamp || "");
            const processId = String(item.id || "unknown");
            const status = String(item.status || "unknown");
            const pid = item.pid ?? "-";
            const reason = String(item.reason || "").trim();
            const restartRc = item.restart_returncode;
            return [
              '<div class="feed-item">',
              `<div class="feed-head"><span>${{escapeHtml(eventTime || "-")}}</span><span>id=${{escapeHtml(processId)}}</span><span>status=${{escapeHtml(status)}}</span><span>pid=${{escapeHtml(String(pid))}}</span><span>restart_rc=${{escapeHtml(String(restartRc ?? "-"))}}</span></div>`,
              reason ? `<div>${{escapeHtml(reason)}}</div>` : '<div>reason: -</div>',
              '</div>',
            ].join("");
          }}).join("")
        : '<div class="feed-item">No watchdog history events yet.</div>';
    }}

    function liveIndicatorMarkup(state, label) {{
      const normalized = String(state || "offline").trim().toLowerCase() || "offline";
      const className = normalized.startsWith("think")
        ? "live-thinking"
        : (normalized === "active" ? "live-active" : (normalized === "stale" ? "live-stale" : "live-offline"));
      const text = String(label || normalized || "offline");
      return `<span class="live-indicator ${{className}}"><span class="live-dot"></span><span>${{escapeHtml(text)}}</span></span>`;
    }}

    function signalLedMarkup(row) {{
      const level = Number(row && row.signal_level || 0);
      const liveState = String((row && row.live_state) || "offline").trim().toLowerCase() || "offline";
      const totalLeds = 12;
      const activeLeds = Math.max(0, Math.min(totalLeds, Math.round(level * totalLeds)));
      const liveClass = liveState.startsWith("think")
        ? "live"
        : (liveState === "active" ? "live" : (liveState === "stale" ? "stale" : "offline"));
      const rendered = [];
      for (let idx = 0; idx < totalLeds; idx += 1) {{
        const on = idx < activeLeds;
        rendered.push(`<span class="led ${{on ? `on ${{liveClass}}` : ''}}"></span>`);
      }}
      return `<span class="led-strip">${{rendered.join('')}}</span>`;
    }}

    function sparklineMarkup(values, tone, liveState) {{
      const series = Array.isArray(values)
        ? values.map((value) => {{
            const numeric = Number(value);
            return Number.isFinite(numeric) && numeric > 0 ? numeric : 0;
          }})
        : [];
      const toneClass = String(tone || "signal").trim().toLowerCase() || "signal";
      if (!series.length) {{
        return `<span class="sparkline-strip sparkline-${{toneClass}}"></span>`;
      }}
      let maxValue = 0;
      for (const value of series) {{
        if (value > maxValue) maxValue = value;
      }}
      if (maxValue <= 0) maxValue = 1;
      const state = String(liveState || "active").trim().toLowerCase() || "active";
      const stateClass = state === "stale" ? "stale" : (state === "offline" ? "offline" : "live");
      const bars = series.map((value) => {{
        const active = value > 0;
        const heightPct = active
          ? Math.max(16, Math.min(100, Math.round((value / maxValue) * 100)))
          : 12;
        return `<span class="sparkline-bar ${{active ? 'active' : ''}}" style="--h:${{heightPct}}%;"></span>`;
      }});
      return `<span class="sparkline-strip sparkline-${{toneClass}} ${{stateClass}}">${{bars.join('')}}</span>`;
    }}

    function attentionBadgeMarkup(level, score) {{
      const normalized = String(level || "ok").trim().toLowerCase();
      const resolved = ["critical", "warn", "watch", "ok"].includes(normalized) ? normalized : "ok";
      const numericScore = Number(score);
      const scoreLabel = Number.isFinite(numericScore) ? ` ${{Math.max(0, Math.round(numericScore))}}` : "";
      return `<span class="attention-badge attention-${{resolved}}">${{escapeHtml(resolved + scoreLabel)}}</span>`;
    }}

    function renderCollaboratorRuntime(payload) {{
      const data = (payload && typeof payload === "object") ? payload : {{}};
      const rows = Array.isArray(data.rows) ? data.rows : [];
      const errors = Array.isArray(data.errors)
        ? data.errors.filter((item) => String(item || "").trim())
        : [];
      const summary = (data.summary && typeof data.summary === "object") ? data.summary : {{}};
      const totalRows = Number(summary.total_rows || rows.length || 0);
      const runningRows = Number(summary.running_rows || 0);
      const thinkingRows = Number(summary.thinking_rows || 0);
      const activeRows = Number(summary.active_rows || 0);
      const staleRows = Number(summary.stale_rows || 0);
      const offlineRows = Number(summary.offline_rows || 0);
      const commits1h = Number(summary.commits_last_hour_total || 0);
      const attentionRows = Number(summary.attention_rows || 0);
      const criticalRows = Number(summary.critical_rows || 0);
      const warnRows = Number(summary.warn_rows || 0);
      const watchRows = Number(summary.watch_rows || 0);
      const partial = Boolean(data.partial);
      const baseSummary =
        `agents=${{totalRows}} · running=${{runningRows}} · thinking=${{thinkingRows}} · active=${{activeRows}} · stale=${{staleRows}} · offline=${{offlineRows}} · commits_1h=${{commits1h}} · attention=${{attentionRows}} (critical=${{criticalRows}} warn=${{warnRows}} watch=${{watchRows}})`;
      const errorSuffix = errors.length ? ` · errors=${{errors.length}}` : "";
      byId("collabSummary").textContent = `${{baseSummary}}${{partial ? " · partial=true" : ""}}${{errorSuffix}}`;
      byId("collabActivity").textContent =
        `latest_signal: ${{formatTimestamp(summary.latest_signal_at || "") || "-"}} · age=${{formatAgeSeconds(summary.latest_signal_age_sec)}} · latest_health: ${{formatTimestamp(summary.latest_health_at || "") || "-"}} · task_done: ${{formatTimestamp(summary.latest_task_done_at || "") || "-"}} · push: ${{formatTimestamp(summary.latest_push_at || "") || "-"}} · latest_commit: ${{formatTimestamp(summary.latest_commit_at || "") || "-"}}`;
      const topAttention = Array.isArray(summary.top_attention)
        ? summary.top_attention.filter((item) => String(item || "").trim())
        : [];
      const anomalyText = topAttention.length
        ? `attention: ${{topAttention.join(" | ")}}`
        : (attentionRows > 0 ? "attention: check lane-level details" : "attention: stable");
      const anomalyNode = byId("collabAnomaly");
      anomalyNode.textContent = anomalyText;
      anomalyNode.className = attentionRows > 0
        ? (criticalRows > 0 ? "mono bad" : "mono warn")
        : "mono ok";

      if (!rows.length) {{
        byId("collabTableBody").innerHTML = `<tr><td colspan="10" class="mono bad">${{escapeHtml(errors[0] || "No collaborative runtime rows available.")}}</td></tr>`;
        return;
      }}

      const body = [];
      for (const row of rows) {{
        const item = row || {{}};
        const ai = String(item.ai || item.owner || "unknown");
        const laneId = String(item.lane_id || "").trim();
        const workTitle = String(item.work_title || "unknown");
        const pid = item.pid ?? "-";
        const running = Boolean(item.running);
        const runningFor = formatDuration(item.running_age_sec);
        const healthLabel = String(item.health || "unknown");
        const healthAt = formatTimestamp(item.latest_health_confirmation_at || "");
        const healthAge = formatAgeSeconds(item.latest_health_confirmation_age_sec);
        const commits = Number(item.commits_last_hour);
        const commitLabel = Number.isFinite(commits) && commits >= 0 ? String(commits) : "-";
        const commitBars = sparklineMarkup(item.commit_bins_5m, "commit", item.live_state);
        const latestCommitAge = formatAgeSeconds(item.latest_commit_age_sec);
        const taskDoneAt = formatTimestamp(item.latest_task_done_at || "");
        const taskDoneAge = formatAgeSeconds(item.latest_task_done_age_sec);
        const pushAt = formatTimestamp(item.latest_push_at || "");
        const pushAge = formatAgeSeconds(item.latest_push_age_sec);
        const attentionBadge = attentionBadgeMarkup(item.attention_level, item.attention_score);
        const attentionMessage = String(item.attention_message || "stable");
        const liveHtml = liveIndicatorMarkup(item.live_state, item.live_label || item.live_state || "offline");
        const signalHtml = signalLedMarkup(item);
        const signalBars = sparklineMarkup(item.signal_bins_5m, "signal", item.live_state);
        const signalAge = formatAgeSeconds(item.latest_signal_age_sec);
        const aiCell = laneId
          ? `<div class="mono">${{escapeHtml(ai)}}</div><div class="mono">${{escapeHtml(laneId)}}</div>`
          : `<div class="mono">${{escapeHtml(ai)}}</div>`;
        const healthCell = `<div class="cell-stack"><div>${{escapeHtml(healthLabel)}}</div><div class="mono">${{escapeHtml(healthAt || "-")}} · age=${{escapeHtml(healthAge)}}</div></div>`;
        const commitsCell = `<div class="cell-stack"><div class="mono">${{escapeHtml(commitLabel)}}</div><div>${{commitBars}}</div><div class="mono">latest=${{escapeHtml(latestCommitAge)}}</div></div>`;
        const successCell = `<div class="cell-stack"><div class="mono">task_done=${{escapeHtml(taskDoneAt || "-")}} · age=${{escapeHtml(taskDoneAge)}}</div><div class="mono">push=${{escapeHtml(pushAt || "-")}} · age=${{escapeHtml(pushAge)}}</div></div>`;
        const signalCell = `<div class="cell-stack"><div>${{signalHtml}}</div><div>${{signalBars}}</div><div class="mono">age=${{escapeHtml(signalAge)}}</div></div>`;
        const attentionCell = `<div class="cell-stack"><div>${{attentionBadge}}</div><div class="mono">${{escapeHtml(attentionMessage)}}</div></div>`;
        body.push(
          `<tr>
            <td>${{aiCell}}</td>
            <td>${{escapeHtml(workTitle)}}</td>
            <td class="mono">${{escapeHtml(String(pid))}}</td>
            <td class="mono">${{running ? escapeHtml(runningFor) : "-"}}</td>
            <td>${{healthCell}}</td>
            <td>${{commitsCell}}</td>
            <td>${{successCell}}</td>
            <td>${{liveHtml}}</td>
            <td>${{signalCell}}</td>
            <td>${{attentionCell}}</td>
          </tr>`
        );
      }}
      if (errors.length) {{
        body.push(`<tr><td colspan="10" class="mono bad">${{escapeHtml(errors.join(" | "))}}</td></tr>`);
      }}
      byId("collabTableBody").innerHTML = body.join("");
    }}

    function buildConversationSourceMap(payload) {{
      const sourceReports = payload && Array.isArray(payload.sources) ? payload.sources : [];
      const byLane = {{}};
      for (const source of sourceReports) {{
        const item = source || {{}};
        const laneId = String(item.lane_id || "").trim();
        if (!laneId) continue;
        if (!byLane[laneId]) {{
          byLane[laneId] = {{
            ok: true,
            event_count: 0,
            fallback_used: false,
            missing: false,
            recoverable_missing: false,
            errors: [],
          }};
        }}
        const current = byLane[laneId];
        current.ok = current.ok && Boolean(item.ok);
        current.event_count += Number(item.event_count || 0);
        current.fallback_used = current.fallback_used || Boolean(item.fallback_used);
        current.missing = current.missing || Boolean(item.missing);
        current.recoverable_missing = current.recoverable_missing || Boolean(item.recoverable_missing);
        const message = String(item.error || "").trim();
        if (message) current.errors.push(message);
      }}
      return byLane;
    }}

    function eventTimestampInfo(raw) {{
      const text = String(raw || "").trim();
      if (!text) {{
        return {{ valid: false, epochMs: Number.NEGATIVE_INFINITY }};
      }}
      const parsed = new Date(text);
      if (!Number.isFinite(parsed.getTime())) {{
        return {{ valid: false, epochMs: Number.NEGATIVE_INFINITY }};
      }}
      return {{ valid: true, epochMs: parsed.getTime() }};
    }}

    function buildLatestConversationByLane(payload) {{
      const events = payload && Array.isArray(payload.events) ? payload.events : [];
      const byLane = {{}};
      for (const entry of events) {{
        const event = entry || {{}};
        const laneId = String(event.lane_id || "").trim();
        if (!laneId) continue;
        const existing = byLane[laneId];
        if (!existing) {{
          byLane[laneId] = event;
          continue;
        }}
        const candidateTs = eventTimestampInfo(event.timestamp);
        const existingTs = eventTimestampInfo(existing.timestamp);
        if (!candidateTs.valid && !existingTs.valid) {{
          // Preserve sequence when both timestamps are invalid.
          byLane[laneId] = event;
          continue;
        }}
        if (!candidateTs.valid) {{
          continue;
        }}
        if (!existingTs.valid || candidateTs.epochMs >= existingTs.epochMs) {{
          byLane[laneId] = event;
        }}
      }}
      return byLane;
    }}

    function renderLanes(lanes, runtime, conversations) {{
      const lanePayload = lanes || {{}};
      const laneItems = lanePayload.lanes || [];
      const laneErrors = Array.isArray(lanePayload.errors)
        ? lanePayload.errors.filter((item) => String(item || "").trim())
        : [];
      const laneSourceMap = buildConversationSourceMap(conversations || {{}});
      const latestConversationByLane = buildLatestConversationByLane(conversations || {{}});
      const laneSourceErrorCount = laneItems.filter((lane) => {{
        const laneId = String((lane && lane.id) || "").trim();
        const source = laneSourceMap[laneId] || null;
        if (source) {{
          return !source.ok;
        }}
        return lane && lane.conversation_source_ok === false;
      }}).length;
      const recoveredLanes = Number(lanePayload.recovered_lane_count || 0);
      const runningLanes = Number(lanePayload.running_count || 0);
      const totalLanes = Number(lanePayload.total_count || 0);
      const laneHealthCounts = lanePayload.health_counts || {{}};
      const runtimePayload = runtime || {{}};
      const operationalLanes = Number(runtimePayload.lane_operational_count ?? laneItems.filter((lane) => {{
        const h = String(lane.health || "unknown").toLowerCase();
        return h === "ok" || h === "paused" || h === "idle";
      }}).length);
      const degradedLanes = Number(runtimePayload.lane_degraded_count ?? Math.max(totalLanes - operationalLanes, 0));
      const healthSummary = Object.entries(laneHealthCounts)
        .map(([name, count]) => `${{name}}=${{Number(count || 0)}}`)
        .join(", ");
      const derivedOwnerSummary = {{}};
      for (const lane of laneItems) {{
        const owner = String((lane && lane.owner) || "unknown").trim() || "unknown";
        if (!derivedOwnerSummary[owner]) {{
          derivedOwnerSummary[owner] = {{ total: 0, running: 0, healthy: 0, degraded: 0 }};
        }}
        const health = String((lane && lane.health) || "unknown").toLowerCase();
        derivedOwnerSummary[owner].total += 1;
        if (lane && lane.running) derivedOwnerSummary[owner].running += 1;
        if (health === "ok" || health === "paused" || health === "idle") {{
          derivedOwnerSummary[owner].healthy += 1;
        }} else {{
          derivedOwnerSummary[owner].degraded += 1;
        }}
      }}
      const ownerPayload = (lanePayload.owner_counts && Object.keys(lanePayload.owner_counts).length)
        ? lanePayload.owner_counts
        : derivedOwnerSummary;
      const ownerSummary = Object.entries(ownerPayload)
        .map(([owner, stats]) => {{
          const item = stats || {{}};
          return `${{owner}} t=${{Number(item.total || 0)}} r=${{Number(item.running || 0)}} h=${{Number(item.healthy || 0)}} d=${{Number(item.degraded || 0)}}`;
        }})
        .join(" · ");
      byId("laneSummary").textContent =
        `running lanes: ${{runningLanes}}/${{totalLanes}} · operational: ${{operationalLanes}} · degraded: ${{degradedLanes}} · health: ${{healthSummary || "none"}} · source_errors: ${{laneErrors.length}} · conversation_source_errors: ${{laneSourceErrorCount}} · recovered: ${{recoveredLanes}}`;
      byId("laneOwnerSummary").textContent = `owners: ${{ownerSummary || "none"}}`;
      const laneErrorMarkup = laneErrors.length
        ? laneErrors.map((item) => `<div class="line bad">source_error: ${{escapeHtml(String(item || ""))}}</div>`).join("")
        : "";
      byId("laneList").innerHTML = `${{laneErrorMarkup}}${{laneItems.length
        ? laneItems.map((lane) => {{
            const state = lane.running ? "running" : "stopped";
            const health = lane.health || "unknown";
            const age = lane.heartbeat_age_sec ?? -1;
            const counts = lane.state_counts || {{}};
            const done = Number(counts.done || 0);
            const inProgress = Number(counts.in_progress || 0);
            const pending = Number(counts.pending || 0);
            const blocked = Number(counts.blocked || 0);
            const buildCurrent = lane.build_current ? "current" : "stale";
            const lastEvent = (lane.last_event && lane.last_event.event_type) ? ` · event=${{lane.last_event.event_type}}` : "";
            const error = lane.error ? `<div class="line bad">error: ${{escapeHtml(lane.error)}}</div>` : "";
            const laneId = String(lane.id || "").trim();
            const source = laneSourceMap[laneId] || null;
            const sourceFlags = [];
            let sourceState = "unreported";
            let sourceEvents = Number(lane.conversation_event_count || 0);
            if (source) {{
              sourceState = source.ok ? "ok" : "error";
              sourceEvents = Number(source.event_count || 0);
              if (source.fallback_used) sourceFlags.push("fallback");
              if (source.missing && source.recoverable_missing) {{
                sourceFlags.push("recoverable_missing");
              }} else if (source.missing) {{
                sourceFlags.push("missing");
              }}
              if ((source.errors || []).length) sourceFlags.push(`errors=${{(source.errors || []).length}}`);
            }} else {{
              if (lane.conversation_source_ok === true) {{
                sourceState = "ok";
              }} else if (lane.conversation_source_ok === false) {{
                sourceState = "error";
              }}
              const fallbackCount = Number(lane.conversation_source_fallback_count || 0);
              const missingCount = Number(lane.conversation_source_missing_count || 0);
              const recoverableMissingCount = Number(lane.conversation_source_recoverable_missing_count || 0);
              const sourceErrorCount = Number(lane.conversation_source_error_count || 0);
              if (fallbackCount > 0) sourceFlags.push("fallback");
              if (recoverableMissingCount > 0) {{
                sourceFlags.push("recoverable_missing");
              }} else if (missingCount > 0) {{
                sourceFlags.push("missing");
              }}
              if (sourceErrorCount > 0) sourceFlags.push(`errors=${{sourceErrorCount}}`);
            }}
            const sourceExtras = sourceFlags.length ? ` (${{sourceFlags.join(",")}})` : "";
            const conversationSourceLine =
              `<div class="line mono">conversation_source=${{sourceState}} events=${{sourceEvents}}${{sourceExtras}}</div>`;
            const latestConversation = (lane && lane.latest_conversation_event)
              ? lane.latest_conversation_event
              : (latestConversationByLane[laneId] || null);
            const latestConversationLine = latestConversation
              ? `<div class="line mono">latest_conversation=${{escapeHtml(formatTimestamp(String(latestConversation.timestamp || "")))}} owner=${{escapeHtml(String(latestConversation.owner || "unknown"))}} type=${{escapeHtml(String(latestConversation.event_type || "-"))}}${{String(latestConversation.content || "").trim() ? ` content=${{escapeHtml(String(latestConversation.content).trim().slice(0, 120))}}` : ""}}</div>`
              : `<div class="line mono">latest_conversation=none</div>`;
            return [
              `<div class="line"><span class="mono">${{escapeHtml(lane.id)}}</span> [${{escapeHtml(lane.owner)}}] ${{state}} · health=${{health}} · hb=${{age}}s · build=${{buildCurrent}}</div>`,
              `<div class="line mono">tasks d=${{done}} p=${{pending}} w=${{inProgress}} b=${{blocked}}${{lastEvent}}</div>`,
              conversationSourceLine,
              latestConversationLine,
              error,
            ].join("");
          }}).join("")
        : '<div class="line">No lanes configured.</div>'}}`;
      return {{ runningLanes, totalLanes }};
    }}

    function render(snapshot, laneOverride, conversationOverride) {{
      const status = snapshot.status || {{}};
      const runtime = snapshot.runtime || {{}};
      const lanes = laneOverride || snapshot.lanes || {{}};
      const conversations = conversationOverride || snapshot.conversations || {{}};
      const laneStats = renderLanes(lanes, runtime, conversations);
      const runningLanes = laneStats.runningLanes;
      const totalLanes = laneStats.totalLanes;
      const progress = snapshot.progress || {{}};
      const counts = progress.counts || {{}};
      const diagnostics = snapshot.diagnostics || {{}};
      const responseMetrics = snapshot.response_metrics || {{}};
      const done = counts.done || 0;
      const inProgress = counts.in_progress || 0;
      const pending = counts.pending || 0;
      const blocked = counts.blocked || 0;
      const unknown = counts.unknown || 0;
      const completed24h = Number(progress.completed_last_24h || 0);
      const completed24hUnique = Number(progress.completed_last_24h_unique_tasks || 0);
      const completed24hByOwner = (progress.completed_last_24h_by_owner && typeof progress.completed_last_24h_by_owner === "object")
        ? progress.completed_last_24h_by_owner
        : {{}};
      const completed24hOwnerSummary = Object.entries(completed24hByOwner)
        .map(([owner, value]) => `${{owner}}=${{Number(value || 0)}}`)
        .join(", ");
      const total = done + inProgress + pending + blocked + unknown;

      byId("done").textContent = done;
      byId("in_progress").textContent = inProgress;
      byId("pending").textContent = pending;
      byId("blocked").textContent = blocked;
      byId("unknown").textContent = unknown;
      byId("completed_24h").textContent = completed24h;
      const progressSource = String(progress.source || "primary_state");
      byId("activeTasks").textContent =
        `active_tasks: ${{(progress.active_tasks || []).join(", ") || "none"}} · blocked: ${{(progress.blocked_tasks || []).join(", ") || "none"}} · source=${{progressSource}}`;
      byId("completed24hSummary").textContent =
        `completed_last_24h: ${{completed24h}} · unique_tasks: ${{completed24hUnique}}${{completed24hOwnerSummary ? ` · by_owner: ${{completed24hOwnerSummary}}` : ""}}`;

      byId("taskBar").style.setProperty("--done", pct(done, total) + "%");
      byId("taskBar").style.setProperty("--in_progress", pct(inProgress, total) + "%");
      byId("taskBar").style.setProperty("--pending", pct(pending, total) + "%");
      byId("taskBar").style.setProperty("--blocked", pct(blocked, total) + "%");

      const runnerState = status.runner_running ? '<span class="ok">running</span>' : '<span class="bad">stopped</span>';
      const supervisorState = status.supervisor_running ? '<span class="ok">running</span>' : '<span class="bad">stopped</span>';
      const laneAgentState = runningLanes > 0
        ? '<span class="ok">active</span>'
        : (totalLanes > 0 ? '<span class="warn">idle</span>' : '<span class="mono">n/a</span>');
      const fabricState = runtime.effective_agents_running
        ? '<span class="ok">active</span>'
        : '<span class="bad">stopped</span>';
      const effectiveRunnerState = (!status.runner_running && runningLanes > 0)
        ? '<span class="warn">idle (lane mode)</span>'
        : runnerState;
      byId("runtimeState").innerHTML =
        `supervisor: ${{supervisorState}} · runner: ${{effectiveRunnerState}} · lane_agents: ${{laneAgentState}} · fabric: ${{fabricState}}`;
      const hbNote = (!status.runner_running && runningLanes > 0)
        ? ' · note: lane runners active'
        : '';
      byId("heartbeatState").innerHTML =
        `heartbeat_age: <span class="mono">${{status.heartbeat_age_sec ?? -1}}s</span> · stale_threshold: <span class="mono">${{status.heartbeat_stale_threshold_sec ?? -1}}s</span>${{hbNote}}`;

      const responseCount = Number(responseMetrics.responses_total || 0);
      const firstPassRate = Number(responseMetrics.first_time_pass_rate || 0);
      const acceptanceRate = Number(responseMetrics.acceptance_pass_rate || 0);
      const latencyAvg = Number(responseMetrics.latency_sec_avg || 0);
      const difficultyAvg = Number(responseMetrics.prompt_difficulty_score_avg || 0);
      const costTotal = Number(responseMetrics.cost_usd_total || 0);
      const costCoverage = Number(responseMetrics.exact_cost_coverage || 0);
      const estimatedTokens = Number(responseMetrics.estimated_tokens_total || responseMetrics.tokens_total || 0);
      let blendedCostPerMillion = Number(responseMetrics.estimated_cost_per_million_tokens || 0);
      if ((!Number.isFinite(blendedCostPerMillion) || blendedCostPerMillion <= 0) && costTotal > 0 && estimatedTokens > 0) {{
        blendedCostPerMillion = (costTotal * 1000000) / estimatedTokens;
      }}
      if (!Number.isFinite(blendedCostPerMillion) || blendedCostPerMillion < 0) blendedCostPerMillion = 0;
      const excitingStat = responseMetrics.exciting_stat || {{}};
      byId("metricsSummary").textContent =
        `responses: ${{responseCount}} · first-pass: ${{Math.round(firstPassRate * 100)}}% · acceptance: ${{Math.round(acceptanceRate * 100)}}% · avg latency: ${{latencyAvg.toFixed(2)}}s · avg difficulty: ${{difficultyAvg.toFixed(1)}} · total cost: $${{costTotal.toFixed(4)}} · exact cost: ${{Math.round(costCoverage * 100)}}%`;
      byId("metricsEconomics").textContent =
        `estimated tokens used: ${{Math.max(0, Math.round(estimatedTokens)).toLocaleString()}} · blended cost / 1M tokens: $${{blendedCostPerMillion.toFixed(3)}}`;
      byId("excitingStat").textContent =
        `Most exciting stat: ${{excitingStat.label || 'Awaiting Data'}} -> ${{excitingStat.value || '0'}}${{excitingStat.detail ? ' · ' + excitingStat.detail : ''}}`;
      const ownerRows = Object.entries(responseMetrics.by_owner || {{}}).map(([owner, payload]) => {{
        const item = payload || {{}};
        const ownerResponses = Number(item.responses || 0);
        const ownerCost = Number(item.cost_usd_total || 0);
        const ownerTokens = Number(item.tokens_total || 0);
        let ownerCostPerMillion = Number(item.cost_per_million_tokens || 0);
        if ((!Number.isFinite(ownerCostPerMillion) || ownerCostPerMillion <= 0) && ownerCost > 0 && ownerTokens > 0) {{
          ownerCostPerMillion = (ownerCost * 1000000) / ownerTokens;
        }}
        if (!Number.isFinite(ownerCostPerMillion) || ownerCostPerMillion < 0) ownerCostPerMillion = 0;
        const ownerFirstPass = Number(item.first_time_pass_rate || 0);
        const ownerValidation = Number(item.validation_pass_rate || 0);
        return `<div class="line"><span class="mono">${{escapeHtml(owner)}}</span> responses=${{ownerResponses}} · first-pass=${{Math.round(ownerFirstPass * 100)}}% · validation=${{Math.round(ownerValidation * 100)}}% · tokens=${{ownerTokens}} · cost=$${{ownerCost.toFixed(4)}} · cost/1M=$${{ownerCostPerMillion.toFixed(3)}}</div>`;
      }});
      const recommendations = Array.isArray(responseMetrics.optimization_recommendations)
        ? responseMetrics.optimization_recommendations
        : [];
      if (!ownerRows.length) {{
        ownerRows.push('<div class="line">No response metrics yet.</div>');
      }}
      for (const recommendation of recommendations.slice(0, 3)) {{
        ownerRows.push(`<div class="line warn">${{escapeHtml(recommendation)}}</div>`);
      }}
      byId("metricsList").innerHTML = ownerRows.join("");

      byId("repoImpl").innerHTML = repoMarkup((snapshot.repos || {{}}).implementation);
      byId("repoTest").innerHTML = repoMarkup((snapshot.repos || {{}}).tests);
      byId("latestLog").textContent = snapshot.latest_log_line || "(no log line yet)";
      byId("updated").textContent = `updated: ${{formatNowTimestamp()}} (${{USER_TIMEZONE}})`;

      byId("meta").innerHTML = [
        `<span class="pill">runner pid: ${{status.runner_pid ?? "-"}}</span>`,
        `<span class="pill">supervisor pid: ${{status.supervisor_pid ?? "-"}}</span>`,
        `<span class="pill mono">monitor file: ${{snapshot.monitor_file || "-"}}</span>`,
      ].join("");

      renderDiagnostics(diagnostics, {{}});
    }}

    function renderDiagnostics(payload, endpointErrors) {{
      const sources = (payload && payload.sources) || {{}};
      const errors = (payload && payload.errors) || [];
      const endpointIssues = Object.entries(endpointErrors || {{}})
        .filter((entry) => Boolean(entry[1]))
        .map((entry) => `${{entry[0]}}: ${{entry[1]}}`);
      const sourceRows = Object.entries(sources);
      const failedSourceCount = sourceRows.filter((entry) => !(entry[1] || {{}}).ok).length + endpointIssues.length;
      const totalSourceCount = sourceRows.length + endpointIssues.length;
      const healthySourceCount = Math.max(totalSourceCount - failedSourceCount, 0);
      byId("resilienceSummary").textContent =
        `sources: ${{healthySourceCount}}/${{totalSourceCount || 0}} healthy · errors: ${{errors.length + endpointIssues.length}}`;

      const rows = [];
      for (const [name, item] of sourceRows) {{
        const source = item || {{}};
        const message = source.error ? escapeHtml(source.error) : "";
        rows.push(
          `<div class="diag-item"><span class="diag-name mono">${{escapeHtml(name)}}</span><span>${{stateBadge(Boolean(source.ok))}}</span><span class="mono">${{message}}</span></div>`
        );
      }}
      for (const issue of endpointIssues) {{
        rows.push(
          `<div class="diag-item"><span class="diag-name mono">dashboard_api</span><span>${{stateBadge(false)}}</span><span class="mono">${{escapeHtml(issue)}}</span></div>`
        );
      }}
      if (!rows.length) {{
        rows.push('<div class="diag-item">No diagnostics available yet.</div>');
      }}
      byId("resilienceList").innerHTML = rows.join('');
    }}

    function renderConversations(payload) {{
      const events = payload.events || [];
      const ownerCounts = payload.owner_counts || {{}};
      const sourceReports = payload.sources || [];
      const suppressedSourceErrors = Number(payload.suppressed_source_error_count || 0);
      const filters = payload.filters || {{}};
      const failedSources = sourceReports.filter((source) => !source.ok).length;
      const healthySources = Math.max(sourceReports.length - failedSources, 0);
      const partial = payload.partial ? " · partial=true" : "";
      const errors = (payload.errors || []).length ? ` · errors=${{(payload.errors || []).length}}` : "";
      const filterParts = [];
      if (filters.owner) filterParts.push(`owner=${{filters.owner}}`);
      if (filters.lane) filterParts.push(`lane=${{filters.lane}}`);
      if (filters.event_type) filterParts.push(`type=${{filters.event_type}}`);
      if (filters.contains) filterParts.push(`contains=${{filters.contains}}`);
      if (Number(filters.tail || 0) > 0) filterParts.push(`tail=${{filters.tail}}`);
      const filterLabel = filterParts.length ? ` · filters: ${{filterParts.join(", ")}}` : "";
      const suppressedLabel = suppressedSourceErrors > 0
        ? ` · suppressed_source_errors=${{suppressedSourceErrors}}`
        : "";
      const sourceSummary = sourceReports.length
        ? sourceReports.map((source) => {{
            const item = source || {{}};
            const label = String(item.lane_id || item.owner || item.kind || "source");
            const state = item.ok ? "ok" : "error";
            const flags = [];
            if (item.fallback_used) flags.push("fallback");
            if (item.recoverable_missing) flags.push("recoverable_missing");
            if (item.missing && !item.recoverable_missing) flags.push("missing");
            const eventCount = Number(item.event_count || 0);
            const extras = flags.length ? ` (${{flags.join(",")}})` : "";
            return `${{label}}:${{state}}#${{eventCount}}${{extras}}`;
          }}).join(" | ")
        : "none reported";
      byId("conversationSummary").textContent =
        `events: ${{payload.total_events ?? 0}} · owners: ${{JSON.stringify(ownerCounts)}} · sources: ${{healthySources}}/${{sourceReports.length}} healthy${{partial}}${{errors}}${{filterLabel}}${{suppressedLabel}}`;
      byId("conversationSources").textContent = `source health: ${{sourceSummary}}`;
      if (!events.length) {{
        byId("conversationFeed").innerHTML = '<div class="feed-item">No conversation events yet.</div>';
        return;
      }}
      const rows = events.slice(-80).map((event) => {{
        const ts = event.timestamp || '';
        const owner = event.owner || 'unknown';
        const laneId = event.lane_id || '-';
        const task = event.task_id || '-';
        const type = event.event_type || '-';
        const source = event.source || '';
        const content = escapeHtml(event.content || '');
        return [
          '<div class="feed-item">',
          `<div class="feed-head"><span>${{escapeHtml(formatTimestamp(ts))}}</span><span>owner=${{escapeHtml(owner)}}</span><span>lane=${{escapeHtml(laneId)}}</span><span>task=${{escapeHtml(task)}}</span><span>type=${{escapeHtml(type)}}</span></div>`,
          source ? `<div class="mono">source=${{escapeHtml(source)}}</div>` : '',
          `<div>${{content}}</div>`,
          '</div>',
        ].join('');
      }});
      byId("conversationFeed").innerHTML = rows.join('');
    }}

    function toBool(value) {{
      if (typeof value === "boolean") return value;
      if (typeof value === "number") return Number.isFinite(value) && value !== 0;
      const normalized = String(value || "").trim().toLowerCase();
      if (!normalized) return false;
      return normalized === "1" || normalized === "true" || normalized === "yes" || normalized === "on";
    }}

    function parseJsonObject(rawValue) {{
      const raw = String(rawValue || "").trim();
      if (!raw || raw[0] !== "{{") return {{}};
      try {{
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {{
          return parsed;
        }}
      }} catch (_err) {{
        return {{}};
      }}
      return {{}};
    }}

    function normalizeRoutingDecisionEvent(entry) {{
      const event = entry && typeof entry === "object" ? entry : {{}};
      const payload = (event.payload && typeof event.payload === "object")
        ? event.payload
        : parseJsonObject(event.content || "");
      const meta = (event.meta && typeof event.meta === "object") ? event.meta : {{}};
      const owner = String(event.owner || "unknown").trim() || "unknown";
      const laneId = String(event.lane_id || "").trim();
      const provider = String(payload.provider || meta.provider || owner).trim() || owner;
      const strategy = String(payload.strategy || "static_fallback").trim() || "static_fallback";
      const requestedModel = String(payload.requested_model || "").trim();
      const selectedModel = String(payload.selected_model || "").trim();
      const reason = String(payload.reason || "").trim();
      const routerError = String(payload.router_error || "").trim();
      const routerUrl = String(payload.router_url || "").trim();
      const routerEnabled = toBool(payload.router_enabled);
      const fallbackUsed = toBool(payload.fallback_used);
      const latencyRaw = Number(payload.router_latency_sec || 0);
      const routerLatencySec = (Number.isFinite(latencyRaw) && latencyRaw >= 0) ? latencyRaw : 0;
      return {{
        timestamp: String(event.timestamp || "").trim(),
        lane_id: laneId,
        owner,
        provider,
        strategy,
        requested_model: requestedModel,
        selected_model: selectedModel,
        reason,
        router_error: routerError,
        router_url: routerUrl,
        router_enabled: routerEnabled,
        fallback_used: fallbackUsed,
        router_latency_sec: routerLatencySec,
      }};
    }}

    function buildRoutingProviderMapFromEvents(decisions) {{
      const out = {{}};
      for (const decision of decisions) {{
        const provider = String((decision && decision.provider) || "unknown").trim() || "unknown";
        const current = out[provider] || {{
          responses: 0,
          routellm_count: 0,
          fallback_count: 0,
          router_error_count: 0,
          routellm_rate: 0,
          fallback_rate: 0,
          router_error_rate: 0,
        }};
        current.responses += 1;
        if (String(decision.strategy || "").trim().toLowerCase() === "routellm") current.routellm_count += 1;
        if (toBool(decision.fallback_used)) current.fallback_count += 1;
        if (String(decision.router_error || "").trim()) current.router_error_count += 1;
        out[provider] = current;
      }}
      for (const provider of Object.keys(out)) {{
        const responses = Math.max(1, Number(out[provider].responses || 0));
        out[provider].routellm_rate = Number(out[provider].routellm_count || 0) / responses;
        out[provider].fallback_rate = Number(out[provider].fallback_count || 0) / responses;
        out[provider].router_error_rate = Number(out[provider].router_error_count || 0) / responses;
      }}
      return out;
    }}

    function buildRoutingLaneStats(decisions) {{
      const out = {{}};
      for (const decision of decisions) {{
        const laneId = String((decision && decision.lane_id) || "unscoped").trim() || "unscoped";
        const current = out[laneId] || {{
          lane_id: laneId,
          owner: String((decision && decision.owner) || "unknown").trim() || "unknown",
          decisions: 0,
          routellm_count: 0,
          fallback_count: 0,
          router_error_count: 0,
          router_latency_sum: 0,
          router_latency_avg: 0,
          latest: null,
          latest_epoch_ms: Number.NEGATIVE_INFINITY,
          latest_valid_ts: false,
        }};
        current.decisions += 1;
        if (String(decision.strategy || "").trim().toLowerCase() === "routellm") current.routellm_count += 1;
        if (toBool(decision.fallback_used)) current.fallback_count += 1;
        if (String(decision.router_error || "").trim()) current.router_error_count += 1;
        current.router_latency_sum += Number(decision.router_latency_sec || 0);
        const tsInfo = eventTimestampInfo(decision.timestamp || "");
        if (!current.latest) {{
          current.latest = decision;
          current.latest_epoch_ms = tsInfo.epochMs;
          current.latest_valid_ts = tsInfo.valid;
        }} else if (tsInfo.valid && (!current.latest_valid_ts || tsInfo.epochMs >= current.latest_epoch_ms)) {{
          current.latest = decision;
          current.latest_epoch_ms = tsInfo.epochMs;
          current.latest_valid_ts = true;
        }} else if (!tsInfo.valid && !current.latest_valid_ts) {{
          current.latest = decision;
        }}
        out[laneId] = current;
      }}
      for (const laneId of Object.keys(out)) {{
        const item = out[laneId];
        const decisionsTotal = Math.max(1, Number(item.decisions || 0));
        item.router_latency_avg = Number(item.router_latency_sum || 0) / decisionsTotal;
      }}
      return out;
    }}

    function renderRouting(snapshot, lanePayload, routingPayload) {{
      const safeSnapshot = (snapshot && typeof snapshot === "object") ? snapshot : {{}};
      const responseMetrics = (safeSnapshot.response_metrics && typeof safeSnapshot.response_metrics === "object")
        ? safeSnapshot.response_metrics
        : {{}};
      const safeLanePayload = (lanePayload && typeof lanePayload === "object") ? lanePayload : {{}};
      const laneItemsRaw = Array.isArray(safeLanePayload.lanes) ? safeLanePayload.lanes : [];
      const laneItems = laneItemsRaw.filter((item) => item && typeof item === "object");
      const safeRoutingPayload = (routingPayload && typeof routingPayload === "object") ? routingPayload : {{}};
      const routingEventsRaw = Array.isArray(safeRoutingPayload.events) ? safeRoutingPayload.events : [];
      const decisions = routingEventsRaw
        .filter((item) => item && typeof item === "object")
        .map((item) => normalizeRoutingDecisionEvent(item));
      const decisionCountDerived = decisions.length;
      const routedCountDerived = decisions.filter((decision) => String(decision.strategy).toLowerCase() === "routellm").length;
      const fallbackCountDerived = decisions.filter((decision) => Boolean(decision.fallback_used)).length;
      const routerErrorCountDerived = decisions.filter((decision) => String(decision.router_error || "").trim()).length;
      const routerLatencySumDerived = decisions.reduce((sum, decision) => sum + Number(decision.router_latency_sec || 0), 0);
      const metricsDecisionCount = Number(responseMetrics.routing_decisions_total || 0);
      const usingMetricCounts = Number.isFinite(metricsDecisionCount) && metricsDecisionCount > 0;
      const decisionsTotal = usingMetricCounts ? metricsDecisionCount : decisionCountDerived;
      const routedCount = usingMetricCounts
        ? Number(responseMetrics.routing_routellm_count || 0)
        : routedCountDerived;
      const fallbackCount = usingMetricCounts
        ? Number(responseMetrics.routing_fallback_count || 0)
        : fallbackCountDerived;
      const routerErrorCount = usingMetricCounts
        ? Number(responseMetrics.routing_router_error_count || 0)
        : routerErrorCountDerived;
      let latencyAvgSec = Number(responseMetrics.routing_router_latency_avg || 0);
      if ((!Number.isFinite(latencyAvgSec) || latencyAvgSec <= 0) && decisionCountDerived > 0) {{
        latencyAvgSec = routerLatencySumDerived / Math.max(1, decisionCountDerived);
      }}
      if (!Number.isFinite(latencyAvgSec) || latencyAvgSec < 0) latencyAvgSec = 0;
      const enabledLaneItems = laneItems.filter((lane) => Boolean(lane.routellm_enabled));
      const enabledLaneCount = enabledLaneItems.length;
      const routerUrlSet = new Set(
        enabledLaneItems
          .map((lane) => String(lane.routellm_url || "").trim())
          .filter((value) => Boolean(value))
      );
      const distinctRouterCount = routerUrlSet.size;
      const estimatedTokens = Number(responseMetrics.estimated_tokens_total || responseMetrics.tokens_total || 0);
      const totalCost = Number(responseMetrics.cost_usd_total || 0);
      let blendedCostPerMillion = Number(responseMetrics.estimated_cost_per_million_tokens || 0);
      if ((!Number.isFinite(blendedCostPerMillion) || blendedCostPerMillion <= 0) && estimatedTokens > 0 && totalCost > 0) {{
        blendedCostPerMillion = (totalCost * 1000000) / estimatedTokens;
      }}
      if (!Number.isFinite(blendedCostPerMillion) || blendedCostPerMillion < 0) blendedCostPerMillion = 0;
      const estimatedTokensDisplay = Math.max(0, Math.round(estimatedTokens)).toLocaleString();
      const routellmRate = formatPercent(routedCount, decisionsTotal);
      const fallbackRate = formatPercent(fallbackCount, decisionsTotal);
      const routerErrorRate = formatPercent(routerErrorCount, decisionsTotal);
      const fallbackRatio = Number(decisionsTotal) > 0 ? (Number(fallbackCount) / Number(decisionsTotal)) : 0;
      const errorRatio = Number(decisionsTotal) > 0 ? (Number(routerErrorCount) / Number(decisionsTotal)) : 0;
      let routingState = "idle";
      let routingStateClass = "warn";
      if (Number(decisionsTotal) <= 0) {{
        routingState = "idle";
        routingStateClass = "warn";
      }} else if (errorRatio >= 0.05) {{
        routingState = "degraded";
        routingStateClass = "bad";
      }} else if (fallbackRatio >= 0.2) {{
        routingState = "elevated_fallback";
        routingStateClass = "warn";
      }} else {{
        routingState = "healthy";
        routingStateClass = "ok";
      }}

      byId("routingSummary").innerHTML =
        `state=<span class="${{routingStateClass}}">${{escapeHtml(routingState)}}</span> · decisions=${{Number(decisionsTotal)}} · routeLLM=${{routellmRate}} · fallback=${{fallbackRate}} · router_errors=${{routerErrorRate}} · est_tokens=${{estimatedTokensDisplay}} · blended_$1M=$${{blendedCostPerMillion.toFixed(3)}} · enabled_lanes=${{enabledLaneCount}} · routers=${{distinctRouterCount}}`;
      byId("routingDecisions").textContent = String(Number(decisionsTotal));
      byId("routingRoutellmRate").textContent = routellmRate;
      byId("routingFallbackRate").textContent = fallbackRate;
      byId("routingRouterErrorRate").textContent = routerErrorRate;
      byId("routingLatencyAvg").textContent = `${{Math.round(latencyAvgSec * 1000)}}ms`;
      byId("routingEnabledLanes").textContent = `${{enabledLaneCount}}/${{laneItems.length || 0}}`;
      byId("routingEstimatedTokens").textContent = estimatedTokensDisplay;
      byId("routingBlendedCostPerM").textContent = `$${{blendedCostPerMillion.toFixed(3)}}`;

      const providerMapCandidate = (responseMetrics.routing_by_provider && typeof responseMetrics.routing_by_provider === "object")
        ? responseMetrics.routing_by_provider
        : {{}};
      const providerMap = Object.keys(providerMapCandidate).length
        ? providerMapCandidate
        : buildRoutingProviderMapFromEvents(decisions);
      const providerEntries = Object.entries(providerMap).sort((a, b) => {{
        const aResponses = Number((a[1] || {{}}).responses || 0);
        const bResponses = Number((b[1] || {{}}).responses || 0);
        if (aResponses === bResponses) return String(a[0]).localeCompare(String(b[0]));
        return bResponses - aResponses;
      }});
      byId("routingProviderSummary").textContent =
        `providers: ${{providerEntries.length}} · decisions_source=${{usingMetricCounts ? "metrics_summary" : "event_tail"}}`;
      if (!providerEntries.length) {{
        byId("routingProviderBody").innerHTML = '<tr><td colspan="6" class="mono">No provider routing metrics yet.</td></tr>';
      }} else {{
        byId("routingProviderBody").innerHTML = providerEntries.map(([provider, payload]) => {{
          const item = payload || {{}};
          const responses = Number(item.responses || 0);
          const routellmPct = Number(item.routellm_rate || 0) * 100;
          const fallbackPct = Number(item.fallback_rate || 0) * 100;
          const routerErrorPct = Number(item.router_error_rate || 0) * 100;
          const providerTokens = Number(item.tokens_total || 0);
          const providerCost = Number(item.cost_usd_total || 0);
          let providerCostPerMillion = Number(item.cost_per_million_tokens || 0);
          if ((!Number.isFinite(providerCostPerMillion) || providerCostPerMillion <= 0) && providerTokens > 0 && providerCost > 0) {{
            providerCostPerMillion = (providerCost * 1000000) / providerTokens;
          }}
          if (!Number.isFinite(providerCostPerMillion) || providerCostPerMillion < 0) providerCostPerMillion = 0;
          return `<tr>
            <td class="mono">${{escapeHtml(provider)}}</td>
            <td class="mono">${{responses}}</td>
            <td class="mono">${{routellmPct.toFixed(1)}}%</td>
            <td class="mono">${{fallbackPct.toFixed(1)}}%</td>
            <td class="mono">${{routerErrorPct.toFixed(1)}}%</td>
            <td class="mono">$${{providerCostPerMillion.toFixed(3)}}</td>
          </tr>`;
        }}).join("");
      }}

      const laneRoutingStats = buildRoutingLaneStats(decisions);
      const laneById = {{}};
      for (const lane of laneItems) {{
        const laneId = String(lane.id || "").trim();
        if (!laneId) continue;
        laneById[laneId] = lane;
      }}
      const laneIds = new Set([...Object.keys(laneById), ...Object.keys(laneRoutingStats)]);
      const orderedLaneIds = Array.from(laneIds).sort((a, b) => a.localeCompare(b));
      byId("routingLaneSummary").textContent =
        `lanes: ${{orderedLaneIds.length}} · enabled=${{enabledLaneCount}} · with_decisions=${{Object.keys(laneRoutingStats).length}}`;
      if (!orderedLaneIds.length) {{
        byId("routingLaneBody").innerHTML = '<tr><td colspan="7" class="mono">No lane routing data available.</td></tr>';
      }} else {{
        byId("routingLaneBody").innerHTML = orderedLaneIds.map((laneId) => {{
          const lane = laneById[laneId] || {{}};
          const laneStats = laneRoutingStats[laneId] || {{}};
          const owner = String(lane.owner || laneStats.owner || "unknown").trim() || "unknown";
          const laneHealth = String(lane.health || "unknown").trim() || "unknown";
          const healthClass = laneHealth === "ok" ? "ok" : (laneHealth === "error" ? "bad" : "warn");
          const routeEnabled = Boolean(lane.routellm_enabled);
          const routeClass = routeEnabled ? "ok" : "warn";
          const routeLabel = routeEnabled ? "enabled" : "disabled";
          const routerUrl = String(lane.routellm_url || "").trim();
          const routerCell = routerUrl ? escapeHtml(routerUrl) : "-";
          const decisionCount = Number(laneStats.decisions || 0);
          const fallbackCountTail = Number(laneStats.fallback_count || 0);
          const errorCountTail = Number(laneStats.router_error_count || 0);
          return `<tr>
            <td><div class="mono">${{escapeHtml(laneId)}}</div><div class="mono">${{escapeHtml(owner)}}</div></td>
            <td><span class="${{healthClass}}">${{escapeHtml(laneHealth)}}</span></td>
            <td><span class="${{routeClass}}">${{routeLabel}}</span></td>
            <td class="mono">${{routerCell}}</td>
            <td class="mono">${{decisionCount}}</td>
            <td class="mono">${{fallbackCountTail}}</td>
            <td class="mono">${{errorCountTail}}</td>
          </tr>`;
        }}).join("");
      }}

      const routingSourceReports = Array.isArray(safeRoutingPayload.sources) ? safeRoutingPayload.sources : [];
      const sourceFailures = routingSourceReports.filter((source) => !source.ok).length;
      const routingErrors = Array.isArray(safeRoutingPayload.errors)
        ? safeRoutingPayload.errors.filter((item) => String(item || "").trim())
        : [];
      const partialLabel = safeRoutingPayload.partial ? " · partial=true" : "";
      const errorLabel = routingErrors.length ? ` · errors=${{routingErrors.length}}` : "";
      byId("routingDecisionSummary").textContent =
        `events: ${{decisionCountDerived}} · sources: ${{routingSourceReports.length - sourceFailures}}/${{routingSourceReports.length}} healthy${{partialLabel}}${{errorLabel}}`;
      const recentDecisions = decisions
        .slice(-90)
        .sort((a, b) => {{
          const aInfo = eventTimestampInfo(a.timestamp);
          const bInfo = eventTimestampInfo(b.timestamp);
          if (aInfo.valid && bInfo.valid) return bInfo.epochMs - aInfo.epochMs;
          if (aInfo.valid) return -1;
          if (bInfo.valid) return 1;
          return 0;
        }});
      if (!recentDecisions.length) {{
        byId("routingDecisionFeed").innerHTML = '<div class="feed-item">No routing decisions yet.</div>';
      }} else {{
        byId("routingDecisionFeed").innerHTML = recentDecisions.map((decision) => {{
          const ts = formatTimestamp(decision.timestamp || "") || "-";
          const laneId = decision.lane_id || "-";
          const provider = decision.provider || decision.owner || "unknown";
          const strategy = decision.strategy || "static_fallback";
          const selectedModel = decision.selected_model || "-";
          const requestedModel = decision.requested_model || "-";
          const reason = decision.reason || "-";
          const fallback = decision.fallback_used ? "yes" : "no";
          const latencyMs = Math.round(Number(decision.router_latency_sec || 0) * 1000);
          const routerError = String(decision.router_error || "").trim();
          return [
            '<div class="feed-item">',
            `<div class="feed-head"><span>${{escapeHtml(ts)}}</span><span>lane=${{escapeHtml(laneId)}}</span><span>provider=${{escapeHtml(provider)}}</span><span>strategy=${{escapeHtml(strategy)}}</span><span>fallback=${{escapeHtml(fallback)}}</span></div>`,
            `<div class="mono">selected=${{escapeHtml(selectedModel)}} · requested=${{escapeHtml(requestedModel)}} · reason=${{escapeHtml(reason)}} · latency=${{latencyMs}}ms</div>`,
            routerError ? `<div class="bad">router_error=${{escapeHtml(routerError)}}</div>` : '<div class="ok">router_error=none</div>',
            '</div>',
          ].join("");
        }}).join("");
      }}
    }}

    async function fetchJson(path) {{
      let controller = null;
      let timeoutHandle = null;
      try {{
        controller = typeof AbortController === "function" ? new AbortController() : null;
        const options = controller
          ? {{ cache: 'no-store', signal: controller.signal }}
          : {{ cache: 'no-store' }};
        const timeoutPromise = new Promise((_, reject) => {{
          timeoutHandle = setTimeout(() => {{
            if (controller) {{
              controller.abort();
            }}
            reject(new Error(`timeout after ${{FETCH_TIMEOUT_MS}}ms`));
          }}, FETCH_TIMEOUT_MS);
        }});
        const response = await Promise.race([fetch(path, options), timeoutPromise]);
        const rawBody = await response.text();
        let payload = null;
        if (rawBody) {{
          try {{
            payload = JSON.parse(rawBody);
          }} catch (_jsonErr) {{
            payload = null;
          }}
        }}
        if (!response.ok) {{
          const detail = payload && typeof payload === "object" && payload.error
            ? String(payload.error)
            : (rawBody ? rawBody.slice(0, 200) : "");
          const message = detail
            ? `HTTP ${{response.status}}: ${{detail}}`
            : `HTTP ${{response.status}}`;
          return {{ ok: false, payload, error: message }};
        }}
        return {{ ok: true, payload: payload || {{}}, error: "" }};
      }} catch (err) {{
        return {{ ok: false, payload: null, error: String(err) }};
      }} finally {{
        if (timeoutHandle) clearTimeout(timeoutHandle);
      }}
    }}

    async function invokeLaneAction(action) {{
      const laneTarget = String(byId("laneTarget").value || "").trim();
      const query = new URLSearchParams();
      query.set("action", action);
      if (laneTarget) query.set("lane", laneTarget);
      const result = await fetchJson(`/api/lanes/action?${{query.toString()}}`);
      if (!result.ok) {{
        const detail = result.payload && result.payload.error
          ? String(result.payload.error)
          : result.error;
        setLaneActionStatus(`${{action}} failed: ${{detail}}`, true);
        return;
      }}
      const payload = result.payload || {{}};
      const suffix = laneTarget ? ` lane=${{laneTarget}}` : "";
      const failed = Number(payload.failed_count || 0);
      if (action === "ensure") {{
        setLaneActionStatus(
          `ensure${{suffix}} started=${{payload.started_count || 0}} restarted=${{payload.restarted_count || 0}} failed=${{failed}}`,
          failed > 0,
        );
      }} else if (action === "start") {{
        setLaneActionStatus(`start${{suffix}} started=${{payload.started_count || 0}} failed=${{failed}}`, failed > 0);
      }} else if (action === "stop") {{
        setLaneActionStatus(`stop${{suffix}} stopped=${{payload.stopped_count || 0}} failed=${{failed}}`, failed > 0);
      }} else {{
        setLaneActionStatus(`${{action}}${{suffix}} complete`);
      }}
      await refresh();
    }}

    async function inspectLaneStatus() {{
      const laneTarget = String(byId("laneTarget").value || "").trim();
      const result = await fetchJson(laneStatusPath());
      if (!result.ok) {{
        const detail = result.payload && result.payload.error
          ? String(result.payload.error)
          : result.error;
        setLaneActionStatus(`status failed: ${{detail}}`, true);
        return;
      }}
      const payload = result.payload || {{}};
      lastSuccessfulLanePayload = payload;
      const running = Number(payload.running_count || 0);
      const total = Number(payload.total_count || 0);
      const healthCounts = (payload.health_counts && typeof payload.health_counts === "object")
        ? payload.health_counts
        : {{}};
      const degraded = Number(
        healthCounts.degraded || healthCounts.error || healthCounts.stale || healthCounts.unknown || 0
      );
      const errors = Array.isArray(payload.errors)
        ? payload.errors.filter((item) => String(item || "").trim())
        : [];
      const partial = Boolean(payload.partial);
      const recovered = Number(payload.recovered_lane_count || 0);
      const suffix = laneTarget ? ` lane=${{laneTarget}}` : "";
      setLaneActionStatus(
        `status${{suffix}} running=${{running}}/${{total}} degraded=${{degraded}} errors=${{errors.length}} partial=${{yn(partial)}} recovered=${{recovered}}`,
        partial || errors.length > 0,
      );
      await refresh();
    }}

    async function refresh() {{
      const [monitorResult, convResult, laneResult, dawResult, watchdogResult, collabResult, routingResult] = await Promise.all([
        fetchJson('/api/monitor'),
        fetchJson(conversationPath()),
        fetchJson(laneStatusPath()),
        fetchJson('/api/daw?window_sec=120'),
        fetchJson('/api/watchdog?events=40'),
        fetchJson('/api/collab-runtime'),
        fetchJson(routingDecisionPath()),
      ]);

      if (monitorResult.ok && monitorResult.payload) {{
        lastSuccessfulMonitor = monitorResult.payload;
      }}
      const monitorPayload = monitorResult.payload;
      const monitorFallbackPayload = (monitorResult.ok && monitorPayload)
        ? monitorPayload
        : lastSuccessfulMonitor;

      const laneTarget = String(byId("laneTarget").value || "").trim();
      let lanePayload = null;
      if (laneResult.ok && laneResult.payload) {{
        lanePayload = laneResult.payload;
        lastSuccessfulLanePayload = laneResult.payload;
      }} else if (monitorFallbackPayload) {{
        lanePayload = fallbackLanePayloadFromMonitor(monitorFallbackPayload, laneTarget, laneResult.error);
      }} else if (lastSuccessfulLanePayload) {{
        lanePayload = fallbackLanePayloadFromMonitor(
          {{ lanes: lastSuccessfulLanePayload }},
          laneTarget,
          `stale cache used: ${{String(laneResult.error || "lane endpoint unavailable")}}`,
        );
      }} else {{
        lanePayload = fallbackLanePayloadFromMonitor(null, laneTarget, laneResult.error || "lane endpoint unavailable");
      }}

      let effectiveConversationPayload = null;
      if (convResult.ok && convResult.payload) {{
        effectiveConversationPayload = convResult.payload;
        lastSuccessfulConversationPayload = convResult.payload;
      }} else if (monitorFallbackPayload) {{
        effectiveConversationPayload = fallbackConversationPayloadFromMonitor(monitorFallbackPayload, convResult.error);
      }} else if (lastSuccessfulConversationPayload) {{
        effectiveConversationPayload = fallbackConversationPayloadFromCache(lastSuccessfulConversationPayload, convResult.error);
      }} else {{
        effectiveConversationPayload = fallbackConversationPayloadFromCache(null, convResult.error);
      }}

      let effectiveRoutingPayload = null;
      if (routingResult.ok && routingResult.payload) {{
        effectiveRoutingPayload = routingResult.payload;
        lastSuccessfulRoutingPayload = routingResult.payload;
      }} else if (lastSuccessfulRoutingPayload) {{
        effectiveRoutingPayload = {{
          ...lastSuccessfulRoutingPayload,
          partial: true,
          ok: false,
          errors: [
            `routing decisions endpoint unavailable: ${{String(routingResult.error || "unknown error")}}`,
            "routing decisions data from stale cache",
          ],
        }};
      }} else {{
        effectiveRoutingPayload = {{
          total_events: 0,
          owner_counts: {{}},
          events: [],
          sources: [],
          partial: true,
          ok: false,
          errors: [String(routingResult.error || "routing decisions endpoint unavailable")],
          filters: {{}},
          unfiltered_total_events: 0,
        }};
      }}

      let snapshotForRender = monitorFallbackPayload;
      if (snapshotForRender && !monitorResult.ok) {{
        const endpointError = String(monitorResult.error || "monitor endpoint unavailable");
        snapshotForRender = {{
          ...snapshotForRender,
          latest_log_line: `monitor endpoint unavailable (${{endpointError}}); using cached snapshot`,
        }};
      }}

      if (snapshotForRender) {{
        render(snapshotForRender, lanePayload, effectiveConversationPayload);
      }} else {{
        byId("latestLog").textContent = `monitor fetch failed: ${{monitorResult.error}}`;
        byId("updated").textContent = `updated: ${{formatNowTimestamp()}} (${{USER_TIMEZONE}})`;
        if (lanePayload) {{
          renderLanes(lanePayload, {{}}, effectiveConversationPayload);
        }} else {{
          byId("laneSummary").textContent = "lanes: unavailable";
          byId("laneList").innerHTML = '<div class="line bad">lane endpoint unavailable</div>';
        }}
      }}
      renderConversations(effectiveConversationPayload || {{
        total_events: 0,
        owner_counts: {{}},
        events: [],
        sources: [],
        partial: true,
        errors: [],
        filters: {{}},
      }});
      renderRouting(snapshotForRender || {{}}, lanePayload || {{}}, effectiveRoutingPayload || {{}});
      if (dawResult.ok && dawResult.payload) {{
        lastSuccessfulDawPayload = dawResult.payload;
        renderDaw(dawResult.payload);
      }} else if (lastSuccessfulDawPayload) {{
        renderDaw(lastSuccessfulDawPayload);
      }} else {{
        renderDaw({{
          tracks: [],
          mixer: [],
          activity_feed: [],
          prompt_midi_events: 0,
          response_audio_events: 0,
          control_events: 0,
        }});
      }}
      let effectiveWatchdogPayload = null;
      if (watchdogResult.ok && watchdogResult.payload) {{
        effectiveWatchdogPayload = watchdogResult.payload;
        lastSuccessfulWatchdogPayload = watchdogResult.payload;
      }} else if (lastSuccessfulWatchdogPayload) {{
        effectiveWatchdogPayload = {{
          ...lastSuccessfulWatchdogPayload,
          partial: true,
          ok: false,
          errors: [
            `watchdog endpoint unavailable: ${{String(watchdogResult.error || "unknown error")}}`,
            "watchdog data from stale cache",
          ],
        }};
      }} else {{
        effectiveWatchdogPayload = {{
          ok: false,
          partial: true,
          state_exists: false,
          history_exists: false,
          state_file: "",
          history_file: "",
          total_processes: 0,
          healthy_count: 0,
          restarted_count: 0,
          problematic_count: 0,
          runs_total: 0,
          last_run_at: "",
          last_run_age_sec: -1,
          processes: [],
          recent_events: [],
          problematic_ids: [],
          errors: [String(watchdogResult.error || "watchdog endpoint unavailable")],
        }};
      }}
      renderWatchdog(effectiveWatchdogPayload);
      let effectiveCollabPayload = null;
      if (collabResult.ok && collabResult.payload) {{
        effectiveCollabPayload = collabResult.payload;
        lastSuccessfulCollabPayload = collabResult.payload;
      }} else if (lastSuccessfulCollabPayload) {{
        effectiveCollabPayload = {{
          ...lastSuccessfulCollabPayload,
          ok: false,
          partial: true,
          errors: [
            `collaboration runtime endpoint unavailable: ${{String(collabResult.error || "unknown error")}}`,
            "collaboration runtime data from stale cache",
          ],
        }};
      }} else {{
        effectiveCollabPayload = {{
          ok: false,
          partial: true,
          errors: [String(collabResult.error || "collaboration runtime endpoint unavailable")],
          rows: [],
          summary: {{
            total_rows: 0,
            running_rows: 0,
            thinking_rows: 0,
            active_rows: 0,
            stale_rows: 0,
            offline_rows: 0,
            commits_last_hour_total: 0,
            attention_rows: 0,
            critical_rows: 0,
            warn_rows: 0,
            watch_rows: 0,
            max_attention_score: 0,
            top_attention: [],
            latest_signal_at: "",
            latest_signal_age_sec: -1,
            latest_health_at: "",
            latest_health_age_sec: -1,
            latest_task_done_at: "",
            latest_task_done_age_sec: -1,
            latest_push_at: "",
            latest_push_age_sec: -1,
            latest_commit_at: "",
            latest_commit_age_sec: -1,
            signal_bins_5m_total: [],
            signal_bins_5m_max: 0,
            commit_bins_5m_total: [],
            commit_bins_5m_max: 0,
          }},
        }};
      }}
      renderCollaboratorRuntime(effectiveCollabPayload);

      const diagnosticsPayload = (snapshotForRender && snapshotForRender.diagnostics) || {{}};
      renderDiagnostics(diagnosticsPayload, {{
        monitor_endpoint: monitorResult.ok ? "" : monitorResult.error,
        conversations_endpoint: convResult.ok ? "" : convResult.error,
        lanes_endpoint: laneResult.ok ? "" : laneResult.error,
        watchdog_endpoint: watchdogResult.ok ? "" : watchdogResult.error,
        collab_runtime_endpoint: collabResult.ok ? "" : collabResult.error,
        routing_decisions_endpoint: routingResult.ok ? "" : routingResult.error,
      }});
    }}

    function schedule() {{
      if (timer) clearInterval(timer);
      timer = setInterval(() => {{ if (!paused) refresh(); }}, REFRESH_MS);
    }}

    function applyConversationFilters() {{
      conversationFilters.owner = String(byId("convOwner").value || "").trim();
      conversationFilters.lane = String(byId("convLane").value || "").trim();
      conversationFilters.event_type = String(byId("convType").value || "").trim();
      conversationFilters.contains = String(byId("convContains").value || "").trim();
      conversationFilters.tail = parseTail(byId("convTail").value);
      refresh();
    }}

    function clearConversationFilters() {{
      conversationFilters.owner = "";
      conversationFilters.lane = "";
      conversationFilters.event_type = "";
      conversationFilters.contains = "";
      conversationFilters.tail = 0;
      syncConversationInputs();
      refresh();
    }}

    byId("refresh").addEventListener("click", refresh);
    byId("pause").addEventListener("click", () => {{
      paused = !paused;
      byId("pause").textContent = paused ? "Resume" : "Pause";
    }});
    byId("convApply").addEventListener("click", applyConversationFilters);
    byId("convClear").addEventListener("click", clearConversationFilters);
    byId("laneStatus").addEventListener("click", inspectLaneStatus);
    byId("laneEnsure").addEventListener("click", () => invokeLaneAction("ensure"));
    byId("laneStart").addEventListener("click", () => invokeLaneAction("start"));
    byId("laneStop").addEventListener("click", () => invokeLaneAction("stop"));

    initTabs();
    syncConversationInputs();
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
                if parsed.path == "/api/lanes/action":
                    query = parse_qs(parsed.query)
                    action = query.get("action", [""])[0]
                    lane_id = query.get("lane", [""])[0]
                    payload = _safe_lane_action(config, action=action, lane_id=lane_id)
                    status = _lane_action_http_status(payload)
                    self._send_json(payload, status=status)
                    return
                if parsed.path == "/api/lanes":
                    query = parse_qs(parsed.query)
                    payload = _filter_lane_status_payload(
                        _safe_lane_status_snapshot(config),
                        lane_id=query.get("lane", [""])[0],
                    )
                    include_conversations = _parse_bool(query, "include_conversations", default=True)
                    if include_conversations:
                        conversation_lines = _parse_bounded_int(
                            query,
                            "conversation_lines",
                            default=200,
                            minimum=1,
                            maximum=2000,
                        )
                        conversation_payload = _safe_conversations_snapshot(
                            config,
                            lines=conversation_lines,
                            include_lanes=True,
                            lane_id=query.get("lane", [""])[0],
                        )
                        payload = _augment_lane_payload_with_conversation_rollup(payload, conversation_payload)
                    self._send_json(payload)
                    return
                if parsed.path == "/api/conversations":
                    query = parse_qs(parsed.query)
                    lines = _parse_bounded_int(query, "lines", default=200, minimum=1, maximum=2000)
                    tail = _parse_bounded_int(query, "tail", default=0, minimum=0, maximum=2000)
                    include_lanes = _parse_bool(query, "include_lanes", default=True)
                    self._send_json(
                        _safe_conversations_snapshot(
                            config,
                            lines=lines,
                            include_lanes=include_lanes,
                            owner=query.get("owner", [""])[0],
                            lane_id=query.get("lane", [""])[0],
                            event_type=query.get("event_type", [""])[0],
                            contains=query.get("contains", [""])[0],
                            tail=tail,
                        )
                    )
                    return
                if parsed.path == "/api/daw":
                    query = parse_qs(parsed.query)
                    window_sec = _parse_bounded_int(query, "window_sec", default=120, minimum=20, maximum=1800)
                    lines = _parse_bounded_int(query, "lines", default=800, minimum=200, maximum=2000)
                    self._send_json(_safe_daw_snapshot(config, window_sec=window_sec, lines=lines))
                    return
                if parsed.path == "/api/watchdog":
                    query = parse_qs(parsed.query)
                    events = _parse_bounded_int(query, "events", default=40, minimum=1, maximum=500)
                    self._send_json(_safe_watchdog_snapshot(config, events=events))
                    return
                if parsed.path == "/api/collab-runtime":
                    self._send_json(_safe_collab_runtime_snapshot(config))
                    return
                if parsed.path == "/api/status":
                    self._send_json(status_snapshot(config))
                    return
                if parsed.path == "/api/health":
                    self._send_json(health_snapshot(config))
                    return
                if parsed.path == "/api/logs":
                    query = parse_qs(parsed.query)
                    lines = _parse_bounded_int(query, "lines", default=80, minimum=1, maximum=500)
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


def _parse_bounded_int(
    query: dict[str, list[str]],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = query.get(key, [str(default)])[0]
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _parse_bool(query: dict[str, list[str]], key: str, *, default: bool) -> bool:
    raw = query.get(key, [""])[0].strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_event_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _conversation_event_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    parsed = _parse_event_timestamp(item.get("timestamp", ""))
    if parsed is None:
        # Keep invalid timestamps ordered by source sequence (stable sort).
        return (0, float("-inf"), "")
    return (1, parsed.timestamp(), "")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


def _event_kind(event_type: str) -> str:
    normalized = event_type.strip().lower()
    if normalized == "prompt":
        return "midi"
    if normalized in {"agent_output", "task_done", "task_partial"}:
        return "audio"
    return "control"


def _event_level(event: dict[str, Any]) -> float:
    meta = event.get("meta", {})
    if not isinstance(meta, dict):
        meta = {}
    kind = _event_kind(str(event.get("event_type", "")))
    if kind == "midi":
        difficulty = _safe_float(meta.get("prompt_difficulty_score", 0.0), 0.0)
        tokens = _safe_float(meta.get("prompt_tokens_est", 0.0), 0.0)
        return _clamp01(max(difficulty / 10.0, min(tokens / 2000.0, 1.0)))
    if kind == "audio":
        output_tokens = _safe_float(meta.get("response_tokens_est", 0.0), 0.0)
        latency = _safe_float(meta.get("latency_sec", 0.0), 0.0)
        return _clamp01(max(min(output_tokens / 2500.0, 1.0), min(latency / 120.0, 1.0)))
    if str(event.get("event_type", "")).strip().lower() in {"task_blocked", "agent_error", "auto_push_error"}:
        return 1.0
    return 0.45


def _safe_daw_snapshot(config: ManagerConfig, *, window_sec: int = 120, lines: int = 800) -> dict[str, Any]:
    try:
        conv = conversations_snapshot(config, lines=max(200, int(lines)), include_lanes=True)
        monitor = _safe_monitor_snapshot(config)
        return _build_daw_snapshot(conv, monitor, window_sec=max(20, int(window_sec)))
    except Exception as err:
        message = str(err)
        return {
            "timestamp": "",
            "window_sec": max(20, int(window_sec)),
            "tempo_bpm": 120,
            "playhead_sec": 0.0,
            "tracks": [],
            "mixer": [],
            "activity_feed": [],
            "prompt_midi_events": 0,
            "response_audio_events": 0,
            "control_events": 0,
            "ok": False,
            "errors": [message],
        }


def _build_daw_snapshot(
    conv_payload: dict[str, Any],
    monitor_payload: dict[str, Any],
    *,
    window_sec: int = 120,
) -> dict[str, Any]:
    events = conv_payload.get("events", [])
    if not isinstance(events, list):
        events = []
    now = datetime.now(timezone.utc)
    window = max(20, int(window_sec))
    start_time = now.timestamp() - float(window)
    tracks: dict[str, dict[str, Any]] = {}
    feed: list[dict[str, Any]] = []
    midi_count = 0
    audio_count = 0
    control_count = 0
    for item in events:
        if not isinstance(item, dict):
            continue
        ts = _parse_event_timestamp(item.get("timestamp", ""))
        if ts is None:
            continue
        ts_sec = ts.timestamp()
        if ts_sec < start_time:
            continue
        owner = str(item.get("owner", "unknown")).strip() or "unknown"
        lane_id = str(item.get("lane_id", "")).strip()
        track_key = f"{owner}:{lane_id or 'main'}"
        track_name = f"{owner} / {lane_id or 'main'}"
        bucket = tracks.setdefault(track_key, {"name": track_name, "clips": [], "peak": 0.0, "events": 0})

        event_type = str(item.get("event_type", "")).strip()
        kind = _event_kind(event_type)
        if kind == "midi":
            midi_count += 1
        elif kind == "audio":
            audio_count += 1
        else:
            control_count += 1
        level = _event_level(item)
        start_pct = _clamp01((ts_sec - start_time) / float(window)) * 100.0
        width_pct = max(1.4, 1.8 + (level * 6.0))
        bucket["clips"].append(
            {
                "kind": kind,
                "event_type": event_type,
                "start_pct": round(start_pct, 3),
                "width_pct": round(min(width_pct, 16.0), 3),
                "level": round(level, 4),
                "label": str(item.get("task_id", "")).strip() or event_type or "event",
            }
        )
        bucket["peak"] = max(_safe_float(bucket.get("peak", 0.0), 0.0), level)
        bucket["events"] = int(bucket.get("events", 0)) + 1
        feed.append(
            {
                "timestamp": str(item.get("timestamp", "")).strip(),
                "track": track_name,
                "kind": kind,
                "event_type": event_type,
                "label": str(item.get("content", "")).strip()[:180],
            }
        )

    track_rows = sorted(tracks.values(), key=lambda row: str(row.get("name", "")))
    mixer = [
        {
            "name": str(row.get("name", "")),
            "level": round(_clamp01(_safe_float(row.get("peak", 0.0), 0.0)), 4),
            "events": int(row.get("events", 0)),
        }
        for row in track_rows
    ]
    lane_counts = monitor_payload.get("lanes", {}).get("health_counts", {}) if isinstance(monitor_payload.get("lanes", {}), dict) else {}
    tempo = 120 + (int(lane_counts.get("ok", 0)) * 2) + (int(lane_counts.get("error", 0)) * 6)
    return {
        "timestamp": str(monitor_payload.get("timestamp", "")).strip(),
        "window_sec": window,
        "tempo_bpm": max(96, min(160, tempo)),
        "playhead_sec": float(window),
        "tracks": track_rows,
        "mixer": mixer,
        "activity_feed": feed[-80:],
        "prompt_midi_events": midi_count,
        "response_audio_events": audio_count,
        "control_events": control_count,
        "ok": bool(conv_payload.get("ok", False)),
        "errors": list(conv_payload.get("errors", [])) if isinstance(conv_payload.get("errors", []), list) else [],
    }


def _apply_conversation_filters(
    payload: dict[str, Any],
    *,
    owner: str = "",
    lane_id: str = "",
    event_type: str = "",
    contains: str = "",
    tail: int = 0,
) -> dict[str, Any]:
    owner_filter = owner.strip().lower()
    lane_filter = lane_id.strip().lower()
    type_filter = event_type.strip().lower()
    contains_filter = contains.strip().lower()

    events = payload.get("events", [])
    if not isinstance(events, list):
        events = []

    filtered: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        owner_value = str(item.get("owner", "")).strip().lower()
        lane_value = str(item.get("lane_id", "")).strip().lower()
        type_value = str(item.get("event_type", "")).strip().lower()
        haystack = " ".join(
            [
                str(item.get("timestamp", "")),
                str(item.get("owner", "")),
                str(item.get("lane_id", "")),
                str(item.get("task_id", "")),
                str(item.get("event_type", "")),
                str(item.get("content", "")),
            ]
        ).lower()
        if owner_filter and owner_value != owner_filter:
            continue
        if lane_filter and lane_value != lane_filter:
            continue
        if type_filter and type_value != type_filter:
            continue
        if contains_filter and contains_filter not in haystack:
            continue
        filtered.append(item)

    tail_count = max(0, int(tail))
    if tail_count:
        filtered = filtered[-tail_count:]

    owner_counts: dict[str, int] = {}
    for event in filtered:
        event_owner = str(event.get("owner", "unknown")).strip() or "unknown"
        owner_counts[event_owner] = owner_counts.get(event_owner, 0) + 1

    result = dict(payload)
    result["events"] = filtered
    result["owner_counts"] = owner_counts
    result["total_events"] = len(filtered)
    result["unfiltered_total_events"] = len(events)
    result["filters"] = {
        "owner": owner.strip(),
        "lane": lane_id.strip(),
        "event_type": event_type.strip(),
        "contains": contains.strip(),
        "tail": tail_count,
    }
    return result


def _filter_conversation_payload_for_lane(
    payload: dict[str, Any],
    *,
    lane_id: str = "",
) -> dict[str, Any]:
    requested_lane = lane_id.strip()
    requested_lane_normalized = requested_lane.lower()
    source_reports = payload.get("sources", [])
    if not isinstance(source_reports, list):
        source_reports = []
    normalized_sources = [item for item in source_reports if isinstance(item, dict)]

    if not requested_lane:
        result = dict(payload)
        result["sources"] = normalized_sources
        result["suppressed_sources"] = []
        result["suppressed_source_count"] = 0
        result["suppressed_source_errors"] = []
        result["suppressed_source_error_count"] = 0
        return result

    retained_sources: list[dict[str, Any]] = []
    suppressed_sources: list[dict[str, Any]] = []
    for source in normalized_sources:
        source_lane = str(source.get("lane_id", "")).strip()
        if source_lane and source_lane.lower() != requested_lane_normalized:
            suppressed_sources.append(source)
            continue
        retained_sources.append(source)

    # If lane-specific conversation source data is healthy, a failed
    # primary/global source should not block lane-focused observability.
    lane_source_healthy = any(
        str(source.get("lane_id", "")).strip().lower() == requested_lane_normalized
        and bool(source.get("ok", False))
        for source in retained_sources
    )
    if lane_source_healthy:
        lane_scoped_sources: list[dict[str, Any]] = []
        for source in retained_sources:
            source_lane = str(source.get("lane_id", "")).strip()
            if source_lane and source_lane.lower() == requested_lane_normalized:
                lane_scoped_sources.append(source)
                continue
            source_kind = str(source.get("resolved_kind", source.get("kind", ""))).strip().lower()
            if bool(source.get("ok", False)) or source_kind != "primary":
                lane_scoped_sources.append(source)
                continue
            suppressed_sources.append(source)
        retained_sources = lane_scoped_sources

    retained_errors = [
        str(source.get("error", "")).strip()
        for source in retained_sources
        if str(source.get("error", "")).strip()
    ]
    suppressed_source_errors = [
        str(source.get("error", "")).strip()
        for source in suppressed_sources
        if str(source.get("error", "")).strip()
    ]

    def _lane_error_matches_source(message: str, source: dict[str, Any]) -> bool:
        normalized = message.strip().lower()
        if not normalized:
            return False
        lane = str(source.get("lane_id", "")).strip().lower()
        if lane and re.search(rf"(^|[^a-z0-9_-]){re.escape(lane)}([^a-z0-9_-]|$)", normalized):
            return True
        source_error = str(source.get("error", "")).strip().lower()
        if source_error and source_error in normalized:
            return True
        for key in ("path", "resolved_path"):
            source_path = str(source.get(key, "")).strip().lower()
            if source_path and source_path in normalized:
                return True
        source_kind = str(source.get("resolved_kind", source.get("kind", ""))).strip().lower()
        if source_kind == "primary":
            if any(token in normalized for token in ("primary", "global", "conversation source", "conversations.ndjson")):
                return True
        return False

    suppressed_payload_errors: list[str] = []
    payload_errors = _normalize_error_messages(payload.get("errors", []))
    for entry in payload_errors:
        message = str(entry).strip()
        if not message:
            continue
        if any(_lane_error_matches_source(message, source) for source in suppressed_sources):
            suppressed_payload_errors.append(message)
            continue
        if message not in retained_errors:
            retained_errors.append(message)

    retained_source_failures = any(not bool(source.get("ok", False)) for source in retained_sources)
    partial = retained_source_failures or bool(retained_errors)
    filtered = dict(payload)
    filtered["sources"] = retained_sources
    filtered["errors"] = retained_errors
    filtered["ok"] = not partial
    filtered["partial"] = partial
    filtered["suppressed_sources"] = suppressed_sources
    filtered["suppressed_source_count"] = len(suppressed_sources)
    merged_suppressed_errors: list[str] = []
    for message in [*suppressed_source_errors, *suppressed_payload_errors]:
        if message and message not in merged_suppressed_errors:
            merged_suppressed_errors.append(message)
    filtered["suppressed_source_errors"] = merged_suppressed_errors
    filtered["suppressed_source_error_count"] = len(merged_suppressed_errors)
    return filtered


def _normalize_error_messages(raw: Any) -> list[str]:
    if isinstance(raw, list):
        values = raw
    elif raw in (None, "", []):
        values = []
    else:
        values = [raw]
    normalized: list[str] = []
    for item in values:
        message = str(item).strip()
        if message:
            normalized.append(message)
    return normalized


def _lane_conversation_rollup(conversation_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source_reports = conversation_payload.get("sources", [])
    if not isinstance(source_reports, list):
        source_reports = []
    events = conversation_payload.get("events", [])
    if not isinstance(events, list):
        events = []

    rollup_raw: dict[str, dict[str, Any]] = {}

    def _entry(lane_id: str) -> dict[str, Any]:
        return rollup_raw.setdefault(
            lane_id,
            {
                "lane_id": lane_id,
                "owner_hints": {},
                "source_count": 0,
                "source_ok": None,
                "source_event_count": 0,
                "source_error_count": 0,
                "missing_count": 0,
                "recoverable_missing_count": 0,
                "fallback_count": 0,
                "observed_event_count": 0,
                "latest_event": {},
            },
        )

    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    for item in source_reports:
        if not isinstance(item, dict):
            continue
        lane_key = str(item.get("lane_id", "")).strip()
        if not lane_key:
            continue
        current = _entry(lane_key)
        source_owner = str(item.get("owner", "")).strip() or "unknown"
        if source_owner != "unknown":
            owner_hints = current.get("owner_hints")
            if not isinstance(owner_hints, dict):
                owner_hints = {}
                current["owner_hints"] = owner_hints
            owner_hints[source_owner] = _safe_int(owner_hints.get(source_owner, 0)) + 1
        current["source_count"] += 1
        current_ok = bool(item.get("ok", False))
        if current["source_ok"] is None:
            current["source_ok"] = current_ok
        else:
            current["source_ok"] = bool(current["source_ok"]) and current_ok
        current["source_event_count"] += max(0, _safe_int(item.get("event_count", 0)))
        if str(item.get("error", "")).strip():
            current["source_error_count"] += 1
        if bool(item.get("missing", False)):
            current["missing_count"] += 1
        if bool(item.get("recoverable_missing", False)):
            current["recoverable_missing_count"] += 1
        if bool(item.get("fallback_used", False)):
            current["fallback_count"] += 1

    for item in events:
        if not isinstance(item, dict):
            continue
        lane_key = str(item.get("lane_id", "")).strip()
        if not lane_key:
            continue
        current = _entry(lane_key)
        current["observed_event_count"] += 1
        event_owner = str(item.get("owner", "")).strip() or "unknown"
        if event_owner != "unknown":
            owner_hints = current.get("owner_hints")
            if not isinstance(owner_hints, dict):
                owner_hints = {}
                current["owner_hints"] = owner_hints
            owner_hints[event_owner] = _safe_int(owner_hints.get(event_owner, 0)) + 1
        candidate = {
            "timestamp": str(item.get("timestamp", "")).strip(),
            "owner": event_owner,
            "lane_id": lane_key,
            "task_id": str(item.get("task_id", "")).strip(),
            "event_type": str(item.get("event_type", "")).strip(),
            "content": str(item.get("content", "")).strip(),
            "source": str(item.get("source", "")).strip(),
            "source_kind": str(item.get("source_kind", "")).strip(),
        }
        existing = current.get("latest_event", {})
        existing_ts = _parse_event_timestamp(existing.get("timestamp")) if isinstance(existing, dict) else None
        candidate_ts = _parse_event_timestamp(candidate.get("timestamp"))
        if existing_ts is None and candidate_ts is None:
            # Preserve event sequence when timestamps are malformed.
            should_replace = True
        elif existing_ts is None:
            should_replace = True
        elif candidate_ts is None:
            should_replace = False
        else:
            should_replace = candidate_ts >= existing_ts
        if should_replace:
            current["latest_event"] = candidate

    rollup: dict[str, dict[str, Any]] = {}
    for lane_key, item in rollup_raw.items():
        source_ok = item.get("source_ok")
        source_state = "unreported"
        if source_ok is True:
            source_state = "ok"
        elif source_ok is False:
            source_state = "error"
        latest_event = item.get("latest_event", {}) if isinstance(item.get("latest_event", {}), dict) else {}
        owner = str(latest_event.get("owner", "")).strip() or "unknown"
        owner_hints = item.get("owner_hints", {})
        if owner == "unknown" and isinstance(owner_hints, dict):
            ordered_hints = sorted(
                (
                    (str(name).strip(), _safe_int(count))
                    for name, count in owner_hints.items()
                    if str(name).strip() and str(name).strip() != "unknown"
                ),
                key=lambda pair: (-pair[1], pair[0]),
            )
            if ordered_hints:
                owner = ordered_hints[0][0]
        rollup[lane_key] = {
            "lane_id": lane_key,
            "owner": owner,
            "source_count": _safe_int(item.get("source_count", 0)),
            "source_ok": source_ok,
            "source_state": source_state,
            "event_count": max(
                _safe_int(item.get("source_event_count", 0)),
                _safe_int(item.get("observed_event_count", 0)),
            ),
            "source_error_count": _safe_int(item.get("source_error_count", 0)),
            "missing_count": _safe_int(item.get("missing_count", 0)),
            "recoverable_missing_count": _safe_int(item.get("recoverable_missing_count", 0)),
            "fallback_count": _safe_int(item.get("fallback_count", 0)),
            "latest_event": latest_event,
        }
    return rollup


def _lane_error_mentions_unknown_lane(error: str, lane_id: str) -> bool:
    message = str(error).strip().lower()
    lane = lane_id.strip().lower()
    if not message or not lane:
        return False
    if not message.startswith("unknown lane id "):
        return False
    return lane in message


def _lane_error_mentions_unavailable_lane(error: str, lane_id: str) -> bool:
    message = str(error).strip().lower()
    lane = lane_id.strip().lower()
    if not message or not lane:
        return False
    if not message.startswith("requested lane "):
        return False
    if "is unavailable because lane status sources failed." not in message:
        return False
    return lane in message


def _lane_owner_health_counts(lanes: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, dict[str, int]], int]:
    health_counts: dict[str, int] = {}
    owner_counts: dict[str, dict[str, int]] = {}
    running_count = 0
    healthy_states = {"ok", "paused", "idle"}
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        health = str(lane.get("health", "unknown")).strip().lower() or "unknown"
        health_counts[health] = health_counts.get(health, 0) + 1
        owner = str(lane.get("owner", "unknown")).strip() or "unknown"
        owner_entry = owner_counts.setdefault(owner, {"total": 0, "running": 0, "healthy": 0, "degraded": 0})
        owner_entry["total"] += 1
        if bool(lane.get("running", False)):
            running_count += 1
            owner_entry["running"] += 1
        if health in healthy_states:
            owner_entry["healthy"] += 1
        else:
            owner_entry["degraded"] += 1
    return health_counts, owner_counts, running_count


def _augment_lane_payload_with_conversation_rollup(
    lane_payload: dict[str, Any],
    conversation_payload: dict[str, Any],
) -> dict[str, Any]:
    rollup = _lane_conversation_rollup(conversation_payload)
    lane_items = lane_payload.get("lanes", [])
    if not isinstance(lane_items, list):
        lane_items = []

    requested_lane_raw = str(lane_payload.get("requested_lane", "all")).strip() or "all"
    requested_lane = requested_lane_raw
    if requested_lane != "all":
        known_lane_ids = sorted(set(rollup) | {str(item.get("id", "")).strip() for item in lane_items if isinstance(item, dict)})
        folded_matches = [lane_id for lane_id in known_lane_ids if lane_id and lane_id.lower() == requested_lane.lower()]
        if len(folded_matches) == 1:
            requested_lane = folded_matches[0]
    enriched_lanes: list[dict[str, Any]] = []
    seen_lanes: set[str] = set()
    for lane in lane_items:
        if not isinstance(lane, dict):
            continue
        lane_id = str(lane.get("id", "")).strip()
        if lane_id:
            seen_lanes.add(lane_id)
        lane_rollup = rollup.get(lane_id, {})
        lane_copy = dict(lane)
        rollup_owner = str(lane_rollup.get("owner", "")).strip() or "unknown"
        if (str(lane_copy.get("owner", "")).strip() or "unknown") == "unknown" and rollup_owner != "unknown":
            lane_copy["owner"] = rollup_owner
        lane_copy["conversation_event_count"] = int(lane_rollup.get("event_count", 0))
        lane_copy["conversation_source_count"] = int(lane_rollup.get("source_count", 0))
        lane_copy["conversation_source_state"] = str(lane_rollup.get("source_state", "unreported"))
        source_ok = lane_rollup.get("source_ok")
        lane_copy["conversation_source_ok"] = source_ok if isinstance(source_ok, bool) else None
        lane_copy["conversation_source_error_count"] = int(lane_rollup.get("source_error_count", 0))
        lane_copy["conversation_source_missing_count"] = int(lane_rollup.get("missing_count", 0))
        lane_copy["conversation_source_recoverable_missing_count"] = int(lane_rollup.get("recoverable_missing_count", 0))
        lane_copy["conversation_source_fallback_count"] = int(lane_rollup.get("fallback_count", 0))
        lane_copy["latest_conversation_event"] = (
            lane_rollup.get("latest_event", {})
            if isinstance(lane_rollup.get("latest_event", {}), dict)
            else {}
        )
        enriched_lanes.append(lane_copy)

    recovered_lanes: list[str] = []
    synthesize_lane_ids: list[str] = []
    if requested_lane != "all":
        if requested_lane in rollup and requested_lane not in seen_lanes:
            synthesize_lane_ids.append(requested_lane)
    else:
        lane_source_degraded = (
            not bool(lane_payload.get("ok", True))
            or bool(lane_payload.get("partial", False))
            or bool(lane_payload.get("errors", []))
        )
        if lane_source_degraded:
            synthesize_lane_ids.extend(sorted(lane_id for lane_id in rollup if lane_id not in seen_lanes))

    for lane_id in synthesize_lane_ids:
        lane_rollup = rollup.get(lane_id, {})
        latest_event = lane_rollup.get("latest_event", {})
        if not isinstance(latest_event, dict):
            latest_event = {}
        owner = str(latest_event.get("owner", "")).strip() or ""
        if not owner or owner == "unknown":
            owner = str(lane_rollup.get("owner", "unknown")).strip() or "unknown"
        lane_copy = {
            "id": lane_id,
            "owner": owner,
            "running": False,
            "pid": None,
            "health": "unknown",
            "heartbeat_age_sec": -1,
            "state_counts": {},
            "build_current": False,
            "error": "lane status missing; derived from conversation logs",
            "conversation_lane_fallback": True,
            "conversation_event_count": int(lane_rollup.get("event_count", 0)),
            "conversation_source_count": int(lane_rollup.get("source_count", 0)),
            "conversation_source_state": str(lane_rollup.get("source_state", "unreported")),
            "conversation_source_ok": (
                lane_rollup.get("source_ok")
                if isinstance(lane_rollup.get("source_ok"), bool)
                else None
            ),
            "conversation_source_error_count": int(lane_rollup.get("source_error_count", 0)),
            "conversation_source_missing_count": int(lane_rollup.get("missing_count", 0)),
            "conversation_source_recoverable_missing_count": int(lane_rollup.get("recoverable_missing_count", 0)),
            "conversation_source_fallback_count": int(lane_rollup.get("fallback_count", 0)),
            "latest_conversation_event": latest_event,
        }
        enriched_lanes.append(lane_copy)
        recovered_lanes.append(lane_id)

    conversation_errors = conversation_payload.get("errors", [])
    if not isinstance(conversation_errors, list):
        conversation_errors = [str(conversation_errors)] if str(conversation_errors).strip() else []
    normalized_errors = [str(item).strip() for item in conversation_errors if str(item).strip()]
    lane_errors = lane_payload.get("errors", [])
    if not isinstance(lane_errors, list):
        lane_errors = [str(lane_errors)] if str(lane_errors).strip() else []
    combined_lane_errors = [str(item).strip() for item in lane_errors if str(item).strip()]
    if recovered_lanes:
        combined_lane_errors = [
            message
            for message in combined_lane_errors
            if not any(
                _lane_error_mentions_unknown_lane(message, lane_id)
                or _lane_error_mentions_unavailable_lane(message, lane_id)
                for lane_id in recovered_lanes
            )
        ]
        for lane_id in recovered_lanes:
            warning = f"Lane status missing for {lane_id!r}; using conversation-derived fallback."
            if warning not in combined_lane_errors:
                combined_lane_errors.append(warning)

    health_counts, owner_counts, running_count = _lane_owner_health_counts(enriched_lanes)
    lane_partial = bool(lane_payload.get("partial", False))
    conversation_partial = bool(conversation_payload.get("partial", False))
    partial = lane_partial or conversation_partial or bool(combined_lane_errors) or bool(recovered_lanes)
    ok = bool(lane_payload.get("ok", not combined_lane_errors)) and not partial

    result = dict(lane_payload)
    result["lanes"] = enriched_lanes
    result["running_count"] = running_count
    result["total_count"] = len(enriched_lanes)
    result["health_counts"] = health_counts
    result["owner_counts"] = owner_counts
    result["errors"] = combined_lane_errors
    result["partial"] = partial
    result["ok"] = ok
    result["recovered_lanes"] = recovered_lanes
    result["recovered_lane_count"] = len(recovered_lanes)
    result["conversation_by_lane"] = rollup
    result["conversation_partial"] = bool(conversation_payload.get("partial", False))
    result["conversation_ok"] = bool(conversation_payload.get("ok", False))
    result["conversation_errors"] = normalized_errors
    result["requested_lane"] = requested_lane if requested_lane != "all" else "all"
    return result


def _safe_lane_action(config: ManagerConfig, *, action: str, lane_id: str = "") -> dict[str, Any]:
    normalized_action = action.strip().lower()
    normalized_lane = lane_id.strip() or None
    try:
        if normalized_action == "status":
            payload = _filter_lane_status_payload(
                _safe_lane_status_snapshot(config),
                lane_id=normalized_lane or "",
            )
            conversation_payload = _safe_conversations_snapshot(
                config,
                lines=200,
                include_lanes=True,
                lane_id=normalized_lane or "",
            )
            payload = _augment_lane_payload_with_conversation_rollup(payload, conversation_payload)
            payload["action"] = "status"
            payload["lane"] = (
                str(payload.get("requested_lane", normalized_lane)).strip()
                if normalized_lane
                else ""
            )
            return payload
        if normalized_action == "ensure":
            payload = ensure_lanes_background(config, lane_id=normalized_lane)
            payload["action"] = "ensure"
            payload["lane"] = (
                str(payload.get("requested_lane", normalized_lane)).strip()
                if normalized_lane
                else ""
            )
            return payload
        if normalized_action == "start":
            payload = start_lanes_background(config, lane_id=normalized_lane)
            payload["action"] = "start"
            payload["lane"] = (
                str(payload.get("requested_lane", normalized_lane)).strip()
                if normalized_lane
                else ""
            )
            return payload
        if normalized_action == "stop":
            payload = stop_lanes_background(config, lane_id=normalized_lane)
            payload["action"] = "stop"
            payload["lane"] = (
                str(payload.get("requested_lane", normalized_lane)).strip()
                if normalized_lane
                else ""
            )
            if "ok" not in payload:
                payload["ok"] = int(payload.get("failed_count", 0)) == 0
            payload.setdefault("failed_count", 0)
            return payload
        return {
            "ok": False,
            "action": normalized_action,
            "lane": normalized_lane or "",
            "error": "unsupported action",
            "supported_actions": ["status", "ensure", "start", "stop"],
        }
    except Exception as err:
        return {
            "ok": False,
            "action": normalized_action,
            "lane": normalized_lane or "",
            "error": str(err),
        }


def _lane_action_http_status(payload: dict[str, Any]) -> HTTPStatus:
    action = str(payload.get("action", "")).strip().lower()
    if action == "status" and not str(payload.get("error", "")).strip():
        return HTTPStatus.OK
    if bool(payload.get("ok", False)):
        return HTTPStatus.OK
    error = str(payload.get("error", "")).strip().lower()
    if error == "unsupported action":
        return HTTPStatus.BAD_REQUEST
    if error.startswith("unknown lane id "):
        return HTTPStatus.NOT_FOUND
    return HTTPStatus.SERVICE_UNAVAILABLE


def _lane_error_matches_requested_lane(
    error: str,
    requested_lane: str,
    *,
    known_lane_ids: set[str] | None = None,
) -> bool:
    message = str(error).strip()
    lane = requested_lane.strip().lower()
    if not message or not lane:
        return True
    if ":" not in message:
        return True
    prefix = message.split(":", 1)[0].strip().lower()
    if prefix == lane:
        return True
    if known_lane_ids and prefix in known_lane_ids:
        return False
    return True


def _normalize_lane_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    lane = dict(raw)
    lane["id"] = str(lane.get("id", "")).strip() or "unknown"
    lane["owner"] = str(lane.get("owner", "")).strip() or "unknown"
    lane["running"] = bool(lane.get("running", False))
    lane["pid"] = lane.get("pid")
    lane["health"] = str(lane.get("health", "")).strip().lower() or "unknown"
    lane["error"] = str(lane.get("error", "")).strip()
    state_counts = lane.get("state_counts", {})
    lane["state_counts"] = state_counts if isinstance(state_counts, dict) else {}
    try:
        lane["heartbeat_age_sec"] = int(lane.get("heartbeat_age_sec", -1))
    except (TypeError, ValueError):
        lane["heartbeat_age_sec"] = -1
    lane["build_current"] = bool(lane.get("build_current", False))
    return lane


def _resolve_lane_filter(lane_items: list[dict[str, Any]], requested_lane: str) -> str:
    lane_filter = requested_lane.strip()
    if not lane_filter:
        return ""
    known_ids = sorted(
        {
            str(item.get("id", "")).strip()
            for item in lane_items
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
    )
    if lane_filter in known_ids:
        return lane_filter
    folded_matches = [lane_id for lane_id in known_ids if lane_id.lower() == lane_filter.lower()]
    if len(folded_matches) == 1:
        return folded_matches[0]
    return lane_filter


def _filter_lane_status_payload(payload: dict[str, Any], *, lane_id: str = "") -> dict[str, Any]:
    requested_lane_raw = lane_id.strip()
    raw_lane_items = payload.get("lanes", [])
    if not isinstance(raw_lane_items, list):
        raw_lane_items = []
    lane_items = [item for item in (_normalize_lane_entry(raw) for raw in raw_lane_items) if item is not None]
    requested_lane = _resolve_lane_filter(lane_items, requested_lane_raw)
    requested_lane_normalized = requested_lane.lower()
    known_lane_ids = {
        str(item.get("id", "")).strip().lower()
        for item in lane_items
        if str(item.get("id", "")).strip()
    }
    normalized_errors = _normalize_error_messages(payload.get("errors", []))
    suppressed_errors: list[str] = []

    if requested_lane:
        lane_items = [lane for lane in lane_items if str(lane.get("id", "")).strip().lower() == requested_lane_normalized]
        lane_specific_errors = [
            item
            for item in normalized_errors
            if _lane_error_matches_requested_lane(item, requested_lane, known_lane_ids=known_lane_ids)
        ]
        suppressed_errors = [item for item in normalized_errors if item not in lane_specific_errors]
        if lane_items:
            normalized_errors = lane_specific_errors
        else:
            normalized_errors = lane_specific_errors or normalized_errors
            if normalized_errors:
                normalized_errors.append(
                    f"Requested lane {requested_lane!r} is unavailable because lane status sources failed."
                )
            else:
                lanes_file = str(payload.get("lanes_file", "")).strip()
                normalized_errors.append(f"Unknown lane id {requested_lane!r}. Update {lanes_file}.")

    health_counts: dict[str, int] = {}
    owner_counts: dict[str, dict[str, int]] = {}
    healthy_states = {"ok", "paused", "idle"}
    for lane in lane_items:
        if not isinstance(lane, dict):
            continue
        health = str(lane.get("health", "unknown")).strip().lower() or "unknown"
        health_counts[health] = health_counts.get(health, 0) + 1
        owner = str(lane.get("owner", "unknown")).strip() or "unknown"
        owner_entry = owner_counts.setdefault(owner, {"total": 0, "running": 0, "healthy": 0, "degraded": 0})
        owner_entry["total"] += 1
        if bool(lane.get("running", False)):
            owner_entry["running"] += 1
        if health in healthy_states:
            owner_entry["healthy"] += 1
        else:
            owner_entry["degraded"] += 1

    filtered = dict(payload)
    filtered["requested_lane"] = requested_lane or "all"
    filtered["lanes"] = lane_items
    filtered["running_count"] = sum(1 for lane in lane_items if bool(lane.get("running", False)))
    filtered["total_count"] = len(lane_items)
    filtered["health_counts"] = health_counts
    filtered["owner_counts"] = owner_counts
    filtered["errors"] = normalized_errors
    filtered["suppressed_errors"] = suppressed_errors
    if requested_lane and lane_items and not normalized_errors:
        filtered["partial"] = False
        filtered["ok"] = True
    else:
        filtered["partial"] = bool(payload.get("partial", False)) or bool(normalized_errors)
        filtered["ok"] = bool(payload.get("ok", not normalized_errors)) and not bool(normalized_errors)
    return filtered


def _safe_monitor_snapshot(config: ManagerConfig) -> dict:
    try:
        return monitor_snapshot(config)
    except Exception as err:
        message = str(err)
        lane_payload: dict[str, Any] = {
            "timestamp": "",
            "lanes_file": "",
            "running_count": 0,
            "total_count": 0,
            "lanes": [],
            "health_counts": {},
            "owner_counts": {},
            "partial": True,
            "ok": False,
            "errors": [],
        }
        try:
            lane_candidate = _safe_lane_status_snapshot(config)
            if isinstance(lane_candidate, dict):
                lane_payload = lane_candidate
        except Exception as lane_err:
            lane_payload["errors"] = [f"lane status fallback error: {lane_err}"]

        conversation_payload: dict[str, Any] = {
            "total_events": 0,
            "events": [],
            "owner_counts": {},
            "sources": [],
            "partial": True,
            "ok": False,
            "errors": [],
        }
        try:
            conv_candidate = _safe_conversations_snapshot(config, lines=60, include_lanes=True)
            if isinstance(conv_candidate, dict):
                conversation_payload = conv_candidate
        except Exception as conv_err:
            conversation_payload["errors"] = [f"conversation fallback error: {conv_err}"]

        lane_items = lane_payload.get("lanes", [])
        if not isinstance(lane_items, list):
            lane_items = []
        lane_items = [item for item in lane_items if isinstance(item, dict)]

        lane_errors = lane_payload.get("errors", [])
        if not isinstance(lane_errors, list):
            lane_errors = [str(lane_errors)] if str(lane_errors).strip() else []
        lane_errors = [str(item).strip() for item in lane_errors if str(item).strip()]

        lane_health_counts = lane_payload.get("health_counts", {})
        if not isinstance(lane_health_counts, dict):
            lane_health_counts = {}
        lane_owner_counts = lane_payload.get("owner_counts", {})
        if not isinstance(lane_owner_counts, dict):
            lane_owner_counts = {}

        conversation_events = conversation_payload.get("events", [])
        if not isinstance(conversation_events, list):
            conversation_events = []
        normalized_conversation_events = [item for item in conversation_events if isinstance(item, dict)]
        normalized_conversation_events = sorted(normalized_conversation_events, key=_conversation_event_sort_key)
        recent_events = normalized_conversation_events[-20:]

        conversation_errors = conversation_payload.get("errors", [])
        if not isinstance(conversation_errors, list):
            conversation_errors = [str(conversation_errors)] if str(conversation_errors).strip() else []
        conversation_errors = [str(item).strip() for item in conversation_errors if str(item).strip()]

        conversation_sources = conversation_payload.get("sources", [])
        if not isinstance(conversation_sources, list):
            conversation_sources = []
        normalized_conversation_sources = [item for item in conversation_sources if isinstance(item, dict)]
        source_error_count = sum(1 for item in normalized_conversation_sources if str(item.get("error", "")).strip())
        source_missing_count = sum(1 for item in normalized_conversation_sources if bool(item.get("missing", False)))
        source_recoverable_missing_count = sum(
            1 for item in normalized_conversation_sources if bool(item.get("recoverable_missing", False))
        )
        source_fallback_count = sum(1 for item in normalized_conversation_sources if bool(item.get("fallback_used", False)))

        conversation_owner_counts = conversation_payload.get("owner_counts", {})
        if not isinstance(conversation_owner_counts, dict):
            conversation_owner_counts = {}

        status_payload: dict[str, Any] = {
            "supervisor_running": False,
            "runner_running": False,
            "heartbeat_age_sec": -1,
            "heartbeat_stale_threshold_sec": 0,
            "runner_pid": None,
            "supervisor_pid": None,
        }
        status_error = ""
        try:
            status_candidate = status_snapshot(config)
            if isinstance(status_candidate, dict):
                for key in status_payload:
                    status_payload[key] = status_candidate.get(key, status_payload[key])
            else:
                status_error = f"unexpected status payload type: {type(status_candidate).__name__}"
        except Exception as status_err:
            status_error = str(status_err)

        diagnostics_sources = {
            "monitor": {"ok": False, "error": message},
            "status": {"ok": status_error == "", "error": status_error},
            "lanes": {"ok": bool(lane_payload.get("ok", False)), "error": "; ".join(lane_errors)},
            "conversations": {
                "ok": bool(conversation_payload.get("ok", False)),
                "error": "; ".join(conversation_errors),
            },
            "response_metrics": {"ok": False, "error": message},
            "implementation_repo": {"ok": False, "error": message},
            "tests_repo": {"ok": False, "error": message},
            "logs": {"ok": False, "error": message},
            "handoffs": {"ok": False, "error": message},
        }
        diagnostics_errors = [f"monitor: {message}"]
        for source_name, source_info in diagnostics_sources.items():
            if source_name == "monitor":
                continue
            if source_info["ok"]:
                continue
            error_text = str(source_info.get("error", "")).strip()
            if error_text:
                diagnostics_errors.append(f"{source_name}: {error_text}")

        progress_counts = {"done": 0, "in_progress": 0, "pending": 0, "blocked": 0, "unknown": 0}
        active_tasks: list[str] = []
        blocked_tasks: list[str] = []
        for lane in lane_items:
            state_counts = lane.get("state_counts", {})
            if not isinstance(state_counts, dict):
                continue
            lane_id = str(lane.get("id", "")).strip() or "unknown"
            for key in progress_counts:
                if key == "unknown":
                    continue
                try:
                    progress_counts[key] += int(state_counts.get(key, 0))
                except (TypeError, ValueError):
                    continue
            try:
                if int(state_counts.get("in_progress", 0)) > 0:
                    active_tasks.append(f"lane:{lane_id}")
            except (TypeError, ValueError):
                pass
            try:
                if int(state_counts.get("blocked", 0)) > 0:
                    blocked_tasks.append(f"lane:{lane_id}")
            except (TypeError, ValueError):
                pass
        if sum(progress_counts.values()) == 0:
            progress_counts["unknown"] = 1

        running_count = sum(1 for lane in lane_items if bool(lane.get("running", False)))
        lanes_snapshot_payload = _augment_lane_payload_with_conversation_rollup(
            {
                **lane_payload,
                "running_count": running_count,
                "total_count": len(lane_items),
                "lanes": lane_items,
                "health_counts": lane_health_counts,
                "owner_counts": lane_owner_counts,
                "partial": bool(lane_payload.get("partial", False)) or bool(lane_errors),
                "ok": bool(lane_payload.get("ok", False)) and not bool(lane_errors),
                "errors": lane_errors,
            },
            conversation_payload,
        )
        runtime_lane_items = lanes_snapshot_payload.get("lanes", [])
        if not isinstance(runtime_lane_items, list):
            runtime_lane_items = []
        runtime_lane_items = [item for item in runtime_lane_items if isinstance(item, dict)]
        runtime_health_counts, runtime_owner_counts, runtime_running_count = _lane_owner_health_counts(runtime_lane_items)
        lane_operational_states = {"ok", "paused", "idle"}
        runtime_operational_count = 0
        runtime_degraded_count = 0
        for lane in runtime_lane_items:
            health = str(lane.get("health", "unknown")).strip().lower()
            if health in lane_operational_states:
                runtime_operational_count += 1
            else:
                runtime_degraded_count += 1
        primary_runner_running = bool(status_payload.get("runner_running", False))
        return {
            "timestamp": "",
            "latest_log_line": f"monitor snapshot error: {message}",
            "status": status_payload,
            "progress": {
                "counts": progress_counts,
                "active_tasks": sorted(set(active_tasks)),
                "blocked_tasks": sorted(set(blocked_tasks)),
                "completed_last_24h": 0,
                "completed_last_24h_unique_tasks": 0,
                "completed_last_24h_by_owner": {},
                "completed_last_24h_window_start": "",
                "completed_last_24h_window_end": "",
                "completed_last_24h_sources_scanned": 0,
                "completed_last_24h_errors": [],
                "source": "fallback_partial",
            },
            "lanes": lanes_snapshot_payload,
            "runtime": {
                "primary_runner_running": primary_runner_running,
                "lane_agents_running": runtime_running_count > 0,
                "effective_agents_running": primary_runner_running or runtime_running_count > 0,
                "lane_operational_count": runtime_operational_count,
                "lane_degraded_count": runtime_degraded_count,
                "lane_health_counts": runtime_health_counts,
                "lane_owner_health": runtime_owner_counts,
                "push_recovery_events": {
                    "recent_total": 0,
                    "auto_push_race_recovered": 0,
                    "task_push_race_recovered": 0,
                    "latest": {},
                },
            },
            "conversations": {
                "ok": bool(conversation_payload.get("ok", False)),
                "total_events": conversation_payload.get("total_events", len(normalized_conversation_events)),
                "owner_counts": conversation_owner_counts,
                "latest": (recent_events[-1] if recent_events else {}),
                "recent_events": recent_events,
                "partial": bool(conversation_payload.get("partial", False)) or bool(conversation_errors),
                "errors": conversation_errors,
                "sources": normalized_conversation_sources,
                "source_error_count": source_error_count,
                "source_missing_count": source_missing_count,
                "source_recoverable_missing_count": source_recoverable_missing_count,
                "source_fallback_count": source_fallback_count,
            },
            "repos": {
                "implementation": {"ok": False, "error": message},
                "tests": {"ok": False, "error": message},
            },
            "response_metrics": {
                "responses_total": 0,
                "first_time_pass_rate": 0.0,
                "acceptance_pass_rate": 0.0,
                "latency_sec_avg": 0.0,
                "prompt_difficulty_score_avg": 0.0,
                "cost_usd_total": 0.0,
                "exact_cost_coverage": 0.0,
                "tokens_total": 0,
                "estimated_tokens_total": 0,
                "token_rate_per_minute": 0.0,
                "estimated_cost_per_million_tokens": 0.0,
                "by_owner": {},
                "exciting_stat": {
                    "label": "Awaiting Data",
                    "value": "0",
                    "detail": "No response metrics recorded yet.",
                    "kind": "idle",
                },
                "optimization_recommendations": [],
                "ok": False,
                "errors": [message],
            },
            "diagnostics": {
                "ok": False,
                "errors": diagnostics_errors,
                "sources": diagnostics_sources,
            },
            "monitor_file": "",
        }


def _safe_lane_status_snapshot(config: ManagerConfig) -> dict:
    try:
        return lane_status_snapshot(config)
    except Exception as err:
        message = str(err)
        try:
            return lane_status_fallback_snapshot(config, error=message)
        except Exception:
            return {
                "timestamp": "",
                "lanes_file": str(config.lanes_file),
                "running_count": 0,
                "total_count": 0,
                "lanes": [],
                "health_counts": {},
                "owner_counts": {},
                "partial": True,
                "ok": False,
                "errors": [message],
            }


def _safe_conversations_snapshot(
    config: ManagerConfig,
    *,
    lines: int = 200,
    include_lanes: bool = True,
    owner: str = "",
    lane_id: str = "",
    event_type: str = "",
    contains: str = "",
    tail: int = 0,
) -> dict:
    try:
        payload = conversations_snapshot(config, lines=lines, include_lanes=include_lanes)
    except Exception as err:
        message = str(err)
        source_path = Path(config.conversation_log_file)
        source_missing = not source_path.exists()
        payload = {
            "timestamp": "",
            "conversation_files": [str(source_path)],
            "total_events": 0,
            "events": [],
            "owner_counts": {},
            "sources": [
                {
                    "path": str(source_path),
                    "resolved_path": str(source_path),
                    "kind": "primary",
                    "resolved_kind": "primary",
                    "lane_id": "",
                    "owner": "",
                    "ok": False,
                    "missing": source_missing,
                    "recoverable_missing": False,
                    "fallback_used": False,
                    "error": message,
                    "event_count": 0,
                }
            ],
            "partial": True,
            "ok": False,
            "errors": [message],
            "unfiltered_total_events": 0,
            "filters": {
                "owner": owner.strip(),
                "lane": lane_id.strip(),
                "event_type": event_type.strip(),
                "contains": contains.strip(),
                "tail": max(0, int(tail)),
            },
        }
    payload = _filter_conversation_payload_for_lane(payload, lane_id=lane_id)
    return _apply_conversation_filters(
        payload,
        owner=owner,
        lane_id=lane_id,
        event_type=event_type,
        contains=contains,
        tail=tail,
    )


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _watchdog_latest_existing_path(candidates: list[Path]) -> Path | None:
    selected_path: Path | None = None
    selected_rank: tuple[int, int] | None = None
    for index, candidate in enumerate(candidates):
        try:
            if not candidate.exists() or not candidate.is_file():
                continue
            rank = (int(candidate.stat().st_mtime_ns), -index)
        except OSError:
            continue
        if selected_rank is None or rank > selected_rank:
            selected_rank = rank
            selected_path = candidate
    return selected_path


def _watchdog_state_path(config: ManagerConfig) -> Path:
    env_path = os.environ.get("ORXAQ_AUTONOMY_PROCESS_WATCHDOG_STATE_FILE", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()

    home_default = (Path.home() / ".codex" / "autonomy" / "process-watchdog-state.json").resolve()
    artifacts_dir = Path(getattr(config, "artifacts_dir", Path.cwd())).resolve()
    artifacts_default = (artifacts_dir / "process-watchdog-state.json").resolve()
    selected = _watchdog_latest_existing_path([home_default, artifacts_default])
    if selected is not None:
        return selected
    return home_default


def _watchdog_history_path(config: ManagerConfig, state_path: Path) -> Path:
    env_path = os.environ.get("ORXAQ_AUTONOMY_PROCESS_WATCHDOG_HISTORY_FILE", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()

    sibling_default = state_path.with_name("process-watchdog-history.ndjson").resolve()
    home_default = (Path.home() / ".codex" / "autonomy" / "process-watchdog-history.ndjson").resolve()
    artifacts_dir = Path(getattr(config, "artifacts_dir", Path.cwd())).resolve()
    artifacts_default = (artifacts_dir / "process-watchdog-history.ndjson").resolve()
    selected = _watchdog_latest_existing_path([sibling_default, home_default, artifacts_default])
    if selected is not None:
        return selected
    return sibling_default


def _watchdog_normalize_pid(value: Any) -> int | None:
    pid = _coerce_int(value, default=-1)
    if pid <= 0:
        return None
    return pid


def _watchdog_load_recent_events(history_path: Path, events: int) -> list[dict[str, Any]]:
    if not history_path.exists() or not history_path.is_file():
        return []
    try:
        lines = history_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    normalized: list[dict[str, Any]] = []
    for raw_line in lines[-max(1, events) :]:
        text = raw_line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "time": str(item.get("time") or item.get("timestamp") or "").strip(),
                "id": str(item.get("id") or "").strip(),
                "status": str(item.get("status") or "").strip(),
                "pid": _watchdog_normalize_pid(item.get("pid")),
                "reason": str(item.get("reason") or "").strip(),
                "restart_returncode": item.get("restart_returncode"),
            }
        )
    return normalized


def _safe_watchdog_snapshot(config: ManagerConfig, *, events: int = 40) -> dict[str, Any]:
    state_path = _watchdog_state_path(config)
    history_path = _watchdog_history_path(config, state_path)
    state_exists = state_path.exists() and state_path.is_file()
    history_exists = history_path.exists() and history_path.is_file()

    critical_errors: list[str] = []
    warning_errors: list[str] = []

    raw_state: dict[str, Any] = {}
    if state_exists:
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw_state = loaded
            else:
                critical_errors.append(f"watchdog state is not an object: {state_path}")
        except Exception as err:
            critical_errors.append(f"watchdog state parse error: {err}")
    else:
        critical_errors.append(f"watchdog state file not found: {state_path}")

    if not history_exists:
        warning_errors.append(f"watchdog history file not found: {history_path}")

    process_items: list[dict[str, Any]] = []
    raw_processes = raw_state.get("processes", {})
    if isinstance(raw_processes, dict):
        for process_id, payload in raw_processes.items():
            process_name = str(process_id).strip()
            if not process_name:
                continue
            item = payload if isinstance(payload, dict) else {}
            process_items.append(
                {
                    "id": process_name,
                    "pid": _watchdog_normalize_pid(item.get("last_pid")),
                    "status": str(item.get("last_status") or "unknown").strip() or "unknown",
                    "last_checked_at": str(item.get("last_checked_at") or "").strip(),
                    "last_restart_at": str(item.get("last_restart_at") or "").strip(),
                    "last_restart_rc": item.get("last_restart_rc"),
                    "reason": str(item.get("last_reason") or "").strip(),
                    "checks_total": max(0, _coerce_int(item.get("checks_total"), default=0)),
                    "healthy_checks": max(0, _coerce_int(item.get("healthy_checks"), default=0)),
                    "unhealthy_checks": max(0, _coerce_int(item.get("unhealthy_checks"), default=0)),
                    "restart_attempts": max(0, _coerce_int(item.get("restart_attempts"), default=0)),
                    "restart_successes": max(0, _coerce_int(item.get("restart_successes"), default=0)),
                    "restart_failures": max(0, _coerce_int(item.get("restart_failures"), default=0)),
                }
            )
    process_items.sort(key=lambda item: item["id"])

    problematic_statuses = {"restart_failed", "down_cooldown", "down_no_restart"}
    healthy_statuses = {"healthy", "restarted"}
    healthy_count = sum(1 for item in process_items if str(item.get("status", "")).lower() in healthy_statuses)
    restarted_count = sum(1 for item in process_items if str(item.get("status", "")).lower() == "restarted")
    problematic_ids = [
        str(item.get("id", ""))
        for item in process_items
        if str(item.get("status", "")).lower() in problematic_statuses
    ]
    restart_attempts_total = sum(_coerce_int(item.get("restart_attempts"), default=0) for item in process_items)
    restart_successes_total = sum(_coerce_int(item.get("restart_successes"), default=0) for item in process_items)
    restart_failures_total = sum(_coerce_int(item.get("restart_failures"), default=0) for item in process_items)

    last_run_at = str(raw_state.get("last_run_at") or "").strip()
    parsed_last_run = _parse_event_timestamp(last_run_at)
    if parsed_last_run is None:
        last_run_age_sec = -1
    else:
        last_run_age_sec = max(0, int((datetime.now(timezone.utc) - parsed_last_run).total_seconds()))

    recent_events = _watchdog_load_recent_events(history_path, events=events)
    all_errors = [*critical_errors, *warning_errors]
    return {
        "ok": not critical_errors,
        "partial": bool(all_errors),
        "errors": all_errors,
        "state_file": str(state_path),
        "history_file": str(history_path),
        "state_exists": state_exists,
        "history_exists": history_exists,
        "runs_total": max(0, _coerce_int(raw_state.get("runs_total"), default=0)),
        "last_run_at": last_run_at,
        "last_run_age_sec": last_run_age_sec,
        "total_processes": len(process_items),
        "healthy_count": healthy_count,
        "restarted_count": restarted_count,
        "problematic_count": len(problematic_ids),
        "problematic_ids": problematic_ids,
        "restart_attempts_total": restart_attempts_total,
        "restart_successes_total": restart_successes_total,
        "restart_failures_total": restart_failures_total,
        "processes": process_items,
        "recent_events": recent_events,
    }


def _read_tail_json_objects(path: Path, *, max_lines: int = 240) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in lines[-max(1, max_lines) :]:
        text = raw.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _lane_last_event_task_id(last_event: dict[str, Any]) -> str:
    task_id = str(last_event.get("task_id", "")).strip()
    if task_id:
        return task_id
    payload = last_event.get("payload")
    if isinstance(payload, dict):
        task_id = str(payload.get("task_id", "")).strip()
        if task_id:
            return task_id
    meta = last_event.get("meta")
    if isinstance(meta, dict):
        task_id = str(meta.get("task_id", "")).strip()
        if task_id:
            return task_id
    return ""


def _load_task_catalog(tasks_file: Path) -> list[dict[str, str]]:
    if not tasks_file.exists() or not tasks_file.is_file():
        return []
    try:
        raw = json.loads(tasks_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id", "")).strip()
        if not task_id:
            continue
        title = str(item.get("title", "")).strip()
        out.append({"id": task_id, "title": title})
    return out


def _load_lane_state_status(state_file: Path) -> dict[str, str]:
    if not state_file.exists() or not state_file.is_file():
        return {}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for task_id, payload in raw.items():
        task_key = str(task_id).strip()
        if not task_key:
            continue
        status = ""
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).strip().lower()
        out[task_key] = status
    return out


def _resolve_lane_work_title(lane: dict[str, Any]) -> tuple[str, str]:
    tasks_path_raw = str(lane.get("tasks_file", "")).strip()
    lane_description = str(lane.get("description", "")).strip()
    lane_label = str(lane.get("id", "")).strip() or "lane"
    if not tasks_path_raw:
        return (lane_description or lane_label, "")

    task_catalog = _load_task_catalog(Path(tasks_path_raw))
    if not task_catalog:
        return (lane_description or lane_label, "")
    title_by_id = {item["id"]: item["title"] for item in task_catalog}
    task_order = [item["id"] for item in task_catalog]

    last_event = lane.get("last_event")
    if isinstance(last_event, dict):
        task_id = _lane_last_event_task_id(last_event)
        if task_id and task_id in title_by_id:
            title = title_by_id.get(task_id, "").strip() or task_id
            return (title, task_id)

    events_path_raw = str(lane.get("events_file", "")).strip()
    state_candidates: list[Path] = []
    if events_path_raw:
        events_path = Path(events_path_raw)
        state_candidates.append(events_path.with_name("state.json"))
        state_candidates.append(events_path.parent / "state.json")
    state_map: dict[str, str] = {}
    for candidate in state_candidates:
        state_map = _load_lane_state_status(candidate)
        if state_map:
            break

    if state_map:
        for preferred in ["in_progress", "blocked", "pending", "done"]:
            for task_id in task_order:
                if state_map.get(task_id, "") == preferred:
                    title = title_by_id.get(task_id, "").strip() or task_id
                    return (title, task_id)

    first_task = task_order[0]
    title = title_by_id.get(first_task, "").strip() or first_task
    return (title, first_task)


def _pid_elapsed_seconds(pid: int | None) -> int:
    if pid is None or pid <= 0:
        return -1
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etimes="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return -1
    if proc.returncode != 0:
        return -1
    return _coerce_int((proc.stdout or "").strip(), default=-1)


def _lane_running_age_seconds(lane: dict[str, Any], now: datetime) -> int:
    pid = _watchdog_normalize_pid(lane.get("pid"))
    elapsed = _pid_elapsed_seconds(pid)
    if elapsed >= 0:
        return elapsed
    meta = lane.get("meta")
    started_at = ""
    if isinstance(meta, dict):
        started_at = str(meta.get("started_at", "")).strip()
    parsed = _parse_event_timestamp(started_at)
    if parsed is None:
        return -1
    return max(0, int((now - parsed).total_seconds()))


def _lane_health_confirmation(lane: dict[str, Any], now: datetime) -> tuple[str, int]:
    heartbeat_age = _coerce_int(lane.get("heartbeat_age_sec"), default=-1)
    if heartbeat_age >= 0:
        confirmed_at = (now - timedelta(seconds=heartbeat_age)).replace(microsecond=0)
        return (confirmed_at.isoformat().replace("+00:00", "Z"), heartbeat_age)
    last_event = lane.get("last_event")
    if isinstance(last_event, dict):
        parsed = _parse_event_timestamp(last_event.get("timestamp"))
        if parsed is not None:
            age = max(0, int((now - parsed).total_seconds()))
            return (parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z"), age)
    return ("", -1)


def _git_recent_commit_count(repo: Path, *, since_iso: str, pathspecs: list[str]) -> int:
    cmd = ["git", "-C", str(repo), "rev-list", "--count", f"--since={since_iso}", "HEAD"]
    if pathspecs:
        cmd.extend(["--", *pathspecs])
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
    except Exception:
        return -1
    if proc.returncode != 0:
        if pathspecs:
            return _git_recent_commit_count(repo, since_iso=since_iso, pathspecs=[])
        return -1
    return _coerce_int((proc.stdout or "").strip(), default=-1)


def _lane_git_scope(lane: dict[str, Any]) -> tuple[Path | None, list[str]]:
    repo_raw = str(lane.get("impl_repo", "")).strip()
    if not repo_raw:
        return (None, [])
    repo = Path(repo_raw).expanduser().resolve()
    if not repo.exists() or not repo.is_dir():
        return (None, [])

    pathspecs: list[str] = []
    raw_paths = lane.get("exclusive_paths", [])
    if isinstance(raw_paths, list):
        for entry in raw_paths:
            raw = str(entry).strip()
            if not raw:
                continue
            if any(token in raw for token in ["*", "?", "["]):
                pathspecs.append(f":(glob){raw}")
            else:
                pathspecs.append(raw)
    return (repo, pathspecs)


def _lane_commit_count_last_hour(lane: dict[str, Any], now: datetime) -> int:
    repo, pathspecs = _lane_git_scope(lane)
    if repo is None:
        return -1

    cache_key = (str(repo), tuple(pathspecs))
    now_epoch = time.time()
    cached = _COMMIT_COUNT_CACHE.get(cache_key)
    if cached and (now_epoch - cached[0]) <= _COMMIT_CACHE_TTL_SEC:
        return cached[1]

    since_iso = (now - timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    count = _git_recent_commit_count(repo, since_iso=since_iso, pathspecs=pathspecs)
    _COMMIT_COUNT_CACHE[cache_key] = (now_epoch, count)
    return count


def _git_recent_commit_timestamps(repo: Path, *, since_iso: str, pathspecs: list[str]) -> list[int]:
    cmd = ["git", "-C", str(repo), "log", f"--since={since_iso}", "--pretty=format:%ct", "HEAD"]
    if pathspecs:
        cmd.extend(["--", *pathspecs])
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    if proc.returncode != 0:
        if pathspecs:
            return _git_recent_commit_timestamps(repo, since_iso=since_iso, pathspecs=[])
        return []
    out: list[int] = []
    for line in (proc.stdout or "").splitlines():
        parsed = _coerce_int(line.strip(), default=-1)
        if parsed > 0:
            out.append(parsed)
    return out


def _lane_commit_velocity_metrics(lane: dict[str, Any], now: datetime) -> dict[str, Any]:
    bucket_count = max(1, int(_COLLAB_ACTIVITY_BUCKETS))
    bucket_sec = max(60, int(_COLLAB_ACTIVITY_BUCKET_SEC))
    default_payload = {
        "commits_last_hour_from_bins": 0,
        "commit_bins_5m": [0 for _ in range(bucket_count)],
        "commit_bins_max": 0,
        "commit_velocity_level": 0.0,
        "latest_commit_at": "",
        "latest_commit_age_sec": -1,
    }

    repo, pathspecs = _lane_git_scope(lane)
    if repo is None:
        return default_payload

    cache_key = (str(repo), tuple(pathspecs), bucket_count, bucket_sec)
    now_epoch = time.time()
    cached = _COMMIT_TIMELINE_CACHE.get(cache_key)
    if cached and (now_epoch - cached[0]) <= _COMMIT_CACHE_TTL_SEC:
        return cached[1]

    window_sec = bucket_count * bucket_sec
    since_iso = (now - timedelta(seconds=window_sec)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    timestamps = _git_recent_commit_timestamps(repo, since_iso=since_iso, pathspecs=pathspecs)

    bins = [0 for _ in range(bucket_count)]
    latest_commit_at = ""
    latest_commit_age_sec = -1
    latest_commit_dt: datetime | None = None
    if timestamps:
        latest_epoch = max(timestamps)
        latest_commit_dt = datetime.fromtimestamp(float(latest_epoch), tz=timezone.utc)
        latest_commit_at = latest_commit_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        latest_commit_age_sec = max(0, int((now - latest_commit_dt).total_seconds()))

    for stamp in timestamps:
        commit_dt = datetime.fromtimestamp(float(stamp), tz=timezone.utc)
        age_sec = (now - commit_dt).total_seconds()
        if age_sec < 0:
            age_sec = 0
        if age_sec > window_sec:
            continue
        slot = bucket_count - 1 - int(age_sec // bucket_sec)
        if 0 <= slot < bucket_count:
            bins[slot] += 1

    total_commits = int(sum(bins))
    max_bin = int(max(bins)) if bins else 0
    velocity_level = min(1.0, max(total_commits / 10.0, max_bin / 4.0))
    if total_commits > 0:
        velocity_level = max(0.2, velocity_level)

    payload = {
        "commits_last_hour_from_bins": total_commits,
        "commit_bins_5m": bins,
        "commit_bins_max": max_bin,
        "commit_velocity_level": round(velocity_level, 3),
        "latest_commit_at": latest_commit_at,
        "latest_commit_age_sec": latest_commit_age_sec,
    }
    _COMMIT_TIMELINE_CACHE[cache_key] = (now_epoch, payload)
    return payload


def _lane_signal_metrics(lane: dict[str, Any], now: datetime) -> dict[str, Any]:
    bucket_count = max(1, int(_COLLAB_ACTIVITY_BUCKETS))
    bucket_sec = max(60, int(_COLLAB_ACTIVITY_BUCKET_SEC))
    window_sec = bucket_count * bucket_sec

    paths: list[Path] = []
    events_file_raw = str(lane.get("events_file", "")).strip()
    if events_file_raw:
        paths.append(Path(events_file_raw))
    meta = lane.get("meta")
    if isinstance(meta, dict):
        conversation_file_raw = str(meta.get("conversation_log_file", "")).strip()
        if conversation_file_raw:
            paths.append(Path(conversation_file_raw))

    latest_signal: datetime | None = None
    latest_task_done: datetime | None = None
    latest_push: datetime | None = None
    events_15 = 0
    events_60 = 0
    events_300 = 0
    recent_error_events_10m = 0
    signal_bins = [0 for _ in range(bucket_count)]

    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        for event in _read_tail_json_objects(resolved, max_lines=900):
            parsed = _parse_event_timestamp(event.get("timestamp") or event.get("time") or "")
            if parsed is None:
                continue
            event_type = str(event.get("event_type", "")).strip().lower()
            if latest_signal is None or parsed >= latest_signal:
                latest_signal = parsed
            if event_type == "task_done" and (latest_task_done is None or parsed >= latest_task_done):
                latest_task_done = parsed
            if event_type == "auto_push" and (latest_push is None or parsed >= latest_push):
                latest_push = parsed
            age_sec = (now - parsed).total_seconds()
            if age_sec < 0:
                age_sec = 0
            if age_sec <= 15:
                events_15 += 1
            if age_sec <= 60:
                events_60 += 1
            if age_sec <= 300:
                events_300 += 1
            if age_sec <= 600 and event_type in {"task_blocked", "agent_error", "auto_push_error"}:
                recent_error_events_10m += 1
            if age_sec <= window_sec:
                slot = bucket_count - 1 - int(age_sec // bucket_sec)
                if 0 <= slot < bucket_count:
                    signal_bins[slot] += 1

    if latest_signal is None:
        latest_signal_at = ""
        latest_signal_age_sec = -1
    else:
        latest_signal_at = latest_signal.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        latest_signal_age_sec = max(0, int((now - latest_signal).total_seconds()))

    if latest_task_done is None:
        latest_task_done_at = ""
        latest_task_done_age_sec = -1
    else:
        latest_task_done_at = latest_task_done.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        latest_task_done_age_sec = max(0, int((now - latest_task_done).total_seconds()))

    if latest_push is None:
        latest_push_at = ""
        latest_push_age_sec = -1
    else:
        latest_push_at = latest_push.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        latest_push_age_sec = max(0, int((now - latest_push).total_seconds()))

    running = bool(lane.get("running", False))
    health = str(lane.get("health", "unknown")).strip().lower()
    if not running:
        live_state = "offline"
        live_label = "offline"
    elif latest_signal_age_sec != -1 and latest_signal_age_sec <= 20:
        live_state = "thinking"
        live_label = "thinking"
    elif latest_signal_age_sec != -1 and latest_signal_age_sec <= 90:
        live_state = "active"
        live_label = "active"
    elif health in {"stale", "error", "stopped_unexpected"}:
        live_state = "stale"
        live_label = "stale"
    else:
        live_state = "active" if running else "offline"
        live_label = "listening" if running else "offline"

    signal_level = min(1.0, max(events_15 / 6.0, events_60 / 24.0, events_300 / 80.0))
    if live_state == "thinking":
        signal_level = max(signal_level, 0.75)
    elif live_state == "active":
        signal_level = max(signal_level, 0.35)
    elif live_state == "stale":
        signal_level = max(signal_level, 0.15)

    return {
        "latest_signal_at": latest_signal_at,
        "latest_signal_age_sec": latest_signal_age_sec,
        "latest_task_done_at": latest_task_done_at,
        "latest_task_done_age_sec": latest_task_done_age_sec,
        "latest_push_at": latest_push_at,
        "latest_push_age_sec": latest_push_age_sec,
        "signal_events_15s": events_15,
        "signal_events_60s": events_60,
        "signal_events_300s": events_300,
        "recent_error_events_10m": recent_error_events_10m,
        "signal_bins_5m": signal_bins,
        "signal_bins_max": max(signal_bins) if signal_bins else 0,
        "signal_level": round(signal_level, 3),
        "live_state": live_state,
        "live_label": live_label,
    }


def _lane_attention_metrics(row: dict[str, Any]) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []

    running = bool(row.get("running", False))
    health = str(row.get("health", "unknown")).strip().lower()
    live_state = str(row.get("live_state", "offline")).strip().lower()
    health_age = _coerce_int(row.get("latest_health_confirmation_age_sec"), default=-1)
    signal_age = _coerce_int(row.get("latest_signal_age_sec"), default=-1)
    task_done_age = _coerce_int(row.get("latest_task_done_age_sec"), default=-1)
    push_age = _coerce_int(row.get("latest_push_age_sec"), default=-1)
    commits_last_hour = _coerce_int(row.get("commits_last_hour"), default=-1)
    signal_events_60 = _coerce_int(row.get("signal_events_60s"), default=0)
    recent_errors_10m = max(0, _coerce_int(row.get("recent_error_events_10m"), default=0))

    if not running:
        score += 60
        reasons.append("process offline")

    if health in {"error", "stopped_unexpected"}:
        score += 40
        reasons.append(f"health={health}")
    elif health == "stale":
        score += 28
        reasons.append("heartbeat stale")
    elif health in {"unknown", ""}:
        score += 12
        reasons.append("health unknown")

    if health_age > 300:
        score += 12
        reasons.append("health confirmation old")

    if live_state == "stale":
        score += 15
        reasons.append("live state stale")
    elif running and live_state == "offline":
        score += 20
        reasons.append("live state offline")

    if running:
        if signal_age == -1:
            score += 18
            reasons.append("no signal seen")
        elif signal_age > 180:
            score += 16
            reasons.append("signal lagging")
        elif signal_age > 90:
            score += 9
            reasons.append("signal cooling")

    if running and signal_events_60 == 0:
        score += 8
        reasons.append("no events in last 60s")

    if recent_errors_10m > 0:
        score += min(25, recent_errors_10m * 8)
        reasons.append(f"errors_10m={recent_errors_10m}")

    if running:
        if task_done_age == -1:
            score += 12
            reasons.append("no task completion signal")
        elif task_done_age > 3600:
            score += 14
            reasons.append("no task completion in 1h")
        elif task_done_age > 1800:
            score += 7
            reasons.append("task completion slowed")

        if commits_last_hour == 0:
            score += 6
            reasons.append("no recent commits")
        elif commits_last_hour < 0:
            score += 4
            reasons.append("commit telemetry unavailable")

    if commits_last_hour > 0:
        if push_age == -1:
            score += 8
            reasons.append("commits without auto-push signal")
        elif push_age > 3600:
            score += 5
            reasons.append("auto-push stale")

    score = min(100, max(0, int(score)))
    if score >= 70:
        level = "critical"
    elif score >= 40:
        level = "warn"
    elif score >= 20:
        level = "watch"
    else:
        level = "ok"

    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        deduped.append(reason)
    message = "; ".join(deduped[:3]) if deduped else "stable"
    rank = {"critical": 0, "warn": 1, "watch": 2, "ok": 3}.get(level, 3)

    return {
        "attention_score": score,
        "attention_level": level,
        "attention_rank": rank,
        "attention_message": message,
    }


def _safe_collab_runtime_snapshot(config: ManagerConfig) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    lane_payload = _safe_lane_status_snapshot(config)
    raw_rows = lane_payload.get("lanes", [])
    if not isinstance(raw_rows, list):
        raw_rows = []

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    lane_errors = lane_payload.get("errors", [])
    if isinstance(lane_errors, list):
        errors.extend(str(item).strip() for item in lane_errors if str(item).strip())
    elif str(lane_errors).strip():
        errors.append(str(lane_errors).strip())

    for raw_lane in raw_rows:
        if not isinstance(raw_lane, dict):
            continue
        lane_id = str(raw_lane.get("id", "lane")).strip() or "lane"
        ai = str(raw_lane.get("owner", "unknown")).strip() or "unknown"
        pid = _watchdog_normalize_pid(raw_lane.get("pid"))
        running = bool(raw_lane.get("running", False))
        health = str(raw_lane.get("health", "unknown")).strip() or "unknown"
        work_title, task_id = _resolve_lane_work_title(raw_lane)
        running_age_sec = _lane_running_age_seconds(raw_lane, now) if running else -1
        health_at, health_age_sec = _lane_health_confirmation(raw_lane, now)
        commits_last_hour = _lane_commit_count_last_hour(raw_lane, now)
        commit_velocity = _lane_commit_velocity_metrics(raw_lane, now)
        if commits_last_hour < 0:
            commits_last_hour = _coerce_int(commit_velocity.get("commits_last_hour_from_bins"), default=-1)
        signal = _lane_signal_metrics(raw_lane, now)
        row = {
            "lane_id": lane_id,
            "ai": ai,
            "pid": pid,
            "running": running,
            "health": health,
            "work_title": work_title,
            "task_id": task_id,
            "running_age_sec": running_age_sec,
            "latest_health_confirmation_at": health_at,
            "latest_health_confirmation_age_sec": health_age_sec,
            "commits_last_hour": commits_last_hour,
            **commit_velocity,
            **signal,
        }
        row.update(_lane_attention_metrics(row))
        rows.append(row)

    rows.sort(
        key=lambda item: (
            _coerce_int(item.get("attention_rank"), default=3),
            -_coerce_int(item.get("attention_score"), default=0),
            0 if bool(item.get("running", False)) else 1,
            str(item.get("ai", "unknown")).strip(),
            str(item.get("lane_id", "")).strip(),
        )
    )

    running_rows = sum(1 for item in rows if bool(item.get("running", False)))
    thinking_rows = sum(1 for item in rows if str(item.get("live_state", "")) == "thinking")
    active_rows = sum(1 for item in rows if str(item.get("live_state", "")) == "active")
    stale_rows = sum(1 for item in rows if str(item.get("live_state", "")) == "stale")
    offline_rows = sum(1 for item in rows if str(item.get("live_state", "")) == "offline")
    commits_total = sum(max(0, _coerce_int(item.get("commits_last_hour"), default=0)) for item in rows)
    critical_rows = sum(1 for item in rows if str(item.get("attention_level", "")) == "critical")
    warn_rows = sum(1 for item in rows if str(item.get("attention_level", "")) == "warn")
    watch_rows = sum(1 for item in rows if str(item.get("attention_level", "")) == "watch")
    attention_rows = critical_rows + warn_rows + watch_rows
    max_attention_score = max((_coerce_int(item.get("attention_score"), default=0) for item in rows), default=0)

    signal_bins_total = [0 for _ in range(max(1, int(_COLLAB_ACTIVITY_BUCKETS)))]
    commit_bins_total = [0 for _ in range(max(1, int(_COLLAB_ACTIVITY_BUCKETS)))]
    for item in rows:
        signal_bins = item.get("signal_bins_5m")
        if isinstance(signal_bins, list):
            for idx, value in enumerate(signal_bins_total):
                if idx >= len(signal_bins):
                    break
                signal_bins_total[idx] = value + max(0, _coerce_int(signal_bins[idx], default=0))
        commit_bins = item.get("commit_bins_5m")
        if isinstance(commit_bins, list):
            for idx, value in enumerate(commit_bins_total):
                if idx >= len(commit_bins):
                    break
                commit_bins_total[idx] = value + max(0, _coerce_int(commit_bins[idx], default=0))

    def _latest_timestamp_and_age(field: str) -> tuple[str, int]:
        parsed_values = [
            _parse_event_timestamp(item.get(field))
            for item in rows
            if str(item.get(field, "")).strip()
        ]
        normalized = [item for item in parsed_values if item is not None]
        if not normalized:
            return ("", -1)
        latest = max(normalized)
        return (
            latest.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            max(0, int((now - latest).total_seconds())),
        )

    latest_signal_at, latest_signal_age_sec = _latest_timestamp_and_age("latest_signal_at")
    latest_health_at, latest_health_age_sec = _latest_timestamp_and_age("latest_health_confirmation_at")
    latest_task_done_at, latest_task_done_age_sec = _latest_timestamp_and_age("latest_task_done_at")
    latest_push_at, latest_push_age_sec = _latest_timestamp_and_age("latest_push_at")
    latest_commit_at, latest_commit_age_sec = _latest_timestamp_and_age("latest_commit_at")

    top_attention = [
        f"{str(item.get('ai', 'unknown')).strip() or 'unknown'}:{str(item.get('lane_id', 'lane')).strip() or 'lane'} {str(item.get('attention_message', '')).strip()}"
        for item in rows
        if str(item.get("attention_level", "")).strip() in {"critical", "warn", "watch"}
    ][:3]

    return {
        "timestamp": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "ok": bool(lane_payload.get("ok", False)) and not errors,
        "partial": bool(lane_payload.get("partial", False)) or bool(errors),
        "errors": errors,
        "rows": rows,
        "summary": {
            "total_rows": len(rows),
            "running_rows": running_rows,
            "thinking_rows": thinking_rows,
            "active_rows": active_rows,
            "stale_rows": stale_rows,
            "offline_rows": offline_rows,
            "commits_last_hour_total": commits_total,
            "attention_rows": attention_rows,
            "critical_rows": critical_rows,
            "warn_rows": warn_rows,
            "watch_rows": watch_rows,
            "max_attention_score": max_attention_score,
            "top_attention": top_attention,
            "latest_signal_at": latest_signal_at,
            "latest_signal_age_sec": latest_signal_age_sec,
            "latest_health_at": latest_health_at,
            "latest_health_age_sec": latest_health_age_sec,
            "latest_task_done_at": latest_task_done_at,
            "latest_task_done_age_sec": latest_task_done_age_sec,
            "latest_push_at": latest_push_at,
            "latest_push_age_sec": latest_push_age_sec,
            "latest_commit_at": latest_commit_at,
            "latest_commit_age_sec": latest_commit_age_sec,
            "signal_bins_5m_total": signal_bins_total,
            "signal_bins_5m_max": max(signal_bins_total) if signal_bins_total else 0,
            "commit_bins_5m_total": commit_bins_total,
            "commit_bins_5m_max": max(commit_bins_total) if commit_bins_total else 0,
        },
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
