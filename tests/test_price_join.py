"""Offline tests for the price-history capture loader and the price/period
join (``fintr.features.price_join``).

All fixture prices/volumes/dates are synthetic; no captured market data is
used or reproduced.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from fintr.data.period_capture import (
    BrokerageDirectoryInfo,
    CodeValue,
    DailyMarketProxyRow,
    DatedCodeValues,
    DatedMarketProxyValues,
    PeriodCapture,
)
from fintr.data.price_history import (
    DailyBar,
    PriceHistoryCapture,
    PriceHistoryCaptureError,
    PriceHistoryCaptureMeta,
    canonical_json,
    load_price_history_capture,
)
from fintr.features.price_join import (
    DEFAULT_ADJUSTMENT_RATIO_EPSILON,
    DEFAULT_VOLUME_RATIO_TOLERANCE,
    FORMULA_VERSION,
    join_price_history,
)


D1, D2, D3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)
PRICE_ONLY_PLACEHOLDER = date(2026, 3, 1)
PRICE_ONLY_UNEXPECTED = date(2026, 3, 5)
PRICE_ONLY_NO_DATA = date(2026, 3, 6)


def _period_capture() -> PeriodCapture:
    """D1/D3 carry AKD net+market rows (so daily_market_proxy computes a
    value); D2 is a listed server date with no rows that day (has_rows=False,
    proxy values None) -- exercises the "missing bar" path independently of
    the proxy path."""
    daily_akd_net = (
        DatedCodeValues(D1, (CodeValue("AAA", Decimal(10)), CodeValue("BBB", Decimal(-10)))),
        DatedCodeValues(D3, (CodeValue("AAA", Decimal(10)), CodeValue("BBB", Decimal(-10)))),
    )
    daily_akd_market = (
        DatedMarketProxyValues(D1, (
            DailyMarketProxyRow("AAA", Decimal(20), Decimal(5)),
            DailyMarketProxyRow("BBB", Decimal(20), Decimal(7)),
        )),
        DatedMarketProxyValues(D3, (
            DailyMarketProxyRow("AAA", Decimal(20), Decimal(9)),
            DailyMarketProxyRow("BBB", Decimal(20), Decimal(11)),
        )),
    )
    return PeriodCapture(
        symbol="TEST", start=D1, end=D3, captured_at_utc="2026-03-04T18:00:00Z",
        packet_sha256="deadbeef", server_dates=(D1, D2, D3), period_akd_net=(),
        daily_akd_net=daily_akd_net, daily_akd_market=daily_akd_market,
        daily_custody_lots=(), custodian_diff_start=D1, custodian_diff_end=D3,
        custodian_diff=(), metadata=(),
        brokerage_directory=BrokerageDirectoryInfo(present=False, row_count=0, index=None),
    )


def _bar(
    d: date, *, timestamp_utc: int = 0, o=None, h=None, l=None, c=None, adj=None, v=None,
) -> DailyBar:
    def dec(x):
        return None if x is None else Decimal(x)
    return DailyBar(
        trade_date=d, timestamp_utc=timestamp_utc, open=dec(o), high=dec(h), low=dec(l),
        close=dec(c), adjusted_close=dec(adj), volume=v,
    )


def _price_capture() -> PriceHistoryCapture:
    bars = (
        # Holiday placeholder: zero volume, O=H=L=C=previous close.
        _bar(PRICE_ONLY_PLACEHOLDER, o="5", h="5", l="5", c="5", adj="5", v=0),
        # D1: volume_ratio = implied_traded_lots(20)/20 = 1 -> within default
        # tolerance; vwap_proxy(6) within [low=5, high=7]; adjustment ratio 1.
        _bar(D1, o="5.5", h="7", l="5", c="6", adj="6", v=20),
        # D2 has no bar at all (server date missing a price bar).
        # D3: volume_ratio = 20/200 = 0.1 -> outside default tolerance;
        # vwap_proxy(10) within [low=9, high=11]; adjustment ratio drops to
        # 0.9 here (a boundary vs. the previous priced bar's ratio of 1).
        _bar(D3, o="9.5", h="11", l="9", c="10", adj="9", v=200),
        # Price-only, real trading bar not in the period capture; ratio
        # stays 0.9 (same as D3), so it is not itself a boundary.
        _bar(PRICE_ONLY_UNEXPECTED, o="9", h="11", l="8", c="10", adj="9", v=500),
        # Price-only, fully null bar (no data at all that date).
        _bar(PRICE_ONLY_NO_DATA),
    )
    meta = PriceHistoryCaptureMeta(
        schema="fintr_price_history_capture_v1", source_id="yahoo_finance_chart_v8",
        request_url="https://example.invalid/TEST", accessed_at_utc="2026-03-06T00:00:00Z",
        permission_status="user_asserted_2026-09-26_not_independently_verified",
        unofficial_source=True, raw_response_sha256="0" * 64, normalized_sha256="1" * 64,
    )
    return PriceHistoryCapture(
        meta=meta, symbol="TEST", requested_start=PRICE_ONLY_PLACEHOLDER,
        requested_end=PRICE_ONLY_NO_DATA, date_source="gmtoffset_fallback",
        dropped_before_start_count=0, dropped_after_end_count=0, bars=bars,
    )


class PriceJoinTests(unittest.TestCase):
    def setUp(self):
        self.result = join_price_history(_period_capture(), _price_capture())

    def test_formula_version_and_default_parameters_recorded(self):
        self.assertEqual(self.result.formula_version, FORMULA_VERSION)
        self.assertEqual(self.result.volume_ratio_tolerance, DEFAULT_VOLUME_RATIO_TOLERANCE)
        self.assertEqual(self.result.adjustment_ratio_epsilon, DEFAULT_ADJUSTMENT_RATIO_EPSILON)

    def test_joined_rows_carry_separate_raw_and_adjusted_fields(self):
        by_date = {day.date: day for day in self.result.joined}
        d1 = by_date[D1]
        self.assertTrue(d1.bar_present)
        self.assertEqual(d1.close, Decimal("6"))
        self.assertEqual(d1.volume, 20)
        self.assertEqual(d1.adjusted_close, Decimal("6"))
        self.assertNotEqual(d1.close, None)
        self.assertEqual(d1.vwap_proxy, Decimal("6"))
        self.assertTrue(d1.vwap_within_bar_range)
        self.assertEqual(d1.volume_ratio, Decimal("1"))
        self.assertTrue(d1.volume_ratio_within_tolerance)

    def test_missing_server_date_has_no_bar_fields_but_keeps_proxy(self):
        by_date = {day.date: day for day in self.result.joined}
        d2 = by_date[D2]
        self.assertFalse(d2.bar_present)
        self.assertIsNone(d2.close)
        self.assertIsNone(d2.volume)
        self.assertIsNone(d2.adjusted_close)
        self.assertIsNone(d2.vwap_proxy)  # D2 has no daily AKD rows either
        self.assertIsNone(d2.vwap_within_bar_range)
        self.assertIsNone(d2.volume_ratio)
        self.assertEqual(self.result.missing_dates, (D2,))

    def test_volume_ratio_outside_default_tolerance_is_flagged(self):
        by_date = {day.date: day for day in self.result.joined}
        d3 = by_date[D3]
        self.assertEqual(d3.volume_ratio, Decimal("0.1"))
        self.assertFalse(d3.volume_ratio_within_tolerance)
        self.assertTrue(d3.vwap_within_bar_range)  # 10 within [9, 11]

    def test_price_only_days_classified_placeholder_unexpected_no_data(self):
        by_date = {day.date: day for day in self.result.price_only_days}
        self.assertEqual(by_date[PRICE_ONLY_PLACEHOLDER].classification, "placeholder")
        self.assertEqual(by_date[PRICE_ONLY_UNEXPECTED].classification, "unexpected_trading_bar")
        self.assertEqual(by_date[PRICE_ONLY_NO_DATA].classification, "no_data_bar")
        self.assertEqual(self.result.placeholder_dates, (PRICE_ONLY_PLACEHOLDER,))
        self.assertEqual(self.result.unexpected_trading_dates, (PRICE_ONLY_UNEXPECTED,))
        self.assertEqual(self.result.no_data_dates, (PRICE_ONLY_NO_DATA,))

    def test_adjustment_boundary_detected_once_at_the_ratio_change(self):
        # Ratio sequence over ascending priced bars: 1 (placeholder), 1 (D1),
        # 0.9 (D3, boundary), 0.9 (unexpected, no change) -> exactly one date.
        self.assertEqual(self.result.adjustment_boundary_dates, (D3,))

    def test_wider_tolerance_and_epsilon_suppress_flags(self):
        lenient = join_price_history(
            _period_capture(), _price_capture(),
            volume_ratio_tolerance=Decimal("0.95"), adjustment_ratio_epsilon=Decimal("0.2"),
        )
        by_date = {day.date: day for day in lenient.joined}
        self.assertTrue(by_date[D3].volume_ratio_within_tolerance)
        self.assertEqual(lenient.adjustment_boundary_dates, ())

    def test_negative_parameters_rejected(self):
        with self.assertRaises(ValueError):
            join_price_history(_period_capture(), _price_capture(),
                                volume_ratio_tolerance=Decimal("-1"))
        with self.assertRaises(ValueError):
            join_price_history(_period_capture(), _price_capture(),
                                adjustment_ratio_epsilon=Decimal("-1"))


def _normalized_price_payload(symbol: str, start: str, end: str, bars: list[dict]) -> dict:
    return {
        "schema": "fintr_price_history_normalized_v1",
        "symbol": symbol,
        "currency": "TRY",
        "exchange_name": "IST",
        "exchange_timezone_name": "Europe/Istanbul",
        "gmtoffset": 10800,
        "timezone_abbreviation": "TRT",
        "date_source": "gmtoffset_fallback",
        "requested_start_date": start,
        "requested_end_date": end,
        "dropped_before_start_count": 0,
        "dropped_after_end_count": 0,
        "bar_count": len(bars),
        "bars": bars,
    }


def _normalized_bar(d: str, *, o="5", h="5", l="5", c="5", adj="5", v=0, ts=0) -> dict:
    return {
        "date": d, "timestamp_utc": ts, "open": o, "high": h, "low": l, "close": c,
        "adjusted_close": adj, "volume": v,
    }


def _price_capture_document(symbol: str, start: str, end: str, bars: list[dict]) -> dict:
    normalized = _normalized_price_payload(symbol, start, end, bars)
    normalized_hash = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return {
        "capture_metadata": {
            "schema": "fintr_price_history_capture_v1",
            "source_id": "yahoo_finance_chart_v8",
            "request_url": f"https://example.invalid/{symbol}",
            "accessed_at_utc": "2026-09-26T00:00:00Z",
            "permission_provenance": {
                "status": "user_asserted_2026-09-26_not_independently_verified",
                "source_note": "synthetic test fixture",
            },
            "raw_response_sha256": "0" * 64,
            "normalized_sha256": normalized_hash,
        },
        "raw_response": {"synthetic": True},
        "normalized": normalized,
    }


class PriceHistoryLoaderTests(unittest.TestCase):
    def test_loads_and_verifies_normalized_sha256(self):
        document = _price_capture_document(
            "TEST", "2026-03-01", "2026-03-02",
            [_normalized_bar("2026-03-01"), _normalized_bar("2026-03-02", c="6.5", v=10)],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            capture = load_price_history_capture(path)
        self.assertEqual(capture.symbol, "TEST")
        self.assertEqual(len(capture.bars), 2)
        self.assertEqual(capture.bars[1].close, Decimal("6.5"))
        self.assertTrue(capture.meta.unofficial_source)
        self.assertEqual(
            capture.meta.normalized_sha256, document["capture_metadata"]["normalized_sha256"],
        )

    def test_tampered_normalized_section_is_rejected(self):
        document = _price_capture_document(
            "TEST", "2026-03-01", "2026-03-01", [_normalized_bar("2026-03-01")],
        )
        document["normalized"]["bars"][0]["close"] = "999"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(PriceHistoryCaptureError, "normalized_hash_mismatch"):
                load_price_history_capture(path)

    def test_wrong_schema_is_rejected(self):
        document = _price_capture_document(
            "TEST", "2026-03-01", "2026-03-01", [_normalized_bar("2026-03-01")],
        )
        document["capture_metadata"]["schema"] = "other"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(PriceHistoryCaptureError, "unsupported_schema"):
                load_price_history_capture(path)

    def test_duplicate_bar_date_is_rejected(self):
        document = _price_capture_document(
            "TEST", "2026-03-01", "2026-03-01",
            [_normalized_bar("2026-03-01"), _normalized_bar("2026-03-01")],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(PriceHistoryCaptureError, "duplicate_date_in_bars"):
                load_price_history_capture(path)

    def test_unreadable_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(PriceHistoryCaptureError, "unreadable_capture"):
                load_price_history_capture(path)


if __name__ == "__main__":
    unittest.main()
