"""Deterministic features over the Live* models: synthetic fixtures only."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
import unittest

from fintr.data.models import (
    LiveAKDNet,
    LiveAKDResponse,
    LiveAKDRow,
    LiveAKDTotal,
    LiveCustodianResponse,
    LiveCustodianRow,
    LiveCustodyDistributionResponse,
    LiveCustodyDistributionRow,
    LiveCustodyStatsFormat,
    LiveCustodyStatsPoint,
    LiveCustodyStatsResponse,
    LiveCustodyStatsSeries,
)
from fintr.features.live import (
    FORMULA_VERSION,
    FORMULAS,
    calculate_live_akd_features,
    calculate_live_custodian_features,
    calculate_live_distribution_features,
    calculate_live_stats_features,
)
from fintr.inference.live_context import LiveContextLimits, build_live_analysis_context
from test_live_analysis_context import LIMITS, _evidence


def _akd_row(brokerage: str, size: int) -> LiveAKDRow:
    return LiveAKDRow(
        brokerage=brokerage,
        net=LiveAKDNet(size=size, percentage=Decimal("0.1"), cost=Decimal("1")),
        total=LiveAKDTotal(
            size=Decimal(abs(size)), percentage=Decimal("0.2"),
            cost=Decimal("1"), volume=Decimal("10"),
        ),
    )


def _akd_response(sizes: list[int]) -> LiveAKDResponse:
    return LiveAKDResponse(
        request_id="r",
        timestamp=Decimal("1"),
        processing_time_ms=Decimal("1"),
        as_of="00:00:00",
        source="test",
        results=tuple(_akd_row(f"B{i}", size) for i, size in enumerate(sizes)),
    )


class LiveAKDFeaturesTests(unittest.TestCase):
    def test_zero_denominators_yield_none_shares(self) -> None:
        features = calculate_live_akd_features(_akd_response([0, 0]))
        self.assertEqual(features.row_count, 2)
        self.assertEqual(features.positive_net_count, 0)
        self.assertEqual(features.negative_net_count, 0)
        self.assertEqual(features.zero_net_count, 2)
        self.assertEqual(features.positive_net_sum, 0)
        self.assertEqual(features.negative_net_sum, 0)
        self.assertEqual(features.net_sum_as_received, 0)
        self.assertIsNone(features.top5_positive_net_share)
        self.assertIsNone(features.top5_negative_net_share)

    def test_top5_shares_and_integrity_sum_not_forced_to_zero(self) -> None:
        sizes = [100, 90, 80, 70, 60, 10, -50, -40, -30, -20, -10, -1]
        features = calculate_live_akd_features(_akd_response(sizes))
        positive_sum = 100 + 90 + 80 + 70 + 60 + 10
        negative_sum = -50 - 40 - 30 - 20 - 10 - 1
        self.assertEqual(features.positive_net_sum, positive_sum)
        self.assertEqual(features.negative_net_sum, negative_sum)
        # An integrity observation: reported as-is, not forced to zero.
        self.assertEqual(features.net_sum_as_received, positive_sum + negative_sum)
        self.assertNotEqual(features.net_sum_as_received, 0)
        top5_positive = 100 + 90 + 80 + 70 + 60
        self.assertEqual(
            features.top5_positive_net_share, Decimal(top5_positive) / Decimal(positive_sum),
        )
        top5_negative_abs = 50 + 40 + 30 + 20 + 10
        self.assertEqual(
            features.top5_negative_net_share,
            Decimal(top5_negative_abs) / Decimal(-negative_sum),
        )

    def test_rejects_wrong_type(self) -> None:
        with self.assertRaises(TypeError):
            calculate_live_akd_features(object())  # type: ignore[arg-type]


class LiveCustodianFeaturesTests(unittest.TestCase):
    def _response(self, values: list[Decimal], percentages: list[Decimal]) -> LiveCustodianResponse:
        return LiveCustodianResponse(
            request_id="r",
            timestamp=Decimal("1"),
            processing_time_ms=Decimal("1"),
            date=date(2026, 1, 1),
            source="test",
            results=tuple(
                LiveCustodianRow(custodian=f"C{i}", value=value, percentage=pct)
                for i, (value, pct) in enumerate(zip(values, percentages))
            ),
        )

    def test_zero_total_yields_none_share(self) -> None:
        features = calculate_live_custodian_features(
            self._response([Decimal(0), Decimal(0)], [Decimal("0.5"), Decimal("0.5")]),
        )
        self.assertEqual(features.total_value, Decimal(0))
        self.assertIsNone(features.top5_value_share)
        # Percentage sum is an integrity observation, reported as-is even if != 1.
        self.assertEqual(features.percentage_sum_as_received, Decimal("1.0"))

    def test_top5_value_share_over_six_rows(self) -> None:
        values = [Decimal(v) for v in (60, 50, 40, 30, 20, 10)]
        percentages = [Decimal("0.1")] * 6
        features = calculate_live_custodian_features(self._response(values, percentages))
        total = sum(values, Decimal(0))
        top5 = sum(values[:5], Decimal(0))
        self.assertEqual(features.total_value, total)
        self.assertEqual(features.top5_value_share, top5 / total)


class LiveDistributionFeaturesTests(unittest.TestCase):
    def _response(
        self, periods: tuple[date, ...], rows: tuple[LiveCustodyDistributionRow, ...],
    ) -> LiveCustodyDistributionResponse:
        return LiveCustodyDistributionResponse(
            request_id="r",
            timestamp=Decimal("1"),
            processing_time_ms=Decimal("1"),
            periods=periods,
            rows=rows,
            source="test",
        )

    def test_latest_period_found_by_date_not_tuple_position(self) -> None:
        # Periods intentionally NOT in descending order; latest is at index 1.
        periods = (date(2025, 1, 1), date(2025, 3, 1), date(2025, 2, 1))
        rows = (
            LiveCustodyDistributionRow(
                code="A", values=(Decimal(10), Decimal(30), Decimal(20)), percentage=Decimal("0.1"),
            ),
        )
        features = calculate_live_distribution_features(self._response(periods, rows))
        self.assertEqual(features.latest_period, date(2025, 3, 1))
        changes = {change.period: change.change for change in features.rows[0].changes}
        self.assertEqual(changes[date(2025, 1, 1)], Decimal(30) - Decimal(10))
        self.assertEqual(changes[date(2025, 2, 1)], Decimal(30) - Decimal(20))

    def test_empty_period_list_yields_no_changes_instead_of_error(self) -> None:
        rows = (LiveCustodyDistributionRow(code="A", values=(), percentage=None),)
        features = calculate_live_distribution_features(self._response((), rows))
        self.assertEqual((features.period_count, features.latest_period, features.rows),
                         (0, None, ()))
        self.assertEqual(features.row_count, 1)

    def test_none_values_propagate_to_none_change(self) -> None:
        periods = (date(2025, 1, 1), date(2025, 2, 1))
        rows = (
            LiveCustodyDistributionRow(code="A", values=(None, Decimal(5)), percentage=None),
            LiveCustodyDistributionRow(code="B", values=(Decimal(5), None), percentage=None),
        )
        features = calculate_live_distribution_features(self._response(periods, rows))
        self.assertIsNone(features.rows[0].changes[0].change)
        self.assertIsNone(features.rows[1].changes[0].change)

    def test_length_mismatch_raises(self) -> None:
        periods = (date(2025, 1, 1), date(2025, 2, 1))
        rows = (LiveCustodyDistributionRow(code="A", values=(Decimal(1),), percentage=None),)
        with self.assertRaises(ValueError):
            calculate_live_distribution_features(self._response(periods, rows))


class LiveStatsFeaturesTests(unittest.TestCase):
    def _series(self, points: tuple[LiveCustodyStatsPoint, ...]) -> LiveCustodyStatsSeries:
        return LiveCustodyStatsSeries(
            format=LiveCustodyStatsFormat(fields=(("decimals", 2),)), points=points,
        )

    def test_fewer_than_two_non_null_points_yields_none(self) -> None:
        series = self._series((
            LiveCustodyStatsPoint(date=date(2025, 1, 1), value=Decimal("1"), label=None),
            LiveCustodyStatsPoint(date=date(2025, 1, 2), value=None, label=None),
        ))
        response = LiveCustodyStatsResponse(
            request_id=None, cached=False,
            retail_percentage=series, institution_percentage=series, investor_count=series,
        )
        features = calculate_live_stats_features(response)
        self.assertIsNone(features.retail_percentage.change)
        self.assertEqual(features.retail_percentage.first_date, date(2025, 1, 1))
        self.assertEqual(features.retail_percentage.last_date, date(2025, 1, 1))
        self.assertEqual(features.retail_percentage.non_null_point_count, 1)

    def test_unordered_dates_use_first_to_last_by_date_and_skip_nulls(self) -> None:
        series = self._series((
            LiveCustodyStatsPoint(date=date(2025, 3, 1), value=Decimal("30"), label=None),
            LiveCustodyStatsPoint(date=date(2025, 1, 1), value=Decimal("10"), label=None),
            LiveCustodyStatsPoint(date=date(2025, 2, 1), value=None, label=None),
        ))
        response = LiveCustodyStatsResponse(
            request_id=None, cached=False,
            retail_percentage=series, institution_percentage=series, investor_count=series,
        )
        features = calculate_live_stats_features(response)
        self.assertEqual(features.retail_percentage.change, Decimal("20"))
        self.assertEqual(features.retail_percentage.first_date, date(2025, 1, 1))
        self.assertEqual(features.retail_percentage.last_date, date(2025, 3, 1))
        self.assertEqual(features.retail_percentage.non_null_point_count, 2)

    def test_no_points_yields_none_dates(self) -> None:
        series = self._series(())
        response = LiveCustodyStatsResponse(
            request_id=None, cached=False,
            retail_percentage=series, institution_percentage=series, investor_count=series,
        )
        features = calculate_live_stats_features(response)
        self.assertIsNone(features.retail_percentage.change)
        self.assertIsNone(features.retail_percentage.first_date)
        self.assertIsNone(features.retail_percentage.last_date)
        self.assertEqual(features.retail_percentage.non_null_point_count, 0)


class LiveFeaturesFormulaVersionTests(unittest.TestCase):
    def test_formula_version_and_formulas_are_present(self) -> None:
        self.assertEqual(FORMULA_VERSION, "live_features_v1")
        for key in (
            "live_akd_top5_positive_net_share", "live_akd_top5_negative_net_share",
            "live_custodian_top5_value_share", "live_distribution_change",
            "live_stats_series_change",
        ):
            self.assertIn(key, FORMULAS)


class LiveContextComputedFeaturesTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_contains_computed_features_and_is_json_serializable(self) -> None:
        evidence = await _evidence()
        # Use tighter limits than the fixture row counts so truncation is exercised,
        # and confirm computed_features is still derived from the FULL response.
        context = build_live_analysis_context(
            evidence, generated_at=datetime(2026, 9, 24, tzinfo=timezone.utc), limits=LIMITS,
        )
        data = context.to_dict()
        computed = data["computed_features"]
        self.assertEqual(computed["formula_version"], "live_features_v1")
        self.assertIn("formulas", computed)

        expected_akd = calculate_live_akd_features(evidence.akd.source)
        self.assertEqual(computed["akd"]["row_count"], expected_akd.row_count)
        self.assertEqual(computed["akd"]["net_sum_as_received"], expected_akd.net_sum_as_received)

        expected_custodian = calculate_live_custodian_features(evidence.custodian.source)
        self.assertEqual(
            computed["custodian"]["total_value"], format(expected_custodian.total_value, "f"),
        )

        expected_distribution = calculate_live_distribution_features(evidence.custody_distribution)
        self.assertEqual(
            computed["custody_distribution"]["latest_period"],
            expected_distribution.latest_period.isoformat(),
        )
        self.assertEqual(
            computed["custody_distribution"]["row_count"], expected_distribution.row_count,
        )

        expected_stats = calculate_live_stats_features(evidence.custody_stats)
        self.assertEqual(
            computed["custody_stats"]["retail_percentage"]["non_null_point_count"],
            expected_stats.retail_percentage.non_null_point_count,
        )

        # computed_features is derived over ALL rows, independent of the row-selection limits.
        self.assertEqual(computed["akd"]["row_count"], len(evidence.akd.rows))
        self.assertGreaterEqual(computed["akd"]["row_count"], data["akd"]["included_rows"])

        # Must round-trip through JSON (no Decimal/date objects left over).
        reparsed = json.loads(context.to_json())
        self.assertEqual(reparsed["computed_features"]["formula_version"], "live_features_v1")


if __name__ == "__main__":
    unittest.main()
