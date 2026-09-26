"""Offline tests for ``fintr.features.custody_flow``.

All codes, dates and lot/AKD values are synthetic fixtures; no captured
brokerage data is used or reproduced.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from fintr.data.period_capture import (
    BrokerageDirectoryInfo,
    CodeValue,
    DatedCodeValues,
    PeriodCapture,
    PeriodMetadataDay,
)
from fintr.features.custody_flow import (
    CodeSet,
    DEFAULT_MISSING_AKD_POLICY,
    FORMULA_VERSION,
    RETAIL_LOT_LABEL,
    RETAIL_LOT_VALIDATION_STATUS,
    RETAIL_LOT_VALIDATION_STUDY_FILE,
    compute_code_set_series,
    compute_custody_residual,
    compute_estimated_retail_lots,
    find_same_day_pairs,
    load_code_set,
)


D1, D2, D3, D4, D5 = (
    date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 7), date(2026, 5, 8),
)


def _capture(
    daily_custody_lots, daily_akd_net, server_dates=(D1, D2, D3, D4, D5), metadata=(),
) -> PeriodCapture:
    return PeriodCapture(
        symbol="TEST", start=server_dates[0], end=server_dates[-1],
        captured_at_utc="2026-05-09T00:00:00Z", packet_sha256="deadbeef",
        server_dates=server_dates, period_akd_net=(),
        daily_akd_net=daily_akd_net, daily_akd_market=(),
        daily_custody_lots=daily_custody_lots,
        custodian_diff_start=server_dates[0], custodian_diff_end=server_dates[-1],
        custodian_diff=(), metadata=metadata,
        brokerage_directory=BrokerageDirectoryInfo(present=False, row_count=0, index=None),
    )


class CustodyResidualTests(unittest.TestCase):
    def test_residual_matches_formula_with_default_lag(self):
        # lag=2: residual(t=D3) uses custody(D3)-custody(D2) minus akd_net(D1).
        custody = (
            DatedCodeValues(D1, (CodeValue("AAA", Decimal(100)),)),
            DatedCodeValues(D2, (CodeValue("AAA", Decimal(120)),)),
            DatedCodeValues(D3, (CodeValue("AAA", Decimal(150)),)),
        )
        akd = (DatedCodeValues(D1, (CodeValue("AAA", Decimal(25)),)),)
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        result = compute_custody_residual(capture, lag=2)
        self.assertEqual(result.formula_version, FORMULA_VERSION)
        self.assertEqual(len(result.days), 1)
        day = result.days[0]
        self.assertEqual(day.date, D3)
        self.assertEqual(day.previous_date, D2)
        self.assertEqual(day.akd_date, D1)
        # (150 - 120) - 25 = 5
        self.assertEqual(day.by_code(), {"AAA": Decimal(5)})

    def test_residual_none_when_akd_code_not_listed_under_unknown_policy(self):
        """Under missing_akd_policy='unknown' (previous default), a code
        missing from AKD at the anchor date is None, never 0."""
        custody = (
            DatedCodeValues(D1, (CodeValue("BBB", Decimal(10)),)),
            DatedCodeValues(D2, (CodeValue("BBB", Decimal(20)),)),
            DatedCodeValues(D3, (CodeValue("BBB", Decimal(35)),)),
        )
        akd = (DatedCodeValues(D1, ()),)  # AKD panel exists on D1, BBB not listed in it
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        result = compute_custody_residual(capture, lag=2, missing_akd_policy="unknown")
        self.assertEqual(result.missing_akd_policy, "unknown")
        self.assertIsNone(result.days[0].by_code()["BBB"])
        self.assertFalse(result.days[0].rows[0].akd_defaulted)

    def test_residual_defaults_akd_to_zero_under_default_zero_policy(self):
        """Default policy is 'zero': a code missing from an otherwise-present
        AKD panel is treated as net 0 for that date, per the 2026-09-26
        decision. The result records the policy and the akd_defaulted flag."""
        custody = (
            DatedCodeValues(D1, (CodeValue("BBB", Decimal(10)),)),
            DatedCodeValues(D2, (CodeValue("BBB", Decimal(20)),)),
            DatedCodeValues(D3, (CodeValue("BBB", Decimal(35)),)),
        )
        akd = (DatedCodeValues(D1, ()),)  # AKD panel exists on D1, BBB not listed in it
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        result = compute_custody_residual(capture, lag=2)
        self.assertEqual(result.missing_akd_policy, DEFAULT_MISSING_AKD_POLICY)
        self.assertEqual(DEFAULT_MISSING_AKD_POLICY, "zero")
        # (35 - 20) - 0 = 15
        self.assertEqual(result.days[0].by_code()["BBB"], Decimal(15))
        self.assertTrue(result.days[0].rows[0].akd_defaulted)

    def test_residual_none_when_whole_day_akd_panel_missing_under_either_policy(self):
        """If the AKD panel for the anchor date is absent from the capture
        entirely (no DatedCodeValues for it), the residual stays None under
        BOTH policies -- 'zero' fills a missing code within a present panel,
        never a missing panel."""
        custody = (
            DatedCodeValues(D1, (CodeValue("CCC", Decimal(10)),)),
            DatedCodeValues(D2, (CodeValue("CCC", Decimal(20)),)),
            DatedCodeValues(D3, (CodeValue("CCC", Decimal(35)),)),
        )
        akd = ()  # no AKD panel at all for D1 (or any date)
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        for policy in ("zero", "unknown"):
            result = compute_custody_residual(capture, lag=2, missing_akd_policy=policy)
            self.assertIsNone(result.days[0].by_code()["CCC"])
            self.assertFalse(result.days[0].rows[0].akd_defaulted)

    def test_residual_none_when_custody_missing_even_under_zero_policy(self):
        """Custody-lot inputs are never defaulted, regardless of missing_akd_policy."""
        custody = (
            DatedCodeValues(D1, ()),
            DatedCodeValues(D2, (CodeValue("DDD", Decimal(20)),)),
            DatedCodeValues(D3, (CodeValue("DDD", Decimal(35)),)),
        )
        akd = (DatedCodeValues(D1, (CodeValue("DDD", Decimal(5)),)),)
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        result = compute_custody_residual(capture, lag=1, missing_akd_policy="zero")
        d2_day = next(d for d in result.days if d.date == D2)
        self.assertIsNone(d2_day.by_code()["DDD"])

    def test_missing_akd_policy_must_be_valid(self):
        capture = _capture((), (), server_dates=(D1,))
        with self.assertRaises(ValueError):
            compute_custody_residual(capture, lag=1, missing_akd_policy="bogus")

    def test_residual_none_when_custody_code_not_listed_on_previous_day(self):
        """CCC is listed at D2 but has no custody row at D1 (the previous
        server date for lag=1) -> the custody-change input is undefined, so
        the residual is None rather than treating the missing lot as 0."""
        custody = (
            DatedCodeValues(D1, ()),
            DatedCodeValues(D2, (CodeValue("CCC", Decimal(20)),)),
            DatedCodeValues(D3, (CodeValue("CCC", Decimal(35)),)),
        )
        akd = (DatedCodeValues(D1, (CodeValue("CCC", Decimal(5)),)),)
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        result = compute_custody_residual(capture, lag=1)
        d2_day = next(d for d in result.days if d.date == D2)
        self.assertIsNone(d2_day.by_code()["CCC"])

    def test_lag_must_be_positive_int(self):
        capture = _capture((), (), server_dates=(D1,))
        with self.assertRaises(ValueError):
            compute_custody_residual(capture, lag=0)
        with self.assertRaises(ValueError):
            compute_custody_residual(capture, lag=-1)

    def test_rejects_non_capture_input(self):
        with self.assertRaises(TypeError):
            compute_custody_residual("not a capture")  # type: ignore[arg-type]


class SameDayPairTests(unittest.TestCase):
    def _residual_result_for_pairs(self):
        # Build a capture where D3's residuals are AAA=+50, BBB=-50, CCC=+10,
        # DDD=None (missing AKD), EEE=0 (should be excluded, not a pair).
        custody = (
            DatedCodeValues(D1, (
                CodeValue("AAA", Decimal(100)), CodeValue("BBB", Decimal(100)),
                CodeValue("CCC", Decimal(100)), CodeValue("DDD", Decimal(100)),
                CodeValue("EEE", Decimal(100)),
            )),
            DatedCodeValues(D2, (
                CodeValue("AAA", Decimal(100)), CodeValue("BBB", Decimal(100)),
                CodeValue("CCC", Decimal(100)), CodeValue("DDD", Decimal(100)),
                CodeValue("EEE", Decimal(100)),
            )),
            DatedCodeValues(D3, (
                CodeValue("AAA", Decimal(150)), CodeValue("BBB", Decimal(50)),
                CodeValue("CCC", Decimal(110)), CodeValue("DDD", Decimal(140)),
                CodeValue("EEE", Decimal(100)),
            )),
        )
        akd = (DatedCodeValues(D1, (
            CodeValue("AAA", Decimal(0)), CodeValue("BBB", Decimal(0)),
            CodeValue("CCC", Decimal(0)), CodeValue("EEE", Decimal(0)),
            # DDD deliberately not listed here.
        )),)
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        # This fixture is about same-day pairing, not the missing-AKD policy,
        # so pin the previous ('unknown') behaviour: DDD stays None here
        # rather than being defaulted to 0 under the new default policy.
        return compute_custody_residual(capture, lag=2, missing_akd_policy="unknown")

    def test_exact_pair_found_and_zero_excluded(self):
        residuals = self._residual_result_for_pairs()
        result = find_same_day_pairs(residuals)
        self.assertEqual(len(result.pairs), 1)
        pair = result.pairs[0]
        self.assertEqual(pair.date, D3)
        self.assertEqual((pair.code_a, pair.code_b), ("AAA", "BBB"))
        self.assertEqual(pair.residual_a, Decimal(50))
        self.assertEqual(pair.residual_b, Decimal(-50))
        # DDD (None) and EEE (0) never appear in any pair.
        codes_in_pairs = {pair.code_a, pair.code_b}
        self.assertNotIn("DDD", codes_in_pairs)
        self.assertNotIn("EEE", codes_in_pairs)

    def test_each_code_used_at_most_once_per_day_deterministic(self):
        # Three codes with residual +10 and one with -10: only one pair forms,
        # and it is the deterministic (sorted-order) first match.
        custody = (
            DatedCodeValues(D1, (
                CodeValue("A", Decimal(0)), CodeValue("B", Decimal(0)),
                CodeValue("C", Decimal(0)), CodeValue("D", Decimal(0)),
            )),
            DatedCodeValues(D2, (
                CodeValue("A", Decimal(0)), CodeValue("B", Decimal(0)),
                CodeValue("C", Decimal(0)), CodeValue("D", Decimal(0)),
            )),
            DatedCodeValues(D3, (
                CodeValue("A", Decimal(10)), CodeValue("B", Decimal(10)),
                CodeValue("C", Decimal(10)), CodeValue("D", Decimal(-10)),
            )),
        )
        akd = (DatedCodeValues(D1, (
            CodeValue("A", Decimal(0)), CodeValue("B", Decimal(0)),
            CodeValue("C", Decimal(0)), CodeValue("D", Decimal(0)),
        )),)
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        residuals = compute_custody_residual(capture, lag=2)
        result = find_same_day_pairs(residuals)
        self.assertEqual(len(result.pairs), 1)
        # Sorted order is A,B,C,D; A is matched first against D.
        self.assertEqual((result.pairs[0].code_a, result.pairs[0].code_b), ("A", "D"))

    def test_tolerance_widens_matching(self):
        custody = (
            DatedCodeValues(D1, (CodeValue("X", Decimal(0)), CodeValue("Y", Decimal(0)))),
            DatedCodeValues(D2, (CodeValue("X", Decimal(0)), CodeValue("Y", Decimal(0)))),
            DatedCodeValues(D3, (CodeValue("X", Decimal(51)), CodeValue("Y", Decimal(-50)))),
        )
        akd = (DatedCodeValues(D1, (CodeValue("X", Decimal(0)), CodeValue("Y", Decimal(0)))),)
        capture = _capture(custody, akd, server_dates=(D1, D2, D3))
        residuals = compute_custody_residual(capture, lag=2)
        self.assertEqual(len(find_same_day_pairs(residuals).pairs), 0)
        widened = find_same_day_pairs(residuals, tolerance=Decimal(1))
        self.assertEqual(len(widened.pairs), 1)

    def test_negative_tolerance_rejected(self):
        residuals = self._residual_result_for_pairs()
        with self.assertRaises(ValueError):
            find_same_day_pairs(residuals, tolerance=Decimal(-1))


class CodeSetSeriesTests(unittest.TestCase):
    def _capture_for_code_set(self):
        custody = (
            DatedCodeValues(D1, (CodeValue("MDS", Decimal(100)), CodeValue("ZRY", Decimal(200)))),
            DatedCodeValues(D2, (CodeValue("MDS", Decimal(110)), CodeValue("ZRY", Decimal(190)))),
            # D3: VKY missing entirely, MDS also missing -> only ZRY listed.
            DatedCodeValues(D3, (CodeValue("ZRY", Decimal(210)),)),
        )
        return _capture(custody, (), server_dates=(D1, D2, D3))

    def test_sum_counts_and_change(self):
        capture = self._capture_for_code_set()
        code_set = CodeSet(name="three_institution_rule", version="v1", codes=("MDS", "ZRY", "VKY"))
        result = compute_code_set_series(capture, code_set)
        self.assertEqual(result.formula_version, FORMULA_VERSION)
        self.assertEqual(result.code_set_name, "three_institution_rule")
        d1, d2, d3 = result.days
        self.assertEqual(d1.total_lots, Decimal(300))
        self.assertEqual(d1.listed_count, 2)
        self.assertEqual(d1.missing_count, 1)
        self.assertEqual(d1.missing_codes, ("VKY",))
        self.assertIsNone(d1.change)
        self.assertEqual(d2.total_lots, Decimal(300))
        self.assertEqual(d2.change, Decimal(0))
        # D3: only ZRY listed (210); MDS/VKY missing and NOT treated as 0.
        self.assertEqual(d3.total_lots, Decimal(210))
        self.assertEqual(d3.listed_count, 1)
        self.assertEqual(d3.missing_count, 2)
        self.assertEqual(set(d3.missing_codes), {"MDS", "VKY"})
        self.assertEqual(d3.change, Decimal(210) - Decimal(300))

    def test_rejects_non_code_set(self):
        capture = self._capture_for_code_set()
        with self.assertRaises(TypeError):
            compute_code_set_series(capture, ["MDS", "ZRY"])  # type: ignore[arg-type]


class EstimatedRetailLotsTests(unittest.TestCase):
    def test_formula_and_metadata(self):
        custody = (
            DatedCodeValues(D1, (CodeValue("AAA", Decimal(60)), CodeValue("BBB", Decimal(40)))),
            DatedCodeValues(D2, (CodeValue("AAA", Decimal(70)), CodeValue("BBB", Decimal(30)))),
        )
        metadata = (
            PeriodMetadataDay(D1, Decimal("40.00"), Decimal("60.00"), ()),
            PeriodMetadataDay(D2, Decimal("50.00"), Decimal("50.00"), ()),
        )
        capture = _capture(custody, (), server_dates=(D1, D2), metadata=metadata)
        result = compute_estimated_retail_lots(capture, provisional_last_custody_days=0)
        self.assertEqual(result.formula_version, FORMULA_VERSION)
        self.assertEqual(result.label, RETAIL_LOT_LABEL)
        self.assertEqual(result.validation_status, RETAIL_LOT_VALIDATION_STATUS)
        self.assertEqual(result.validation_study_file, RETAIL_LOT_VALIDATION_STUDY_FILE)
        self.assertEqual(result.provisional_last_custody_days, 0)
        d1, d2 = result.days
        self.assertEqual(d1.total_custody_lots, Decimal(100))
        # 40/100 * 100 = 40; 60/100 * 100 = 60
        self.assertEqual(d1.estimated_retail_lots, Decimal(40))
        self.assertEqual(d1.estimated_institutional_lots, Decimal(60))
        self.assertEqual(d2.total_custody_lots, Decimal(100))
        self.assertEqual(d2.estimated_retail_lots, Decimal(50))
        self.assertEqual(d2.estimated_institutional_lots, Decimal(50))
        self.assertFalse(d1.provisional)
        self.assertFalse(d2.provisional)

    def test_null_retail_pct_gives_none_no_forward_fill(self):
        custody = (
            DatedCodeValues(D1, (CodeValue("AAA", Decimal(100)),)),
            DatedCodeValues(D2, (CodeValue("AAA", Decimal(120)),)),
        )
        metadata = (
            PeriodMetadataDay(D1, Decimal("40.00"), Decimal("60.00"), ()),
            PeriodMetadataDay(D2, None, None, ("retail_pct", "institutional_pct")),
        )
        capture = _capture(custody, (), server_dates=(D1, D2), metadata=metadata)
        result = compute_estimated_retail_lots(capture, provisional_last_custody_days=0)
        d1, d2 = result.days
        self.assertEqual(d1.estimated_retail_lots, Decimal(40))
        # D2's null retail_pct is NOT forward-filled from D1.
        self.assertIsNone(d2.estimated_retail_lots)
        self.assertIsNone(d2.estimated_institutional_lots)
        self.assertIsNone(d2.retail_pct)
        # D2's own custody total is still computed even though pct is null.
        self.assertEqual(d2.total_custody_lots, Decimal(120))

    def test_missing_custody_panel_gives_none_even_with_pct_present(self):
        custody = (
            DatedCodeValues(D1, (CodeValue("AAA", Decimal(100)),)),
            # D2: no custody panel at all.
        )
        metadata = (
            PeriodMetadataDay(D1, Decimal("40.00"), Decimal("60.00"), ()),
            PeriodMetadataDay(D2, Decimal("45.00"), Decimal("55.00"), ()),
        )
        capture = _capture(custody, (), server_dates=(D1, D2), metadata=metadata)
        result = compute_estimated_retail_lots(capture, provisional_last_custody_days=0)
        d2 = result.days[1]
        self.assertIsNone(d2.total_custody_lots)
        self.assertIsNone(d2.estimated_retail_lots)
        self.assertIsNone(d2.estimated_institutional_lots)
        # The percentage itself is still recorded even though total is undefined.
        self.assertEqual(d2.retail_pct, Decimal("45.00"))

    def test_provisional_marking_default_marks_last_date_only(self):
        custody = (
            DatedCodeValues(D1, (CodeValue("AAA", Decimal(100)),)),
            DatedCodeValues(D2, (CodeValue("AAA", Decimal(110)),)),
            DatedCodeValues(D3, (CodeValue("AAA", Decimal(120)),)),
        )
        metadata = tuple(
            PeriodMetadataDay(d, Decimal("40.00"), Decimal("60.00"), ()) for d in (D1, D2, D3)
        )
        capture = _capture(custody, (), server_dates=(D1, D2, D3), metadata=metadata)
        result = compute_estimated_retail_lots(capture)  # default provisional_last_custody_days=1
        self.assertEqual(result.provisional_last_custody_days, 1)
        d1, d2, d3 = result.days
        self.assertFalse(d1.provisional)
        self.assertFalse(d2.provisional)
        self.assertTrue(d3.provisional)

    def test_provisional_last_custody_days_widens_marking(self):
        custody = (
            DatedCodeValues(D1, (CodeValue("AAA", Decimal(100)),)),
            DatedCodeValues(D2, (CodeValue("AAA", Decimal(110)),)),
            DatedCodeValues(D3, (CodeValue("AAA", Decimal(120)),)),
        )
        metadata = tuple(
            PeriodMetadataDay(d, Decimal("40.00"), Decimal("60.00"), ()) for d in (D1, D2, D3)
        )
        capture = _capture(custody, (), server_dates=(D1, D2, D3), metadata=metadata)
        result = compute_estimated_retail_lots(capture, provisional_last_custody_days=2)
        d1, d2, d3 = result.days
        self.assertFalse(d1.provisional)
        self.assertTrue(d2.provisional)
        self.assertTrue(d3.provisional)

    def test_rejects_negative_provisional_days(self):
        capture = _capture((), (), server_dates=(D1,))
        with self.assertRaises(ValueError):
            compute_estimated_retail_lots(capture, provisional_last_custody_days=-1)

    def test_rejects_non_capture_input(self):
        with self.assertRaises(TypeError):
            compute_estimated_retail_lots("not a capture")  # type: ignore[arg-type]


class LoadCodeSetTests(unittest.TestCase):
    def test_loads_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "code_set.json"
            path.write_text(
                json.dumps({"name": "three_institution_rule", "version": "v1",
                            "codes": ["MDS", "ZRY", "VKY"]}),
                encoding="utf-8",
            )
            code_set = load_code_set(path)
            self.assertEqual(code_set, CodeSet("three_institution_rule", "v1", ("MDS", "ZRY", "VKY")))

    def test_rejects_duplicate_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "code_set.json"
            path.write_text(
                json.dumps({"name": "n", "version": "v1", "codes": ["MDS", "MDS"]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_code_set(path)

    def test_rejects_empty_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "code_set.json"
            path.write_text(json.dumps({"name": "n", "version": "v1", "codes": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_code_set(path)


if __name__ == "__main__":
    unittest.main()
