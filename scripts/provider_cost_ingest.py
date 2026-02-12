#!/usr/bin/env python3
"""Ingest provider-authoritative usage/cost records into canonical artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy.provider_costs import (
    CANONICAL_SOURCE_AUTHORITATIVE,
    aggregate_canonical_records,
    normalize_canonical_record,
)


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    url_env: str
    key_envs: tuple[str, ...]
    auth_mode: str
    extra_headers: tuple[tuple[str, str], ...] = ()


SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        provider="openai",
        url_env="ORXAQ_COST_OPENAI_USAGE_URL",
        key_envs=("OPENAI_API_KEY",),
        auth_mode="bearer",
    ),
    ProviderSpec(
        provider="anthropic",
        url_env="ORXAQ_COST_ANTHROPIC_USAGE_URL",
        key_envs=("ANTHROPIC_API_KEY",),
        auth_mode="x-api-key",
        extra_headers=(("anthropic-version", "2023-06-01"),),
    ),
    ProviderSpec(
        provider="gemini",
        url_env="ORXAQ_COST_GEMINI_USAGE_URL",
        key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        auth_mode="query-key",
    ),
)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_json_bytes(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    return json.loads(text)


def _path_value(payload: Any, dotted_path: str) -> Any:
    node: Any = payload
    for part in dotted_path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "data",
        "items",
        "results",
        "usage",
        "records",
        "billing.items",
        "billing.data",
    ):
        value = _path_value(payload, key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _extract_next_cursor(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in (
        "next_cursor",
        "next_page",
        "cursor",
        "pagination.next_cursor",
        "paging.next",
        "meta.next_cursor",
    ):
        value = _path_value(payload, key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _build_url(
    base_url: str,
    *,
    start_ts: int,
    end_ts: int,
    cursor: str,
    limit: int,
    api_key: str,
    auth_mode: str,
) -> str:
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("start_time", str(start_ts))
    query.setdefault("end_time", str(end_ts))
    query.setdefault("limit", str(limit))
    if cursor:
        query["cursor"] = cursor
    if auth_mode == "query-key" and api_key and "key" not in query:
        query["key"] = api_key
    return urlunparse(parsed._replace(query=urlencode(query)))


def _request_json(
    url: str,
    *,
    api_key: str,
    auth_mode: str,
    timeout_sec: int,
    retries: int,
    backoff_sec: int,
    extra_headers: tuple[tuple[str, str], ...],
) -> Any:
    headers = {"Accept": "application/json"}
    if auth_mode == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_mode == "x-api-key" and api_key:
        headers["x-api-key"] = api_key
    for key, value in extra_headers:
        headers[key] = value

    last_error = ""
    for attempt in range(max(1, retries + 1)):
        try:
            request = Request(url, headers=headers, method="GET")
            with urlopen(request, timeout=timeout_sec) as response:
                return _parse_json_bytes(response.read())
        except Exception as err:
            last_error = str(err)
            if attempt >= retries:
                break
            time.sleep(max(1, backoff_sec) * (2**attempt))
    raise RuntimeError(last_error or "request_failed")


def _resolve_api_key(spec: ProviderSpec) -> str:
    for key_name in spec.key_envs:
        value = str(os.environ.get(key_name, "")).strip()
        if value:
            return value
    return ""


def ingest_provider(
    spec: ProviderSpec,
    *,
    start_ts: int,
    end_ts: int,
    page_size: int,
    max_pages: int,
    timeout_sec: int,
    retries: int,
    backoff_sec: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    endpoint = str(os.environ.get(spec.url_env, "")).strip()
    if not endpoint:
        return [], {"provider": spec.provider, "ok": False, "status": "skipped", "reason": "endpoint_not_configured"}

    api_key = _resolve_api_key(spec)
    if not api_key:
        return [], {"provider": spec.provider, "ok": False, "status": "skipped", "reason": "api_key_missing"}

    records: list[dict[str, Any]] = []
    page_cursor = ""
    seen_cursors: set[str] = set()
    pages = 0
    errors: list[str] = []

    for _ in range(max(1, max_pages)):
        request_url = _build_url(
            endpoint,
            start_ts=start_ts,
            end_ts=end_ts,
            cursor=page_cursor,
            limit=max(1, page_size),
            api_key=api_key,
            auth_mode=spec.auth_mode,
        )
        try:
            payload = _request_json(
                request_url,
                api_key=api_key,
                auth_mode=spec.auth_mode,
                timeout_sec=timeout_sec,
                retries=retries,
                backoff_sec=backoff_sec,
                extra_headers=spec.extra_headers,
            )
        except Exception as err:
            errors.append(str(err))
            break

        pages += 1
        for item in _extract_items(payload):
            normalized = normalize_canonical_record(
                item,
                default_provider=spec.provider,
                default_source_of_truth=CANONICAL_SOURCE_AUTHORITATIVE,
            )
            if normalized is not None:
                records.append(normalized)

        next_cursor = _extract_next_cursor(payload)
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            errors.append("cursor_loop_detected")
            break
        seen_cursors.add(next_cursor)
        page_cursor = next_cursor

    if errors:
        ok = len(records) > 0
        status_name = "partial" if len(records) > 0 else "failed"
    else:
        # Empty windows are expected for low/no traffic periods and should not
        # fail ingestion health checks.
        ok = True
        status_name = "ok" if len(records) > 0 else "no_data"

    status = {
        "provider": spec.provider,
        "ok": ok,
        "status": status_name,
        "records": len(records),
        "pages": pages,
        "errors": errors,
        "endpoint": endpoint,
    }
    return records, status


def write_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, sort_keys=True) for record in records]
    content = "\n".join(lines) + ("\n" if lines else "")
    path.write_text(content, encoding="utf-8")


def write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest provider-authoritative cost records.")
    parser.add_argument(
        "--output-records",
        default=str(ROOT / "artifacts" / "autonomy" / "provider_costs" / "records.ndjson"),
    )
    parser.add_argument(
        "--output-summary",
        default=str(ROOT / "artifacts" / "autonomy" / "provider_costs" / "summary.json"),
    )
    parser.add_argument("--window-hours", type=int, default=24 * 30)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--timeout-sec", type=int, default=20)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff-sec", type=int, default=2)
    parser.add_argument("--stale-threshold-sec", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now_utc = _now_utc()
    window_hours = max(1, int(args.window_hours))
    start_ts = int((now_utc - dt.timedelta(hours=window_hours)).timestamp())
    end_ts = int(now_utc.timestamp())

    all_records: list[dict[str, Any]] = []
    provider_status: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for spec in SPECS:
        records, status = ingest_provider(
            spec,
            start_ts=start_ts,
            end_ts=end_ts,
            page_size=max(1, int(args.page_size)),
            max_pages=max(1, int(args.max_pages)),
            timeout_sec=max(1, int(args.timeout_sec)),
            retries=max(0, int(args.retries)),
            backoff_sec=max(1, int(args.retry_backoff_sec)),
        )
        provider_status.append(status)
        for record in records:
            record_id = str(record.get("record_id", "")).strip()
            if not record_id or record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            all_records.append(record)

    all_records.sort(
        key=lambda item: (
            str(item.get("window_end", "")),
            str(item.get("provider", "")),
            str(item.get("model", "")),
            str(item.get("record_id", "")),
        )
    )
    output_records = Path(args.output_records).expanduser().resolve()
    output_summary = Path(args.output_summary).expanduser().resolve()
    write_records(output_records, all_records)

    aggregate = aggregate_canonical_records(
        all_records,
        now_utc=now_utc,
        stale_threshold_sec=max(1, int(args.stale_threshold_sec)),
    )
    freshness = dict(aggregate.get("data_freshness", {}))
    freshness["files_scanned"] = 1 if output_records.exists() else 0
    freshness["events_scanned"] = len(all_records)
    aggregate["data_freshness"] = freshness
    summary = {
        "timestamp": now_utc.isoformat(),
        "ok": any(bool(item.get("ok", False)) for item in provider_status),
        "window_hours": window_hours,
        "providers": provider_status,
        "output_records_file": str(output_records),
        "output_summary_file": str(output_summary),
        **aggregate,
    }
    write_summary(output_summary, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
