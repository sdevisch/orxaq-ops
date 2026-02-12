import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orxaq_autonomy import provider_costs


class ProviderCostTests(unittest.TestCase):
    def test_normalize_canonical_record(self):
        record = provider_costs.normalize_canonical_record(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T01:00:00+00:00",
                "prompt_tokens": 1200,
                "completion_tokens": 300,
                "cost_usd": 0.42,
            }
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["provider"], "openai")
        self.assertEqual(record["model"], "gpt-4.1-mini")
        self.assertEqual(record["input_tokens"], 1200)
        self.assertEqual(record["output_tokens"], 300)
        self.assertEqual(record["total_tokens"], 1500)
        self.assertEqual(record["source_of_truth"], provider_costs.CANONICAL_SOURCE_AUTHORITATIVE)
        self.assertAlmostEqual(record["total_cost_usd"], 0.42, places=8)

    def test_load_canonical_records_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            records_path = root / "records.ndjson"
            raw = [
                {
                    "record_id": "same",
                    "provider": "openai",
                    "model": "a",
                    "window_start": "2026-01-01T00:00:00+00:00",
                    "window_end": "2026-01-01T00:05:00+00:00",
                    "total_tokens": 100,
                    "total_cost_usd": 1.0,
                },
                {
                    "record_id": "same",
                    "provider": "openai",
                    "model": "a",
                    "window_start": "2026-01-01T00:00:00+00:00",
                    "window_end": "2026-01-01T00:05:00+00:00",
                    "total_tokens": 100,
                    "total_cost_usd": 1.0,
                },
                {
                    "provider": "gemini",
                    "model": "b",
                    "window_start": "2026-01-01T00:00:00+00:00",
                    "window_end": "2026-01-01T00:10:00+00:00",
                    "total_tokens": 200,
                    "total_cost_usd": 2.0,
                },
            ]
            records_path.write_text(
                "\n".join([json.dumps(item) for item in raw]) + "\n",
                encoding="utf-8",
            )
            records, errors, meta = provider_costs.load_canonical_records([records_path])
            self.assertEqual(errors, [])
            self.assertEqual(meta["records_total"], 2)
            self.assertEqual(len(records), 2)

    def test_aggregate_canonical_records_windows(self):
        now_utc = dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
        records = [
            {
                "record_id": "r1",
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "window_start": "2026-01-02T11:30:00+00:00",
                "window_end": "2026-01-02T11:30:00+00:00",
                "timestamp": "2026-01-02T11:30:00+00:00",
                "total_tokens": 100,
                "total_cost_usd": 1.0,
                "currency": "USD",
                "source_of_truth": provider_costs.CANONICAL_SOURCE_AUTHORITATIVE,
            },
            {
                "record_id": "r2",
                "provider": "anthropic",
                "model": "claude-3-7-sonnet-latest",
                "window_start": "2026-01-01T12:00:00+00:00",
                "window_end": "2026-01-01T12:00:00+00:00",
                "timestamp": "2026-01-01T12:00:00+00:00",
                "total_tokens": 300,
                "total_cost_usd": 3.0,
                "currency": "USD",
                "source_of_truth": provider_costs.CANONICAL_SOURCE_AUTHORITATIVE,
            },
        ]
        aggregate = provider_costs.aggregate_canonical_records(records, now_utc=now_utc, stale_threshold_sec=1800)
        self.assertEqual(aggregate["records_total"], 2)
        self.assertEqual(aggregate["source_of_truth"], provider_costs.CANONICAL_SOURCE_AUTHORITATIVE)
        self.assertAlmostEqual(aggregate["cost_windows_usd"]["last_hour"], 1.0, places=6)
        self.assertAlmostEqual(aggregate["cost_windows_usd"]["today"], 1.0, places=6)
        self.assertAlmostEqual(aggregate["cost_windows_usd"]["last_7d"], 4.0, places=6)
        self.assertIn("openai", aggregate["provider_cost_30d"])
        self.assertIn("gpt-4.1-mini", aggregate["model_cost_30d"])
        self.assertEqual(len(aggregate["cost_series_hourly_24h"]), 24)
        self.assertFalse(aggregate["data_freshness"]["stale"])


if __name__ == "__main__":
    unittest.main()
