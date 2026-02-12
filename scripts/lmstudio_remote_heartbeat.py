#!/usr/bin/env python3
"""Continuously send tagged probe prompts to remote LM Studio endpoints."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path("config/local_model_fleet.json")
PREFERRED_MODELS = [
    "qwen2.5-coder-7b-instruct",
    "qwen/qwen2.5-coder-32b",
    "qwen/qwen3-coder-next",
    "deepseek-coder-v2-lite-instruct",
    "google/gemma-3-4b",
]

STOP = False


def _sig_handler(_sig: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _host_from_base_url(base_url: str) -> str:
    raw = base_url.strip().lower()
    raw = raw.replace("http://", "").replace("https://", "")
    return raw.split("/", 1)[0].split(":", 1)[0]


def _is_remote(base_url: str) -> bool:
    host = _host_from_base_url(base_url)
    if host in {"127.0.0.1", "localhost", "::1"}:
        return False
    return True


def _http_json(method: str, url: str, payload: dict[str, Any] | None, timeout_sec: int) -> tuple[bool, dict[str, Any], str, float]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer lm-studio"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=max(1, timeout_sec)) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        latency_ms = (time.monotonic() - started) * 1000.0
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return True, parsed, "", latency_ms
        return False, {}, "response_not_object", latency_ms
    except Exception as err:  # noqa: BLE001
        latency_ms = (time.monotonic() - started) * 1000.0
        return False, {}, str(err), latency_ms


def _candidate_models(base_url: str, timeout_sec: int) -> list[str]:
    ok, payload, _err, _lat = _http_json("GET", f"{base_url.rstrip('/')}/models", None, timeout_sec)
    if not ok:
        return []
    data = payload.get("data", [])
    available: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                mid = str(item.get("id", "")).strip()
                if mid:
                    available.append(mid)
    preferred: list[str] = []
    for pref in PREFERRED_MODELS:
        if pref in available:
            preferred.append(pref)
    if preferred:
        return preferred
    return available[:3]


def _choose_model(base_url: str, cache: dict[str, list[str]], timeout_sec: int) -> str:
    models = cache.get(base_url)
    if not models:
        models = _candidate_models(base_url, timeout_sec)
        cache[base_url] = models
    if not models:
        return "qwen2.5-coder-7b-instruct"
    return models[0]


def _send_probe(endpoint_id: str, base_url: str, model: str, seq: int, timeout_sec: int, max_tokens: int) -> dict[str, Any]:
    marker = f"REMOTE_HEARTBEAT_{endpoint_id}_{int(time.time())}_{seq}"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": f"{marker}: reply exactly ACK_{endpoint_id}_{seq}",
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    ok, response, error, latency_ms = _http_json("POST", f"{base_url.rstrip('/')}/chat/completions", payload, timeout_sec)
    content = ""
    if ok:
        choices = response.get("choices", []) if isinstance(response.get("choices", []), list) else []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message", {})
            if isinstance(msg, dict):
                content = str(msg.get("content", "")).strip()
    return {
        "timestamp": _now_iso(),
        "event": "remote_heartbeat_probe",
        "endpoint_id": endpoint_id,
        "base_url": base_url,
        "model": model,
        "marker": marker,
        "ok": ok,
        "error": error,
        "latency_ms": round(latency_ms, 3),
        "response_preview": content[:200],
    }


def run(config_path: Path, interval_sec: int, timeout_sec: int, max_tokens: int, endpoint_ids: set[str]) -> int:
    cfg = _read_json(config_path)
    endpoints = cfg.get("endpoints", []) if isinstance(cfg.get("endpoints", []), list) else []
    selected: list[tuple[str, str]] = []
    for item in endpoints:
        if not isinstance(item, dict):
            continue
        endpoint_id = str(item.get("id", "")).strip()
        base_url = str(item.get("base_url", "")).strip()
        enabled = bool(item.get("enabled", True))
        if not endpoint_id or not base_url or not enabled:
            continue
        if endpoint_ids and endpoint_id not in endpoint_ids:
            continue
        if _is_remote(base_url):
            selected.append((endpoint_id, base_url))

    print(json.dumps({"timestamp": _now_iso(), "event": "remote_heartbeat_start", "endpoints": selected}, sort_keys=True), flush=True)
    if not selected:
        print(json.dumps({"timestamp": _now_iso(), "event": "remote_heartbeat_no_endpoints"}, sort_keys=True), flush=True)
        return 1

    seq = 0
    model_cache: dict[str, list[str]] = {}
    while not STOP:
        for endpoint_id, base_url in selected:
            if STOP:
                break
            seq += 1
            model = _choose_model(base_url, model_cache, timeout_sec)
            event = _send_probe(endpoint_id, base_url, model, seq, timeout_sec, max_tokens)
            print(json.dumps(event, sort_keys=True), flush=True)
        for _ in range(max(1, interval_sec)):
            if STOP:
                break
            time.sleep(1)

    print(json.dumps({"timestamp": _now_iso(), "event": "remote_heartbeat_stop"}, sort_keys=True), flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remote LM Studio heartbeat")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--interval-sec", type=int, default=10)
    parser.add_argument("--timeout-sec", type=int, default=25)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--endpoint-id", action="append", default=[])
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--pid-file", default="")
    parser.add_argument("--log-file", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.daemon:
        pid_file = Path(str(args.pid_file).strip()).resolve() if str(args.pid_file).strip() else Path(
            "artifacts/autonomy/local_models/remote_heartbeat.pid"
        ).resolve()
        log_file = Path(str(args.log_file).strip()).resolve() if str(args.log_file).strip() else Path(
            "artifacts/autonomy/local_models/remote_heartbeat.log"
        ).resolve()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        child_args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--config",
            str(Path(args.config).resolve()),
            "--interval-sec",
            str(args.interval_sec),
            "--timeout-sec",
            str(args.timeout_sec),
            "--max-tokens",
            str(args.max_tokens),
        ]
        for endpoint_id in args.endpoint_id:
            child_args.extend(["--endpoint-id", str(endpoint_id)])
        with log_file.open("a", encoding="utf-8") as log_handle:
            proc = subprocess.Popen(
                child_args,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                close_fds=True,
                env=os.environ.copy(),
            )
        pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
        print(proc.pid)
        return 0

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    endpoint_ids = {str(item).strip() for item in args.endpoint_id if str(item).strip()}
    return run(
        Path(args.config).resolve(),
        max(2, int(args.interval_sec)),
        max(3, int(args.timeout_sec)),
        max(8, int(args.max_tokens)),
        endpoint_ids,
    )


if __name__ == "__main__":
    raise SystemExit(main())
