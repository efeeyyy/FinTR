"""Offline tests for the ADR §3 rolling-window composer.

All captures are synthetic, built directly as ``PeriodCapture`` objects (the
same pattern ``tests/test_period_analysis.py`` uses for feature-layer
tests) -- no captured market data, no network. One test reuses
``test_period_analysis``'s packet/capture JSON helpers specifically to
exercise ``load_period_capture``'s existing packet-hash check, since the
composer relies on that check for its own inputs rather than re-validating
them (see the module docstring in ``fintr.research.compose_period_capture``).
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
    PeriodCaptureError,
    PeriodMetadataDay,
    parse_period_capture,
)
from fintr.data.period_composition import (
    CompositionBrokerageSource,
    CompositionLineage,
)
from fintr.inference.period_report import build_period_report
from fintr.research.compose_period_capture import (
    CaptureInput,
    CompositionError,
    compose_period_capture,
)

import test_period_analysis as period_fixtures


D1, D2, D3, D4, D5, D6 = (date(2026, 8, day) for day in (18, 19, 20, 21, 24, 25))


def _dated_code_values(days: dict) -> tuple[DatedCodeValues, ...]:
    return tuple(
        DatedCodeValues(day, tuple(
            CodeValue(code, Decimal(value)) for code, value in rows.items()
        ))
        for day, rows in sorted(days.items())
    )


def _dated_market_values(days: dict) -> tuple[DatedMarketProxyValues, ...]:
    return tuple(
        DatedMarketProxyValues(day, tuple(
            DailyMarketProxyRow(code, Decimal(size), Decimal(cost))
            for code, (size, cost) in rows.items()
        ))
        for day, rows in sorted(days.items())
    )


def _metadata_days(days: dict) -> tuple[PeriodMetadataDay, ...]:
    return tuple(
        PeriodMetadataDay(
            day, spec.get("retail_pct"), spec.get("institutional_pct"),
            tuple(spec.get("null_fields", ())), spec.get("investor_count"),
        )
        for day, spec in sorted(days.items())
    )


def _capture(
    *, symbol="TEST", start, end, captured_at_utc, packet_sha256, server_dates,
    akd=None, market=None, custody=None, metadata=None,
    brokerage_present=False, brokerage_rows=(),
    period_akd_net=(), custodian_diff=(), composition=None,
) -> PeriodCapture:
    return PeriodCapture(
        symbol=symbol, start=start, end=end, captured_at_utc=captured_at_utc,
        packet_sha256=packet_sha256, server_dates=tuple(sorted(server_dates)),
        period_akd_net=period_akd_net, daily_akd_net=_dated_code_values(akd or {}),
        daily_akd_market=_dated_market_values(market or {}),
        daily_custody_lots=_dated_code_values(custody or {}),
        custodian_diff_start=start, custodian_diff_end=end, custodian_diff=custodian_diff,
        metadata=_metadata_days(metadata or {}),
        brokerage_directory=BrokerageDirectoryInfo(
            present=brokerage_present, row_count=len(brokerage_rows), index=None,
        ),
        composition=composition,
    )


def _base_and_rolling() -> tuple[CaptureInput, CaptureInput]:
    base = _capture(
        start=D1, end=D4, captured_at_utc="2026-09-01T00:00:00Z", packet_sha256="base_hash",
        server_dates=(D1, D2, D3, D4),
        akd={D1: {"AAA": "10"}, D2: {"AAA": "20"}, D3: {"AAA": "30"}, D4: {"AAA": "40"}},
        market={
            D1: {"AAA": ("100", "5")}, D2: {"AAA": ("100", "5")},
            D3: {"AAA": ("100", "5")}, D4: {"AAA": ("100", "5")},
        },
        custody={
            D1: {"AAA": "1000"}, D2: {"AAA": "1000"},
            D3: {"AAA": "1000"}, D4: {"AAA": "1000"},
        },
        metadata={d: {"retail_pct": Decimal("40")} for d in (D1, D2, D3, D4)},
        brokerage_present=False,
    )
    # Rolling window request S=D3, E=D6; the provider only returned up to
    # D5 (D6, the requested-but-missing day, becomes a gap, never filled).
    rolling = _capture(
        start=D3, end=D6, captured_at_utc="2026-09-15T00:00:00Z", packet_sha256="roll_hash",
        server_dates=(D3, D4, D5),
        akd={D3: {"AAA": "35"}, D4: {"AAA": "40"}, D5: {"AAA": "50"}},
        market={D3: {"AAA": ("100", "5")}, D4: {"AAA": ("100", "5")}, D5: {"AAA": ("100", "5")}},
        custody={D3: {"AAA": "1000"}, D4: {"AAA": "1005"}, D5: {"AAA": "1010"}},
        metadata={D3: {"retail_pct": Decimal("41")}, D4: {"retail_pct": Decimal("41")},
                  D5: {"retail_pct": Decimal("41")}},
        brokerage_present=True, brokerage_rows=(("AAA", "Title", "Short"),),
    )
    return (
        CaptureInput(base, kind="base", label="base.json"),
        CaptureInput(rolling, kind="rolling", label="rolling.json"),
    )


class ComposeRulesTests(unittest.TestCase):
    def test_rule1_family_selection_is_independent_and_latest_wins(self):
        base_input, rolling_input = _base_and_rolling()
        composed = compose_period_capture([base_input, rolling_input])

        akd = {day.date: day.by_code()["AAA"] for day in composed.daily_akd_net}
        self.assertEqual(akd, {
            D1: Decimal("10"), D2: Decimal("20"),
            D3: Decimal("35"),  # rolling wins (revision)
            D4: Decimal("40"), D5: Decimal("50"),
        })
        custody = {day.date: day.by_code()["AAA"] for day in composed.daily_custody_lots}
        self.assertEqual(custody[D4], Decimal("1005"))  # rolling wins here too

    def test_rule2_server_dates_is_union_and_gaps_never_filled(self):
        base_input, rolling_input = _base_and_rolling()
        composed_no_calendar = compose_period_capture([base_input, rolling_input])
        self.assertEqual(composed_no_calendar.server_dates, (D1, D2, D3, D4, D5))
        self.assertEqual(composed_no_calendar.composition.gaps, ())

        composed = compose_period_capture(
            [base_input, rolling_input], tradeable_dates=(D1, D2, D3, D4, D5, D6),
        )
        self.assertEqual(composed.composition.gaps, (D6,))
        # D6 is requested (rolling.end) but never returned by any input --
        # it must never appear in any composed dated series.
        self.assertNotIn(D6, {day.date for day in composed.daily_akd_net})
        self.assertNotIn(D6, composed.server_dates)

    def test_rule3_revisions_recorded_with_packet_hashes_and_code_counts(self):
        base_input, rolling_input = _base_and_rolling()
        composed = compose_period_capture([base_input, rolling_input])
        revisions = {(r.date, r.family): r for r in composed.composition.revisions}

        self.assertIn((D3, "akd"), revisions)
        akd_rev = revisions[(D3, "akd")]
        self.assertEqual(akd_rev.superseded_packet_sha256, "base_hash")
        self.assertEqual(akd_rev.winning_packet_sha256, "roll_hash")
        self.assertEqual(akd_rev.differing_code_count, 1)

        self.assertIn((D4, "custody"), revisions)
        self.assertIn((D3, "metadata"), revisions)
        self.assertIn((D4, "metadata"), revisions)
        self.assertEqual(revisions[(D3, "metadata")].differing_code_count, 1)

        # D4 akd and D3/D4 akd_market agree between inputs: no revision.
        self.assertNotIn((D4, "akd"), revisions)
        self.assertNotIn((D3, "akd_market"), revisions)
        self.assertNotIn((D4, "akd_market"), revisions)
        self.assertNotIn((D3, "custody"), revisions)

    def test_rule4_settlement(self):
        base_input, rolling_input = _base_and_rolling()
        composed = compose_period_capture([base_input, rolling_input])
        unsettled = {(u.date, u.family) for u in composed.composition.unsettled}

        # AKD: D4 confirmed identical by the two latest observations -> settled.
        self.assertNotIn((D4, "akd"), unsettled)
        # D3 just revised (30 -> 35), not yet reconfirmed -> unsettled.
        self.assertIn((D3, "akd"), unsettled)
        # Single-observation days (D1/D2 base-only, D5 rolling-only) are
        # not yet confirmed by a second observation -> unsettled.
        self.assertIn((D1, "akd"), unsettled)
        self.assertIn((D2, "akd"), unsettled)
        self.assertIn((D5, "akd"), unsettled)

        # Custody: D1-D3 reach T+2 within some input and match -> settled.
        self.assertNotIn((D1, "custody"), unsettled)
        self.assertNotIn((D2, "custody"), unsettled)
        self.assertNotIn((D3, "custody"), unsettled)
        # D4 just revised and no input reaches T+2 past it -> unsettled.
        self.assertIn((D4, "custody"), unsettled)
        # D5 is the last server date anywhere -> never reaches T+2 -> unsettled.
        self.assertIn((D5, "custody"), unsettled)

    def test_rule5_period_aggregates_are_dropped_and_marked_unavailable(self):
        base_input, rolling_input = _base_and_rolling()
        composed = compose_period_capture([base_input, rolling_input])
        self.assertEqual(composed.period_akd_net, ())
        self.assertEqual(composed.custodian_diff, ())
        self.assertFalse(composed.composition.period_aggregates_available)

    def test_rule6_brokerage_directory_from_latest_captured_at_input(self):
        base_input, rolling_input = _base_and_rolling()
        composed = compose_period_capture([base_input, rolling_input])
        self.assertTrue(composed.brokerage_directory.present)  # rolling's, not base's
        source = composed.composition.brokerage_directory_source
        self.assertEqual(source.kind, "rolling")
        self.assertEqual(source.packet_sha256, "roll_hash")

    def test_rule7_deterministic_regardless_of_input_order(self):
        base_input, rolling_input = _base_and_rolling()
        forward = compose_period_capture([base_input, rolling_input])
        backward = compose_period_capture([rolling_input, base_input])
        self.assertEqual(forward.composition.composition_sha256,
                          backward.composition.composition_sha256)
        self.assertEqual(forward.daily_akd_net, backward.daily_akd_net)
        self.assertEqual(forward.daily_custody_lots, backward.daily_custody_lots)
        self.assertEqual(forward.packet_sha256, backward.packet_sha256)

        # Same underlying data, freshly rebuilt: identical composition hash.
        base_input2, rolling_input2 = _base_and_rolling()
        rebuilt = compose_period_capture([base_input2, rolling_input2])
        self.assertEqual(forward.composition.composition_sha256,
                          rebuilt.composition.composition_sha256)


class ComposeErrorTests(unittest.TestCase):
    def test_no_captures_rejected(self):
        with self.assertRaisesRegex(CompositionError, "no_captures"):
            compose_period_capture([])

    def test_symbol_mismatch_rejected(self):
        base_input, rolling_input = _base_and_rolling()
        other = _capture(
            symbol="OTHER", start=D1, end=D1, captured_at_utc="2026-09-01T00:00:00Z",
            packet_sha256="other_hash", server_dates=(D1,),
        )
        with self.assertRaisesRegex(CompositionError, "symbol_mismatch"):
            compose_period_capture([base_input, CaptureInput(other, kind="rolling")])

    def test_duplicate_packet_hash_rejected(self):
        base_input, _rolling_input = _base_and_rolling()
        duplicate = CaptureInput(base_input.capture, kind="rolling")
        with self.assertRaisesRegex(CompositionError, "duplicate_capture_packet"):
            compose_period_capture([base_input, duplicate])

    def test_invalid_input_kind_rejected(self):
        base_input, _rolling_input = _base_and_rolling()
        with self.assertRaisesRegex(CompositionError, "invalid_input_kind"):
            CaptureInput(base_input.capture, kind="weekly")

    def test_unparseable_captured_at_utc_rejected(self):
        bad = _capture(
            start=D1, end=D1, captured_at_utc="not-a-timestamp", packet_sha256="bad_hash",
            server_dates=(D1,),
        )
        with self.assertRaisesRegex(CompositionError, "unparseable_captured_at_utc"):
            compose_period_capture([CaptureInput(bad, kind="base")])


class PeriodCaptureCompositionInvariantTests(unittest.TestCase):
    """``PeriodCapture.__post_init__`` enforces rule 5 even on direct construction."""

    def _lineage(self, **overrides) -> CompositionLineage:
        fields = dict(
            schema="fintr_period_composition_v1", symbol="TEST",
            composer_version="compose_period_capture_v1", inputs=(), selection=(),
            gaps=(), revisions=(), unsettled=(),
            brokerage_directory_source=CompositionBrokerageSource(
                kind="base", captured_at_utc="2026-09-01T00:00:00Z", packet_sha256="x",
            ),
            period_aggregates_available=False, composition_sha256="y",
        )
        fields.update(overrides)
        return CompositionLineage(**fields)

    def test_period_aggregates_available_true_is_rejected(self):
        with self.assertRaisesRegex(
            PeriodCaptureError, "composed_capture_must_mark_period_aggregates_unavailable",
        ):
            _capture(
                start=D1, end=D1, captured_at_utc="2026-09-01T00:00:00Z",
                packet_sha256="x", server_dates=(D1,),
                composition=self._lineage(period_aggregates_available=True),
            )

    def test_nonempty_period_aggregates_with_composition_is_rejected(self):
        with self.assertRaisesRegex(
            PeriodCaptureError, "composed_capture_must_not_carry_period_aggregates",
        ):
            _capture(
                start=D1, end=D1, captured_at_utc="2026-09-01T00:00:00Z",
                packet_sha256="x", server_dates=(D1,),
                period_akd_net=(CodeValue("AAA", Decimal("1")),),
                composition=self._lineage(),
            )

    def test_composition_none_is_unaffected(self):
        capture = _capture(
            start=D1, end=D1, captured_at_utc="2026-09-01T00:00:00Z",
            packet_sha256="x", server_dates=(D1,),
        )
        self.assertIsNone(capture.composition)


class AnalysisToolsRejectCompositeCaptureTests(unittest.TestCase):
    def test_build_period_report_rejects_a_composed_capture(self):
        base_input, rolling_input = _base_and_rolling()
        composed = compose_period_capture([base_input, rolling_input])
        with self.assertRaisesRegex(ValueError, "composite_capture_rejected"):
            build_period_report(composed, lags=(0,), detail_lag=0, top_n=1)

    def test_build_period_report_accepts_an_uncomposed_capture(self):
        base_input, _rolling_input = _base_and_rolling()
        report = build_period_report(base_input.capture, lags=(0,), detail_lag=0, top_n=1)
        self.assertIsInstance(report, dict)


class ReusedLoaderHashValidationTests(unittest.TestCase):
    """"hash uyuşmazlığında red": the composer never re-validates a
    capture's own packet hash -- it relies on load_period_capture (via
    parse_period_capture here) having already done so, per the module
    docstring's "reuse, don't duplicate" note.
    """

    def test_tampered_packet_is_rejected_by_the_reused_loader(self):
        document = period_fixtures._capture()
        document["packet"]["get_akd_period"]["rows_in_provider_order"][0]["net"]["size"] = 1
        with self.assertRaisesRegex(PeriodCaptureError, "packet_hash_mismatch"):
            parse_period_capture(document)


if __name__ == "__main__":
    unittest.main()
