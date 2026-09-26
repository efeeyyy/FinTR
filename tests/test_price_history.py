"""Offline tests for the Yahoo Finance chart-endpoint price history parser.

All fixture prices/volumes are synthetic; no captured market data is used.
"""

from __future__ import annotations

import copy
import json
import unittest
import unittest.mock
from datetime import date, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fintr.data.price_history import (
    DATE_SOURCE_GMTOFFSET_FALLBACK,
    DATE_SOURCE_TIMEZONE_NAME,
    PriceHistoryParseError,
    canonical_json,
    filter_to_range,
    normalized_payload,
    parse_chart_json,
)
from fintr.data import price_history as ph_module


FIXTURE = Path(__file__).parent / "fixtures" / "market" / "price_history_chart.json"
EXPECTED_DATES = [date(2026, 1, day) for day in (5, 6, 7, 8, 9)]


def _stub_istanbul_zoneinfo(key: str):
    """Stands in for ``zoneinfo.ZoneInfo`` so a test doesn't depend on the host
    having the IANA ``tzdata`` package installed (observed absent on this
    project's Windows dev environment). Returns a real ``tzinfo`` instance."""
    if key != "Europe/Istanbul":
        raise AssertionError(f"unexpected tz key: {key}")
    return timezone(timedelta(hours=3))


class PriceHistoryParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_parses_meta_and_bar_dates(self) -> None:
        history = parse_chart_json(self.document)
        self.assertEqual(history.meta.symbol, "TESTX.IS")
        self.assertEqual(history.meta.currency, "TRY")
        self.assertEqual(history.meta.exchange_name, "IST")
        self.assertEqual(history.meta.exchange_timezone_name, "Europe/Istanbul")
        self.assertEqual(history.meta.gmtoffset, 10800)
        self.assertEqual(len(history.bars), 5)
        self.assertEqual([bar.trade_date for bar in history.bars], EXPECTED_DATES)

    def test_timezone_date_conversion_uses_exchange_timezone_when_available(self) -> None:
        with unittest.mock.patch.object(ph_module, "ZoneInfo", _stub_istanbul_zoneinfo):
            history = parse_chart_json(self.document)
        self.assertEqual(history.meta.date_source, DATE_SOURCE_TIMEZONE_NAME)
        self.assertEqual([bar.trade_date for bar in history.bars], EXPECTED_DATES)

    def test_timezone_falls_back_to_gmtoffset_when_zoneinfo_unavailable(self) -> None:
        def _raise(_key: str):
            raise ph_module.ZoneInfoNotFoundError("no tzdata")

        with unittest.mock.patch.object(ph_module, "ZoneInfo", _raise):
            history = parse_chart_json(self.document)
        self.assertEqual(history.meta.date_source, DATE_SOURCE_GMTOFFSET_FALLBACK)
        self.assertEqual([bar.trade_date for bar in history.bars], EXPECTED_DATES)

    def test_raw_close_and_adjusted_close_kept_separate(self) -> None:
        history = parse_chart_json(self.document)
        first = history.bars[0]
        self.assertEqual(first.close, Decimal("10.2"))
        self.assertEqual(first.adjusted_close, Decimal("10.15"))
        self.assertNotEqual(first.close, first.adjusted_close)

    def test_nulls_stay_none_not_coerced_to_zero(self) -> None:
        history = parse_chart_json(self.document)
        halted = history.bars[2]
        self.assertEqual(halted.trade_date, date(2026, 1, 7))
        self.assertIsNone(halted.open)
        self.assertIsNone(halted.high)
        self.assertIsNone(halted.low)
        self.assertIsNone(halted.close)
        self.assertIsNone(halted.adjusted_close)
        self.assertIsNone(halted.volume)

    def test_length_mismatch_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        quote = document["chart"]["result"][0]["indicators"]["quote"][0]
        quote["high"] = quote["high"][:-1]
        with self.assertRaisesRegex(PriceHistoryParseError, "length"):
            parse_chart_json(document)

    def test_adjclose_length_mismatch_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"] = [1, 2]
        with self.assertRaisesRegex(PriceHistoryParseError, "length"):
            parse_chart_json(document)

    def test_duplicate_dates_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        result = document["chart"]["result"][0]
        result["timestamp"][1] = result["timestamp"][0]
        with self.assertRaisesRegex(PriceHistoryParseError, "duplicate trade date"):
            parse_chart_json(document)

    def test_non_finite_number_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        quote = document["chart"]["result"][0]["indicators"]["quote"][0]
        quote["close"][0] = float("nan")
        with self.assertRaisesRegex(PriceHistoryParseError, "finite"):
            parse_chart_json(document)

    def test_wrong_symbol_type_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["chart"]["result"][0]["meta"]["symbol"] = 12345
        with self.assertRaises(PriceHistoryParseError):
            parse_chart_json(document)

    def test_wrong_currency_type_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["chart"]["result"][0]["meta"]["currency"] = ["TRY"]
        with self.assertRaises(PriceHistoryParseError):
            parse_chart_json(document)

    def test_multiple_results_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["chart"]["result"].append(document["chart"]["result"][0])
        with self.assertRaisesRegex(PriceHistoryParseError, "chart.result"):
            parse_chart_json(document)

    def test_upstream_error_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["chart"]["error"] = {"code": "Not Found", "description": "No data found"}
        with self.assertRaisesRegex(PriceHistoryParseError, "chart.error"):
            parse_chart_json(document)

    def test_filter_to_range_drops_and_counts(self) -> None:
        history = parse_chart_json(self.document)
        filtered = filter_to_range(history, date(2026, 1, 6), date(2026, 1, 8))
        self.assertEqual([bar.trade_date for bar in filtered.bars],
                          [date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)])
        self.assertEqual(filtered.dropped_before_start, 1)
        self.assertEqual(filtered.dropped_after_end, 1)

    def test_filter_to_range_rejects_start_after_end(self) -> None:
        history = parse_chart_json(self.document)
        with self.assertRaises(ValueError):
            filter_to_range(history, date(2026, 1, 8), date(2026, 1, 6))

    def test_normalized_payload_decimal_as_string_and_nulls_stay_null(self) -> None:
        history = parse_chart_json(self.document)
        filtered = filter_to_range(history, date(2026, 1, 5), date(2026, 1, 9))
        payload = normalized_payload(filtered)
        self.assertEqual(payload["bar_count"], 5)
        self.assertEqual(payload["dropped_before_start_count"], 0)
        self.assertEqual(payload["dropped_after_end_count"], 0)
        self.assertEqual(payload["bars"][0]["close"], "10.2")
        self.assertIsInstance(payload["bars"][0]["close"], str)
        self.assertIsNone(payload["bars"][2]["close"])
        self.assertIsNone(payload["bars"][2]["volume"])

    def test_canonical_json_is_sorted_and_exact(self) -> None:
        text = canonical_json({"b": Decimal("1.50"), "a": 1})
        self.assertEqual(text, '{"a":1,"b":1.50}')

    def test_canonical_json_rejects_non_finite_decimal(self) -> None:
        with self.assertRaises(PriceHistoryParseError):
            canonical_json({"x": Decimal("Infinity")})


if __name__ == "__main__":
    unittest.main()
