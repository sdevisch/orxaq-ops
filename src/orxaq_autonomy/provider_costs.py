"""Provider-authoritative cost record normalization and aggregation."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

WINDOW_KEYS = ("last_hour", "today", "last_7d", "last_30d")
CANONICAL_SOURCE_AUTHORITATIVE = "authoritative_provider_api"
CANONICAL_SOURCE_ESTIMATED = "estimated_local_telemetry"


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_iso_timestamp(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _int_value(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_value(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _path_value(payload: Any, dotted_path: str) -> Any:
    node: Any = payload
    for part in dotted_path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _first_nonempty_str(payload: dict[str, Any], candidates: list[str]) -> str:
    for candidate in candidates:
        value = _path_value(payload, candidate)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_number(payload: dict[str, Any], candidates: list[str]) -> float | None:
    for candidate in candidates:
        value = _path_value(payload, candidate)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_int(payload: dict[str, Any], candidates: list[str]) -> int | None:
    for candidate in candidates:
        value = _path_value(payload, candidate)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _record_id(record: dict[str, Any]) -> str:
    joined = "|".join(
        [
            str(record.get("provider", "")),
            str(record.get("model", "")),
            str(record.get("window_start", "")),
            str(record.get("window_end", "")),
            str(record.get("total_tokens", 0)),
            str(record.get("total_cost_usd", 0.0)),
            str(record.get("source_of_truth", "")),
        ]
    )
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return digest[:24]


def normalize_canonical_record(
    payload: dict[str, Any],
    *,
    default_provider: str = "unknown",
    default_source_of_truth: str = CANONICAL_SOURCE_AUTHORITATIVE,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    provider = _first_nonempty_str(
        payload,
        ["provider", "provider_name", "source_provider", "vendor"],
    ) or str(default_provider or "unknown").strip()
    model = _first_nonempty_str(
        payload,
        ["model", "model_name", "sku", "usage.model", "metadata.model"],
    ) or "unknown"

    start_ts = _parse_iso_timestamp(
        _first_nonempty_str(
            payload,
            ["window_start", "start_time", "period_start", "start", "timestamp"],
        )
    )
    end_ts = _parse_iso_timestamp(
        _first_nonempty_str(
            payload,
            ["window_end", "end_time", "period_end", "end", "timestamp"],
        )
    )
    if start_ts is None and end_ts is None:
        return None
    if start_ts is None:
        start_ts = end_ts
    if end_ts is None:
        end_ts = start_ts
    if start_ts is None or end_ts is None:
        return None
    if end_ts < start_ts:
        start_ts, end_ts = end_ts, start_ts

    input_tokens = _first_int(
        payload,
        [
            "input_tokens",
            "prompt_tokens",
            "tokens_input",
            "usage.input_tokens",
            "usage.prompt_tokens",
        ],
    )
    output_tokens = _first_int(
        payload,
        [
            "output_tokens",
            "completion_tokens",
            "tokens_output",
            "usage.output_tokens",
            "usage.completion_tokens",
        ],
    )
    cache_tokens = _first_int(
        payload,
        [
            "cache_tokens",
            "cached_tokens",
            "usage.cache_tokens",
            "usage.cached_tokens",
            "usage.cache_read_input_tokens",
        ],
    )
    total_tokens = _first_int(
        payload,
        [
            "total_tokens",
            "tokens_total",
            "usage.total_tokens",
        ],
    )
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    cache_tokens = max(0, int(cache_tokens or 0))
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens + cache_tokens
    total_tokens = max(0, int(total_tokens))

    total_cost = _first_number(
        payload,
        [
            "total_cost_usd",
            "cost_usd",
            "amount_usd",
            "total_cost",
            "billing.total_cost_usd",
            "billing.cost_usd",
            "usage.cost_usd",
        ],
    )
    if total_cost is None:
        return None
    total_cost = max(0.0, float(total_cost))

    currency = (_first_nonempty_str(payload, ["currency", "billing.currency"]) or "USD").upper()
    source_of_truth = (
        _first_nonempty_str(payload, ["source_of_truth"])
        or str(default_source_of_truth or CANONICAL_SOURCE_AUTHORITATIVE).strip()
    )
    if source_of_truth not in {CANONICAL_SOURCE_AUTHORITATIVE, CANONICAL_SOURCE_ESTIMATED}:
        source_of_truth = str(source_of_truth or CANONICAL_SOURCE_AUTHORITATIVE)

    unit_price_input = _first_number(
        payload,
        [
            "unit_price_input_per_million",
            "unit_prices.input_per_million",
            "price.input_per_million",
        ],
    )
    unit_price_output = _first_number(
        payload,
        [
            "unit_price_output_per_million",
            "unit_prices.output_per_million",
            "price.output_per_million",
        ],
    )

    record = {
        "provider": str(provider).strip() or "unknown",
        "model": str(model).strip() or "unknown",
        "window_start": start_ts.isoformat(),
        "window_end": end_ts.isoformat(),
        "timestamp": end_ts.isoformat(),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 8),
        "currency": currency,
        "source_of_truth": source_of_truth,
    }
    if unit_price_input is not None:
        record["unit_price_input_per_million"] = round(max(0.0, float(unit_price_input)), 8)
    if unit_price_output is not None:
        record["unit_price_output_per_million"] = round(max(0.0, float(unit_price_output)), 8)

    provided_id = _first_nonempty_str(payload, ["record_id", "id"])
    record["record_id"] = provided_id or _record_id(record)
    return record


def _iter_record_payloads(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    payloads: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists() or not path.is_file():
        return payloads, errors

    try:
        if path.suffix.lower() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                records = raw.get("records", [])
                if isinstance(records, list):
                    payloads.extend(item for item in records if isinstance(item, dict))
                else:
                    errors.append(f"{path}: records field must be a list")
            elif isinstance(raw, list):
                payloads.extend(item for item in raw if isinstance(item, dict))
            else:
                errors.append(f"{path}: unsupported JSON payload type")
            return payloads, errors
    except Exception as err:
        errors.append(f"{path}: {err}")
        return payloads, errors

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    errors.append(f"{path}:{line_number}: invalid JSON")
                    continue
                if isinstance(item, dict):
                    payloads.append(item)
    except Exception as err:
        errors.append(f"{path}: {err}")
    return payloads, errors


def load_canonical_records(paths: list[Path]) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    files_scanned = 0
    raw_items = 0

    for path in paths:
        resolved = path.resolve()
        payloads, parse_errors = _iter_record_payloads(resolved)
        if resolved.exists() and resolved.is_file():
            files_scanned += 1
        errors.extend(parse_errors)
        for payload in payloads:
            raw_items += 1
            normalized = normalize_canonical_record(payload)
            if normalized is None:
                continue
            record_id = str(normalized.get("record_id", "")).strip() or _record_id(normalized)
            if record_id in seen:
                continue
            seen.add(record_id)
            normalized["record_id"] = record_id
            records.append(normalized)

    records.sort(
        key=lambda item: (
            str(item.get("window_end", "")),
            str(item.get("provider", "")),
            str(item.get("model", "")),
            str(item.get("record_id", "")),
        )
    )
    meta = {
        "files_scanned": files_scanned,
        "raw_items": raw_items,
        "records_total": len(records),
    }
    return records, errors, meta


def aggregate_canonical_records(
    records: list[dict[str, Any]],
    *,
    now_utc: dt.datetime | None = None,
    stale_threshold_sec: int = 900,
) -> dict[str, Any]:
    now = now_utc or _now_utc()
    stale_threshold = max(1, int(stale_threshold_sec))
    window_starts = {
        "last_hour": now - dt.timedelta(hours=1),
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "last_7d": now - dt.timedelta(days=7),
        "last_30d": now - dt.timedelta(days=30),
    }
    window_costs = {key: 0.0 for key in WINDOW_KEYS}
    window_tokens = {key: 0 for key in WINDOW_KEYS}
    window_responses = {key: 0 for key in WINDOW_KEYS}

    bucket_end = now.replace(minute=0, second=0, microsecond=0)
    bucket_start = bucket_end - dt.timedelta(hours=23)
    hourly_rows: dict[str, dict[str, Any]] = {}
    hourly_series: list[dict[str, Any]] = []
    for index in range(24):
        bucket_time = bucket_start + dt.timedelta(hours=index)
        row = {
            "bucket_start": bucket_time.isoformat(),
            "cost_usd_total": 0.0,
            "tokens_total": 0,
            "responses": 0,
        }
        hourly_rows[row["bucket_start"]] = row
        hourly_series.append(row)

    provider_map: dict[str, dict[str, Any]] = {}
    model_map: dict[str, dict[str, Any]] = {}
    latest_ts: dt.datetime | None = None
    source_counts: dict[str, int] = {}
    currencies: set[str] = set()

    for record in records:
        event_ts = _parse_iso_timestamp(record.get("window_end") or record.get("timestamp"))
        if event_ts is None or event_ts > now:
            continue
        cost = max(0.0, _float_value(record.get("total_cost_usd", 0.0), 0.0))
        tokens = max(0, _int_value(record.get("total_tokens", 0), 0))
        provider = str(record.get("provider", "unknown")).strip() or "unknown"
        model = str(record.get("model", "unknown")).strip() or "unknown"
        source = str(record.get("source_of_truth", "")).strip() or CANONICAL_SOURCE_AUTHORITATIVE
        currency = str(record.get("currency", "USD")).strip().upper() or "USD"
        currencies.add(currency)
        source_counts[source] = source_counts.get(source, 0) + 1

        if latest_ts is None or event_ts >= latest_ts:
            latest_ts = event_ts

        for key in WINDOW_KEYS:
            if event_ts >= window_starts[key]:
                window_costs[key] += cost
                window_tokens[key] += tokens
                window_responses[key] += 1

        if event_ts >= window_starts["last_30d"]:
            provider_row = provider_map.setdefault(provider, {"responses": 0, "cost_usd_total": 0.0, "tokens_total": 0})
            provider_row["responses"] = _int_value(provider_row.get("responses", 0), 0) + 1
            provider_row["cost_usd_total"] = _float_value(provider_row.get("cost_usd_total", 0.0), 0.0) + cost
            provider_row["tokens_total"] = _int_value(provider_row.get("tokens_total", 0), 0) + tokens

            model_row = model_map.setdefault(model, {"responses": 0, "cost_usd_total": 0.0, "tokens_total": 0})
            model_row["responses"] = _int_value(model_row.get("responses", 0), 0) + 1
            model_row["cost_usd_total"] = _float_value(model_row.get("cost_usd_total", 0.0), 0.0) + cost
            model_row["tokens_total"] = _int_value(model_row.get("tokens_total", 0), 0) + tokens

        bucket_ts = event_ts.replace(minute=0, second=0, microsecond=0)
        if bucket_start <= bucket_ts <= bucket_end:
            row = hourly_rows.get(bucket_ts.isoformat())
            if row is not None:
                row["cost_usd_total"] = _float_value(row.get("cost_usd_total", 0.0), 0.0) + cost
                row["tokens_total"] = _int_value(row.get("tokens_total", 0), 0) + tokens
                row["responses"] = _int_value(row.get("responses", 0), 0) + 1

    for row in hourly_series:
        row["cost_usd_total"] = round(_float_value(row.get("cost_usd_total", 0.0), 0.0), 8)
        row["tokens_total"] = _int_value(row.get("tokens_total", 0), 0)
        row["responses"] = _int_value(row.get("responses", 0), 0)

    def _finalize_split(source_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        ordered = sorted(
            source_map.items(),
            key=lambda item: (-_float_value(item[1].get("cost_usd_total", 0.0), 0.0), str(item[0])),
        )
        out: dict[str, dict[str, Any]] = {}
        for name, payload in ordered:
            responses = _int_value(payload.get("responses", 0), 0)
            tokens = _int_value(payload.get("tokens_total", 0), 0)
            cost = _float_value(payload.get("cost_usd_total", 0.0), 0.0)
            out[str(name)] = {
                "responses": responses,
                "tokens_total": tokens,
                "cost_usd_total": round(cost, 8),
                "cost_per_million_tokens": round(((cost * 1_000_000.0) / tokens) if tokens > 0 else 0.0, 6),
            }
        return out

    freshness_age_sec = -1
    latest_timestamp = ""
    if latest_ts is not None:
        latest_timestamp = latest_ts.isoformat()
        freshness_age_sec = max(0, int((now - latest_ts).total_seconds()))
    stale = freshness_age_sec < 0 or freshness_age_sec > stale_threshold

    source_of_truth = CANONICAL_SOURCE_ESTIMATED
    if source_counts.get(CANONICAL_SOURCE_AUTHORITATIVE, 0) > 0:
        source_of_truth = CANONICAL_SOURCE_AUTHORITATIVE
    elif source_counts:
        source_of_truth = sorted(source_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

    currency = "USD"
    if len(currencies) == 1:
        currency = next(iter(currencies))

    return {
        "records_total": len(records),
        "currency": currency,
        "source_of_truth": source_of_truth,
        "cost_windows_usd": {key: round(_float_value(window_costs[key], 0.0), 8) for key in WINDOW_KEYS},
        "cost_windows_tokens": {key: _int_value(window_tokens[key], 0) for key in WINDOW_KEYS},
        "cost_windows_responses": {key: _int_value(window_responses[key], 0) for key in WINDOW_KEYS},
        "provider_cost_30d": _finalize_split(provider_map),
        "model_cost_30d": _finalize_split(model_map),
        "cost_series_hourly_24h": hourly_series,
        "data_freshness": {
            "latest_event_timestamp": latest_timestamp,
            "age_sec": freshness_age_sec,
            "stale": stale,
            "stale_threshold_sec": stale_threshold,
            "files_scanned": 0,
            "events_scanned": len(records),
        },
    }
