"""Offline tests for ``fintr.features.period_adjusted``.

All symbols, codes, dates and lot/AKD values are synthetic fixtures; no
captured brokerage data and no real corporate-action fact is used.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from fintr.data.corporate_actions import parse_corporate_action_registry
from fintr.data.period_capture import (
    BrokerageDirectoryInfo,
    CodeValue,
    CustodianDiffRow,
    DailyMarketProxyRow,
    DatedCodeValues,
    DatedMarketProxyValues,
    PeriodCapture,
)
from fintr.features.period_adjusted import (
    FORMULA_VERSION,
    build_adjusted_capture,
    check_corporate_action_consistency,
    cumulative_akd_multiplier,
    cumulative_custody_multiplier,
)


D = [date(2026, 5, day) for day in (1, 2, 3, 4, 5, 6, 7, 8)]


def _action(action_id, ex_date, custody_effective_date, multiplier="6"):
    return {
        "action_id": action_id, "type": "bonus_issue", "lot_multiplier": multiplier,
        "ex_date": ex_date, "custody_effective_date": custody_effective_date,
        "source_url": "https://example.test/notice", "source_published_date": "2026-04-01",
        "source_file_sha256": None, "entered_by": "user",
        "entered_at_utc": "2026-04-01T00:00:00Z",
        "verification": "user_asserted_from_primary_source", "notes": "synthetic",
    }


def _registry(*actions):
    return parse_corporate_action_registry({
        "schema": "fintr_corporate_actions_v1", "symbol": "TEST", "actions": list(actions),
    })


def _capture(*, daily_akd_net, daily_custody_lots, daily_akd_market=(),
             period_akd_net=(), custodian_diff=(), custodian_diff_start=None,
             custodian_diff_end=None, server_dates=None):
    dates = server_dates or D
    return PeriodCapture(
        symbol="TEST", start=dates[0], end=dates[-1],
        captured_at_utc="2026-05-09T00:00:00Z", packet_sha256="deadbeef",
        server_dates=dates, period_akd_net=period_akd_net,
        daily_akd_net=daily_akd_net, daily_akd_market=daily_akd_market,
        daily_custody_lots=daily_custody_lots,
        custodian_diff_start=custodian_diff_start or dates[0],
        custodian_diff_end=custodian_diff_end or dates[-1],
        custodian_diff=custodian_diff, metadata=(),
        brokerage_directory=BrokerageDirectoryInfo(present=False, row_count=0, index=None),
    )


class MultiplierTests(unittest.TestCase):
    def test_single_action_multiplier_before_and_after_ex_date(self):
        registry = _registry(_action("A1", "2026-05-04", "2026-05-06", "6"))
        self.assertEqual(cumulative_akd_multiplier(registry.actions, date(2026, 5, 3)), Decimal(6))
        self.assertEqual(cumulative_akd_multiplier(registry.actions, date(2026, 5, 4)), Decimal(1))
        self.assertEqual(cumulative_custody_multiplier(registry.actions, date(2026, 5, 5)), Decimal(6))
        self.assertEqual(cumulative_custody_multiplier(registry.actions, date(2026, 5, 6)), Decimal(1))

    def test_two_actions_compose_multiplicatively(self):
        registry = _registry(
            _action("A1", "2026-05-04", "2026-05-06", "3"),
            _action("A2", "2026-08-04", "2026-08-06", "2"),
        )
        # Before both ex_dates: both multipliers apply.
        self.assertEqual(cumulative_akd_multiplier(registry.actions, date(2026, 5, 1)), Decimal(6))
        # Between the two ex_dates: only A2 still ahead.
        self.assertEqual(cumulative_akd_multiplier(registry.actions, date(2026, 6, 1)), Decimal(2))
        # After both: identity.
        self.assertEqual(cumulative_akd_multiplier(registry.actions, date(2026, 9, 1)), Decimal(1))


class BuildAdjustedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.registry = _registry(_action("A1", "2026-05-04", "2026-05-06", "6"))
        self.daily_akd_net = tuple(
            DatedCodeValues(d, (CodeValue("AAA", Decimal(10)),)) for d in D
        )
        self.daily_custody_lots = tuple(
            DatedCodeValues(d, (CodeValue("AAA", Decimal(100)),)) for d in D
        )
        self.daily_akd_market = tuple(
            DatedMarketProxyValues(d, (DailyMarketProxyRow("AAA", Decimal(20), Decimal(5)),))
            for d in D
        )

    def test_akd_net_scaled_only_before_ex_date(self):
        capture = _capture(
            daily_akd_net=self.daily_akd_net, daily_custody_lots=self.daily_custody_lots,
            daily_akd_market=self.daily_akd_market,
        )
        bundle = build_adjusted_capture(capture, self.registry.actions)
        by_date = {day.date: day.by_code()["AAA"] for day in bundle.capture.daily_akd_net}
        # ex_date = 2026-05-04: dates before it (05-01..05-03) get *6; on/after unchanged.
        self.assertEqual(by_date[date(2026, 5, 3)], Decimal(60))
        self.assertEqual(by_date[date(2026, 5, 4)], Decimal(10))
        self.assertEqual(by_date[date(2026, 5, 8)], Decimal(10))

    def test_total_size_scaled_but_total_cost_left_alone(self):
        capture = _capture(
            daily_akd_net=self.daily_akd_net, daily_custody_lots=self.daily_custody_lots,
            daily_akd_market=self.daily_akd_market,
        )
        bundle = build_adjusted_capture(capture, self.registry.actions)
        row = bundle.capture.daily_akd_market[0].by_code()["AAA"]  # 2026-05-01, before ex_date
        self.assertEqual(row.total_size, Decimal(120))  # 20 * 6
        self.assertEqual(row.total_cost, Decimal(5))  # price left unadjusted

    def test_custody_scaled_only_before_custody_effective_date(self):
        capture = _capture(
            daily_akd_net=self.daily_akd_net, daily_custody_lots=self.daily_custody_lots,
        )
        bundle = build_adjusted_capture(capture, self.registry.actions)
        by_date = {day.date: day.by_code()["AAA"] for day in bundle.capture.daily_custody_lots}
        # custody_effective_date = 2026-05-06: dates before it get *6.
        self.assertEqual(by_date[date(2026, 5, 5)], Decimal(600))
        self.assertEqual(by_date[date(2026, 5, 6)], Decimal(100))

    def test_period_akd_net_is_recomputed_from_adjusted_daily(self):
        capture = _capture(
            daily_akd_net=self.daily_akd_net, daily_custody_lots=self.daily_custody_lots,
            period_akd_net=(CodeValue("AAA", Decimal(999)),),  # raw, mixed-unit, ignored here
        )
        bundle = build_adjusted_capture(capture, self.registry.actions)
        net = {row.code: row.value for row in bundle.capture.period_akd_net}
        # 3 days * 60 (pre-ex_date, scaled) + 5 days * 10 (post-ex_date) = 180 + 50 = 230
        self.assertEqual(net["AAA"], Decimal(230))

    def test_mixed_unit_period_flag_true_when_akd_multiplier_varies(self):
        capture = _capture(
            daily_akd_net=self.daily_akd_net, daily_custody_lots=self.daily_custody_lots,
        )
        bundle = build_adjusted_capture(capture, self.registry.actions)
        self.assertTrue(bundle.mixed_unit_period)

    def test_mixed_unit_period_flag_false_when_period_entirely_after_ex_date(self):
        later_dates = tuple(date(2026, 9, day) for day in (1, 2, 3))
        daily_akd_net = tuple(DatedCodeValues(d, (CodeValue("AAA", Decimal(10)),)) for d in later_dates)
        daily_custody = tuple(DatedCodeValues(d, (CodeValue("AAA", Decimal(100)),)) for d in later_dates)
        capture = _capture(
            daily_akd_net=daily_akd_net, daily_custody_lots=daily_custody, server_dates=later_dates,
        )
        bundle = build_adjusted_capture(capture, self.registry.actions)
        self.assertFalse(bundle.mixed_unit_period)


class CustodianDiffRestatementTests(unittest.TestCase):
    def test_restated_when_period_start_before_custody_effective_date(self):
        registry = _registry(_action("A1", "2026-05-04", "2026-05-06", "6"))
        diff = (CustodianDiffRow(code="AAA", first_value=Decimal(100),
                                  last_value=Decimal(700), difference=Decimal(600)),)
        capture = _capture(
            daily_akd_net=(), daily_custody_lots=(), custodian_diff=diff,
            custodian_diff_start=date(2026, 5, 1), custodian_diff_end=date(2026, 5, 8),
        )
        bundle = build_adjusted_capture(capture, registry.actions)
        row = bundle.capture.custodian_diff[0]
        self.assertTrue(bundle.diff_restated)
        self.assertEqual(bundle.diff_start_multiplier, Decimal(6))
        self.assertEqual(row.first_value, Decimal(600))  # 100 * 6
        self.assertEqual(row.last_value, Decimal(700))  # unchanged
        self.assertEqual(row.difference, Decimal(100))  # 700 - 600

    def test_not_restated_when_period_start_after_custody_effective_date(self):
        registry = _registry(_action("A1", "2026-05-04", "2026-05-06", "6"))
        diff = (CustodianDiffRow(code="AAA", first_value=Decimal(700),
                                  last_value=Decimal(800), difference=Decimal(100)),)
        capture = _capture(
            daily_akd_net=(), daily_custody_lots=(), custodian_diff=diff,
            custodian_diff_start=date(2026, 5, 7), custodian_diff_end=date(2026, 5, 8),
        )
        bundle = build_adjusted_capture(capture, registry.actions)
        self.assertFalse(bundle.diff_restated)
        row = bundle.capture.custodian_diff[0]
        self.assertEqual(row.first_value, Decimal(700))
        self.assertEqual(row.difference, Decimal(100))


class ConsistencyCheckTests(unittest.TestCase):
    def test_pass_when_custody_matches_multiplier_times_previous_plus_akd_net(self):
        # lag=2 (default): custody_effective_date index i, trade_date = i-2.
        registry = _registry(_action("A1", "2026-05-06", "2026-05-06", "6"))
        dates = D  # 05-01 .. 05-08; 05-06 is index 5
        daily_akd_net = tuple(
            DatedCodeValues(d, (CodeValue("AAA", Decimal(5)),) if d == date(2026, 5, 4) else ())
            for d in dates
        )
        custody_values = {date(2026, 5, 5): Decimal(100), date(2026, 5, 6): Decimal(605)}
        daily_custody = tuple(
            DatedCodeValues(d, (CodeValue("AAA", custody_values[d]),))
            for d in custody_values
        )
        capture = _capture(
            daily_akd_net=daily_akd_net, daily_custody_lots=daily_custody, server_dates=dates,
        )
        results = check_corporate_action_consistency(
            capture, registry.actions, custody_float_tolerance=Decimal("0.000001"),
        )
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertTrue(row.evaluated)
        self.assertEqual(row.previous_custody_date, date(2026, 5, 5))
        self.assertEqual(row.trade_date, date(2026, 5, 4))
        self.assertEqual(row.pass_count, 1)
        self.assertEqual(row.fail_count, 0)

    def test_fail_when_actual_custody_disagrees_with_prediction(self):
        registry = _registry(_action("A1", "2026-05-06", "2026-05-06", "6"))
        dates = D
        daily_akd_net = tuple(
            DatedCodeValues(d, (CodeValue("AAA", Decimal(5)),) if d == date(2026, 5, 4) else ())
            for d in dates
        )
        custody_values = {date(2026, 5, 5): Decimal(100), date(2026, 5, 6): Decimal(999)}
        daily_custody = tuple(
            DatedCodeValues(d, (CodeValue("AAA", custody_values[d]),)) for d in custody_values
        )
        capture = _capture(
            daily_akd_net=daily_akd_net, daily_custody_lots=daily_custody, server_dates=dates,
        )
        results = check_corporate_action_consistency(
            capture, registry.actions, custody_float_tolerance=Decimal("0.000001"),
        )
        self.assertEqual(results[0].fail_count, 1)
        self.assertEqual(results[0].pass_count, 0)

    def test_not_evaluated_when_akd_missing_at_trade_date(self):
        registry = _registry(_action("A1", "2026-05-06", "2026-05-06", "6"))
        dates = D
        # No AKD row at all on 2026-05-04 (the trade_date for lag=2).
        custody_values = {date(2026, 5, 5): Decimal(100), date(2026, 5, 6): Decimal(605)}
        daily_custody = tuple(
            DatedCodeValues(d, (CodeValue("AAA", custody_values[d]),)) for d in custody_values
        )
        capture = _capture(daily_akd_net=(), daily_custody_lots=daily_custody, server_dates=dates)
        results = check_corporate_action_consistency(
            capture, registry.actions, custody_float_tolerance=Decimal("0.000001"),
        )
        self.assertEqual(results[0].not_evaluated_code_count, 1)
        self.assertEqual(results[0].pass_count, 0)
        self.assertEqual(results[0].fail_count, 0)

    def test_not_evaluated_flag_false_when_custody_effective_date_not_a_server_date(self):
        registry = _registry(_action("A1", "2026-05-20", "2026-05-20", "6"))
        capture = _capture(daily_akd_net=(), daily_custody_lots=(), server_dates=D)
        results = check_corporate_action_consistency(
            capture, registry.actions, custody_float_tolerance=Decimal("0.000001"),
        )
        self.assertFalse(results[0].evaluated)
        self.assertEqual(results[0].rows, ())

    def test_formula_version_constant(self):
        self.assertEqual(FORMULA_VERSION, "period_adjusted_v1")


if __name__ == "__main__":
    unittest.main()
