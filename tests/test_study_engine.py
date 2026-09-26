"""Offline tests for the event-study engine (``fintr.research``).

All symbols, dates, codes and numbers here are synthetic; nothing is a real
capture or a real financial figure. Every capture/price object is built
directly as a dataclass (bypassing the JSON loaders), the same way
``tests/test_price_join.py`` and ``tests/test_period_analysis.py`` do.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from fintr.data.period_capture import (
    BrokerageDirectoryInfo,
    CodeValue,
    DailyMarketProxyRow,
    DatedCodeValues,
    DatedMarketProxyValues,
    PeriodCapture,
)
from fintr.data.price_history import DailyBar, PriceHistoryCapture, PriceHistoryCaptureMeta
from fintr.research.engine import (
    EngineError,
    EventOutcome,
    bootstrap_diff_ci,
    build_events,
    detect_custody_scale_jumps,
    load_symbol_data,
    summarize_horizon,
)
from fintr.research.features import FeatureError, evaluate_feature, truncate_period_capture, validate_feature_params
from fintr.research.report import RunParameters, run_study
from fintr.research.study_spec import parse_study_spec


D = [date(2027, 1, day) for day in range(1, 21)]  # D[0] = 2027-01-01 .. D[19] = 2027-01-20


def _capture(
    symbol: str = "TST", *, server_dates=(), daily_akd_net=(), daily_akd_market=(), daily_custody_lots=(),
) -> PeriodCapture:
    dates = tuple(sorted(server_dates))
    return PeriodCapture(
        symbol=symbol, start=dates[0], end=dates[-1], captured_at_utc="2027-01-01T00:00:00Z",
        packet_sha256="0" * 64, server_dates=dates, period_akd_net=(),
        daily_akd_net=tuple(sorted(daily_akd_net, key=lambda d: d.date)),
        daily_akd_market=tuple(sorted(daily_akd_market, key=lambda d: d.date)),
        daily_custody_lots=tuple(sorted(daily_custody_lots, key=lambda d: d.date)),
        custodian_diff_start=dates[0], custodian_diff_end=dates[-1], custodian_diff=(),
        metadata=(), brokerage_directory=BrokerageDirectoryInfo(present=False, row_count=0, index=None),
    )


def _bar(d: date, *, o=None, h=None, l=None, c=None, adj=None, v=None) -> DailyBar:
    def dec(x):
        return None if x is None else Decimal(str(x))
    return DailyBar(
        trade_date=d, timestamp_utc=0, open=dec(o), high=dec(h), low=dec(l), close=dec(c),
        adjusted_close=dec(adj), volume=v,
    )


def _price(symbol: str, bars: tuple[DailyBar, ...]) -> PriceHistoryCapture:
    return PriceHistoryCapture(
        meta=PriceHistoryCaptureMeta(
            schema="fintr_price_history_capture_v1", source_id="synthetic",
            request_url="synthetic://test", accessed_at_utc="2027-01-01T00:00:00Z",
            permission_status="user_asserted_not_independently_verified", unofficial_source=True,
            raw_response_sha256="0" * 64, normalized_sha256="0" * 64,
        ),
        symbol=symbol, requested_start=bars[0].trade_date, requested_end=bars[-1].trade_date,
        date_source="synthetic", dropped_before_start_count=0, dropped_after_end_count=0, bars=bars,
    )


def _spec_doc(**overrides) -> dict:
    doc = {
        "schema": "fintr_study_spec_v1",
        "spec_id": "unit_test_spec",
        "description": "synthetic unit-test spec",
        "frozen": False,
        "symbols": ["TST"],
        "symbol_holdout": [],
        "train_test_cutoff": "2027-06-01",
        "entry_timing": "next_open",
        "horizons_trading_days": [1],
        "min_event_warning_threshold": 30,
        "adjustment_boundary_policy": "use_adjusted",
        "bootstrap": {"seed": 42, "resamples": 200, "confidence_level": "0.90"},
        "condition": {"op": "ge", "left": {"const": "0"}, "right": {"const": "0"}},
    }
    doc.update(overrides)
    return doc


class LookaheadGuardTests(unittest.TestCase):
    def test_truncate_drops_every_future_dated_row(self):
        capture = _capture(
            server_dates=(D[0], D[1]),
            daily_akd_net=(
                DatedCodeValues(D[0], (CodeValue("AAA", Decimal(1)),)),
                DatedCodeValues(D[1], (CodeValue("AAA", Decimal(1000)),)),
            ),
            daily_akd_market=(
                DatedMarketProxyValues(D[0], (DailyMarketProxyRow("AAA", Decimal(10), Decimal(5)),)),
                DatedMarketProxyValues(D[1], (DailyMarketProxyRow("AAA", Decimal(10), Decimal(5)),)),
            ),
        )
        truncated = truncate_period_capture(capture, D[0])
        self.assertEqual(truncated.server_dates, (D[0],))
        self.assertTrue(all(day.date <= D[0] for day in truncated.daily_akd_net))
        self.assertTrue(all(day.date <= D[0] for day in truncated.daily_akd_market))
        self.assertNotIn(D[1], [day.date for day in truncated.daily_akd_net])

    def test_feature_value_on_truncated_capture_never_reflects_future_day(self):
        capture = _capture(
            server_dates=(D[0], D[1]),
            daily_akd_net=(
                DatedCodeValues(D[0], (CodeValue("AAA", Decimal(1)),)),
                DatedCodeValues(D[1], (CodeValue("AAA", Decimal(1000)),)),
            ),
            daily_akd_market=(
                DatedMarketProxyValues(D[0], (DailyMarketProxyRow("AAA", Decimal(10), Decimal(5)),)),
                DatedMarketProxyValues(D[1], (DailyMarketProxyRow("AAA", Decimal(10), Decimal(5)),)),
            ),
        )
        truncated = truncate_period_capture(capture, D[0])
        value_at_d0 = evaluate_feature(
            "top1_akd_net_share_of_volume", {}, truncated, D[0], close_by_date={},
        )
        # 1 / 10, never the huge D[1] value (1000 / 10), because D[1] is
        # structurally absent from `truncated`.
        self.assertEqual(value_at_d0.value, Decimal(1) / Decimal(10))

    def test_no_syntax_exists_for_a_forward_offset(self):
        with self.assertRaises(FeatureError):
            validate_feature_params("custody_change", {"codes": ["AAA"], "lag_days": -1}, "path")


class EntryTimingTests(unittest.TestCase):
    def _symbol_data(self):
        bars = tuple(_bar(D[i], o=100 + i, h=100 + i, l=100 + i, c=200 + i, adj=200 + i, v=1000) for i in range(10))
        capture = _capture(server_dates=(D[2], D[3]))
        return load_symbol_data(
            "TST", capture, _price("TST", bars),
            adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
        )

    def test_next_open_entry_is_the_first_tradeable_bar_strictly_after_signal(self):
        data = self._symbol_data()
        spec = parse_study_spec(_spec_doc(entry_timing="next_open"))
        conditioned, _ = build_events(data, spec, horizon=1, cost_bps=Decimal(0), signal_dates=(D[2],))
        event = conditioned[0]
        self.assertFalse(event.dropped)
        self.assertEqual(event.entry_date, D[3])
        self.assertEqual(event.exit_date, D[4])
        expected = Decimal(204) / Decimal(103) - 1  # exit close / entry open
        self.assertEqual(event.gross_return, expected)

    def test_next_close_entry_uses_the_entry_bars_close_not_open(self):
        data = self._symbol_data()
        spec = parse_study_spec(_spec_doc(entry_timing="next_close"))
        conditioned, _ = build_events(data, spec, horizon=1, cost_bps=Decimal(0), signal_dates=(D[2],))
        event = conditioned[0]
        expected = Decimal(204) / Decimal(203) - 1  # exit close / entry close
        self.assertEqual(event.gross_return, expected)


class HolidayExclusionTests(unittest.TestCase):
    def test_placeholder_bar_is_never_tradeable_and_is_skipped(self):
        bars = [_bar(D[i], o=100 + i, h=100 + i, l=100 + i, c=200 + i, adj=200 + i, v=1000) for i in range(6)]
        # D[3] is a holiday placeholder: zero volume, flat OHLC.
        bars[3] = _bar(D[3], o=150, h=150, l=150, c=150, adj=150, v=0)
        data = load_symbol_data(
            "TST", _capture(server_dates=(D[2],)), _price("TST", tuple(bars)),
            adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
        )
        self.assertNotIn(D[3], data.tradeable_dates)
        spec = parse_study_spec(_spec_doc(entry_timing="next_open"))
        conditioned, _ = build_events(data, spec, horizon=1, cost_bps=Decimal(0), signal_dates=(D[2],))
        event = conditioned[0]
        # Next tradeable bar after D[2] skips the D[3] placeholder -> D[4];
        # exit one tradeable day later is D[5], not D[4].
        self.assertEqual(event.entry_date, D[4])
        self.assertEqual(event.exit_date, D[5])


class AdjustmentBoundaryTests(unittest.TestCase):
    def _symbol_data(self):
        bars = []
        for i in range(8):
            close = Decimal(200 + i)
            adjusted = close if i < 5 else close * Decimal("0.5")  # boundary at D[5]
            bars.append(_bar(D[i], o=100 + i, h=100 + i, l=100 + i, c=close, adj=adjusted, v=1000))
        return _capture(server_dates=(D[3],)), tuple(bars)

    def test_boundary_in_window_uses_adjusted_close_consistently(self):
        capture, bars = self._symbol_data()
        data = load_symbol_data(
            "TST", capture, _price("TST", bars),
            adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
        )
        self.assertIn(D[5], data.adjustment_boundary_dates)
        spec = parse_study_spec(_spec_doc(entry_timing="next_open", adjustment_boundary_policy="use_adjusted"))
        conditioned, _ = build_events(data, spec, horizon=2, cost_bps=Decimal(0), signal_dates=(D[3],))
        event = conditioned[0]
        self.assertFalse(event.dropped)
        self.assertEqual(event.price_basis, "adjusted")
        self.assertEqual(event.entry_date, D[4])
        self.assertEqual(event.exit_date, D[6])
        expected = (Decimal(206) * Decimal("0.5")) / Decimal(204) - 1  # adj(D6) / adj(D4)
        self.assertEqual(event.gross_return, expected)

    def test_boundary_in_window_can_instead_drop_the_event(self):
        capture, bars = self._symbol_data()
        data = load_symbol_data(
            "TST", capture, _price("TST", bars),
            adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
        )
        spec = parse_study_spec(_spec_doc(entry_timing="next_open", adjustment_boundary_policy="drop"))
        conditioned, _ = build_events(data, spec, horizon=2, cost_bps=Decimal(0), signal_dates=(D[3],))
        event = conditioned[0]
        self.assertTrue(event.dropped)
        self.assertEqual(event.drop_reason, "adjustment_boundary_drop")
        stats = summarize_horizon(
            conditioned, [], horizon=2, min_event_warning_threshold=30, bootstrap_seed=1,
            bootstrap_resamples=100, bootstrap_confidence_level=Decimal("0.9"),
        )
        self.assertEqual(stats.dropped_adjustment_boundary, 1)
        self.assertEqual(stats.event_count, 0)


class CostApplicationTests(unittest.TestCase):
    def test_cost_bps_subtracts_round_trip_cost_from_gross_return(self):
        bars = (_bar(D[0], o=100, h=100, l=100, c=100, adj=100, v=1000),
                _bar(D[1], o=100, h=100, l=100, c=100, adj=100, v=1000),
                _bar(D[2], o=110, h=110, l=110, c=110, adj=110, v=1000))
        capture = _capture(server_dates=(D[0],))
        data = load_symbol_data(
            "TST", capture, _price("TST", bars),
            adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
        )
        spec = parse_study_spec(_spec_doc(entry_timing="next_open"))
        no_cost, _ = build_events(data, spec, horizon=1, cost_bps=Decimal(0), signal_dates=(D[0],))
        with_cost, _ = build_events(data, spec, horizon=1, cost_bps=Decimal(100), signal_dates=(D[0],))
        gross = no_cost[0].gross_return
        self.assertEqual(with_cost[0].gross_return, gross)
        self.assertEqual(with_cost[0].net_return, gross - Decimal("0.02"))  # 2 * 100bps


class MinEventWarningTests(unittest.TestCase):
    def _events(self, count: int) -> list[EventOutcome]:
        return [
            EventOutcome("TST", D[0], D[1], D[2], 1, "raw", Decimal("0.01"), Decimal("0.01"), False, None, True)
            for _ in range(count)
        ]

    def test_warns_below_threshold(self):
        stats = summarize_horizon(
            self._events(5), [], horizon=1, min_event_warning_threshold=30, bootstrap_seed=1,
            bootstrap_resamples=50, bootstrap_confidence_level=Decimal("0.9"),
        )
        self.assertEqual(stats.event_count, 5)
        self.assertTrue(stats.below_min_events_warning)

    def test_no_warning_at_or_above_threshold(self):
        stats = summarize_horizon(
            self._events(30), [], horizon=1, min_event_warning_threshold=30, bootstrap_seed=1,
            bootstrap_resamples=50, bootstrap_confidence_level=Decimal("0.9"),
        )
        self.assertEqual(stats.event_count, 30)
        self.assertFalse(stats.below_min_events_warning)


class BootstrapReproducibilityTests(unittest.TestCase):
    def test_same_seed_and_inputs_reproduce_identical_ci(self):
        conditioned = [Decimal("0.02"), Decimal("-0.01"), Decimal("0.03"), Decimal("0.00")]
        baseline = [Decimal("0.00"), Decimal("0.01"), Decimal("-0.02"), Decimal("0.015")]
        first = bootstrap_diff_ci(
            conditioned, baseline, seed=1234, resamples=500, confidence_level=Decimal("0.95"),
        )
        second = bootstrap_diff_ci(
            conditioned, baseline, seed=1234, resamples=500, confidence_level=Decimal("0.95"),
        )
        self.assertEqual(first.lower, second.lower)
        self.assertEqual(first.upper, second.upper)
        self.assertEqual(first.mean_difference, second.mean_difference)

    def test_no_events_yields_no_ci_not_a_fabricated_zero(self):
        result = bootstrap_diff_ci([], [Decimal("0.01")], seed=1, resamples=50, confidence_level=Decimal("0.9"))
        self.assertIsNone(result.lower)
        self.assertIsNone(result.upper)
        self.assertIsNone(result.mean_difference)


class CustodyScaleJumpTests(unittest.TestCase):
    def test_detects_whole_panel_lot_scale_change(self):
        capture = _capture(
            server_dates=(D[0], D[1]),
            daily_custody_lots=(
                DatedCodeValues(D[0], (CodeValue("CUS", Decimal(1000)),)),
                DatedCodeValues(D[1], (CodeValue("CUS", Decimal(1000000)),)),  # 1000x jump
            ),
        )
        jumps = detect_custody_scale_jumps(capture, Decimal("3"))
        self.assertEqual(jumps, (D[1],))

    def test_ordinary_flow_is_not_flagged(self):
        capture = _capture(
            server_dates=(D[0], D[1]),
            daily_custody_lots=(
                DatedCodeValues(D[0], (CodeValue("CUS", Decimal(1000)),)),
                DatedCodeValues(D[1], (CodeValue("CUS", Decimal(1050)),)),
            ),
        )
        self.assertEqual(detect_custody_scale_jumps(capture, Decimal("3")), ())


class FrozenSpecGateTests(unittest.TestCase):
    def _data(self):
        bars = tuple(_bar(D[i], o=100 + i, h=100 + i, l=100 + i, c=200 + i, adj=200 + i, v=1000) for i in range(10))
        capture = _capture(server_dates=(D[2], D[3]))
        return load_symbol_data(
            "TST", capture, _price("TST", bars),
            adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
        )

    def test_unfrozen_spec_refuses_test_split(self):
        spec = parse_study_spec(_spec_doc(frozen=False))
        params = RunParameters(Decimal(0), Decimal("0.5"), Decimal("0.0005"), Decimal("3"))
        with self.assertRaises(EngineError):
            run_study(spec, {"TST": self._data()}, split="test", params=params)

    def test_frozen_spec_allows_test_split(self):
        spec = parse_study_spec(_spec_doc(frozen=True))
        params = RunParameters(Decimal(0), Decimal("0.5"), Decimal("0.0005"), Decimal("3"))
        result = run_study(spec, {"TST": self._data()}, split="test", params=params)
        self.assertEqual(result["split"], "test")
        self.assertTrue(result["spec_frozen"])

    def test_train_split_always_allowed_even_unfrozen(self):
        spec = parse_study_spec(_spec_doc(frozen=False))
        params = RunParameters(Decimal(0), Decimal("0.5"), Decimal("0.0005"), Decimal("3"))
        result = run_study(spec, {"TST": self._data()}, split="train", params=params)
        self.assertEqual(result["split"], "train")


if __name__ == "__main__":
    unittest.main()
