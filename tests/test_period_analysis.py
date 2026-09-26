"""Offline tests for saved period captures, period features, and the report CLI.

All values are synthetic; no captured market data is used.
"""

import copy
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fintr.data.period_capture import (
    BrokerageDirectoryInfo,
    DailyMarketProxyRow,
    DatedMarketProxyValues,
    PeriodCapture,
    PeriodCaptureError,
    canonical_json,
    load_period_capture,
    parse_period_capture,
)
from fintr.features.period import (
    DAILY_MARKET_PROXY_LABEL,
    FORMULA_VERSION,
    FORMULAS,
    broker_flow_series,
    check_custodian_diff,
    daily_market_proxy,
    daily_panel_coverage,
    lagged_akd_custody_match,
    reconcile_daily_akd,
)
from fintr.inference.period_report import REPORT_CONSTRAINTS, build_period_report


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_script("live_collector_smoke")
analyze = _load_script("analyze_period")

D1, D2, D3, D4 = (date(2026, 1, day) for day in (5, 6, 7, 8))
AKD = {  # daily AKD net_size
    D1: {"AAA": 100, "BBB": -40, "GGG": 1},
    D2: {"AAA": 50, "BBB": -10},
    D3: {"AAA": -30, "CCC": 5, "GGG": 2},
    D4: {"AAA": 20},
}
TOTAL = {  # daily AKD total_size, total_cost (unverified derived-proxy fields)
    D1: {"AAA": ("300", "10.0"), "BBB": ("80", "12.0"), "GGG": ("2", "5.0")},
    D2: {"AAA": ("120", "10.5"), "BBB": ("20", "12.5")},
    D3: {"AAA": ("60", "9.5"), "CCC": ("10", "8.0"), "GGG": ("4", "5.5")},
    D4: {"AAA": ("40", "11.0")},
}
CUSTODY = {  # daily custody lot_count; CCC is not listed on D2
    D1: {"AAA": "1000.0", "BBB": "500.0", "CCC": "10.0"},
    D2: {"AAA": "1000.0", "BBB": "500.0"},
    D3: {"AAA": "1100.0", "BBB": "460.0", "CCC": "10.0"},
    D4: {"AAA": "1150.0", "BBB": "455.0", "CCC": "10.0"},
}
PERIOD_AKD = {"AAA": 140, "BBB": -50, "CCC": 7, "DDD": 3, "GGG": 3}
DIFF = (  # custodian, first, last, difference
    ("AAA", "1000.0", "1150.0", "150.0"),
    ("BBB", "500.0", "455.0", "-40.0"),
    ("EEE", "7.0", "0.0", "-7.0"),
)


def _packet() -> dict:
    descending = sorted(AKD, reverse=True)
    return {
        "schema": "fintr_period_analysis_handoff_v1",
        "requested_period": {"symbol": "TEST", "start_date": "2026-01-05",
                             "end_date": "2026-01-08", "origin": "local_cli_arguments"},
        "get_akd_period": {
            "rows_in_provider_order": [
                {"brokerage": code, "net": {"size": value, "percentage": Decimal("0.1"),
                                            "cost": Decimal("1.5")}}
                for code, value in PERIOD_AKD.items()
            ],
        },
        "custody_analysis_daily": {
            "provenance": {"server_echo": {
                "dates_in_provider_order": [day.isoformat() for day in descending],
            }},
            "daily_akd_flow_in_provider_order": [
                {"date": day.isoformat(), "rows": [
                    {"broker_code": code, "net_size": value,
                     "total_size": Decimal(TOTAL[day][code][0]),
                     "total_cost": Decimal(TOTAL[day][code][1])}
                    for code, value in AKD[day].items()
                ]}
                for day in descending
            ],
            "daily_custody_balance_in_provider_order": [
                {"date": day.isoformat(), "rows": [
                    {"broker_code": code, "lot_count": Decimal(value)}
                    for code, value in CUSTODY[day].items()
                ]}
                for day in descending
            ],
            "metadata_in_provider_order": [
                {"date": day.isoformat(),
                 "null_fields": ["retail_pct"] if day == D4 else [],
                 "retail_pct": None if day == D4 else Decimal("40.5"),
                 "institutional_pct": None if day == D4 else Decimal("59.5")}
                for day in descending
            ],
        },
        "get_custodian_diff_period": {
            "provenance": {"server_echo": {"start_date": "2026-01-05",
                                           "end_date": "2026-01-08"}},
            "rows_in_provider_order": [
                {"custodian": code, "first_value": Decimal(first),
                 "last_value": Decimal(last), "difference": Decimal(diff)}
                for code, first, last, diff in DIFF
            ],
        },
    }


def _capture(packet: dict | None = None) -> dict:
    packet = _packet() if packet is None else packet
    return {
        "capture_metadata": {
            "schema": "fintr_period_analysis_capture_v1",
            "captured_at_utc": "2026-01-08T18:00:00Z",
            "packet_schema": packet.get("schema", "fintr_period_analysis_handoff_v1"),
            "packet_sha256_canonical_json": hashlib.sha256(
                canonical_json(packet).encode("utf-8")
            ).hexdigest(),
        },
        "packet": packet,
    }


# code, title, short_title; EEE is deliberately duplicated with conflicting
# names so its exact-code resolution is ambiguous, never a guessed name.
DIRECTORY_ROWS = (
    ("AAA", "AAA Synthetic Title", "AAA Short"),
    ("EEE", "First EEE Synthetic Name", "First EEE Short"),
    ("EEE", "Second EEE Synthetic Name", "Second EEE Short"),
)


def _v2_packet(brokerage_rows: tuple = DIRECTORY_ROWS) -> dict:
    packet = _packet()
    packet["schema"] = "fintr_period_analysis_handoff_v2"
    packet["brokerage_directory"] = {
        "grain": (
            "undated_provider_snapshot_rows; exact_code_match_only; "
            "not_ultimate_investor"
        ),
        "source_event": "brokerages_data",
        "row_count": len(brokerage_rows),
        "rows": [
            {"code": code, "title": title, "short_title": short_title}
            for code, title, short_title in brokerage_rows
        ],
        "flags": {
            "undated": True,
            "exact_code_match_only": True,
            "identifies_ultimate_investor": False,
        },
    }
    return packet


class PeriodCaptureLoaderTests(unittest.TestCase):
    def test_canonical_json_matches_capture_writer(self):
        packet = _packet()
        self.assertEqual(canonical_json(packet), smoke._canonical_period_json(packet))

    def test_saved_file_round_trips_in_ascending_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            path.write_text(smoke._compact_json(_capture()), encoding="utf-8")
            capture = load_period_capture(path)
        self.assertEqual(capture.server_dates, (D1, D2, D3, D4))
        self.assertEqual([day.date for day in capture.daily_akd_net], [D1, D2, D3, D4])
        self.assertEqual(capture.custodian_diff_end, D4)
        self.assertIsNone(capture.metadata[-1].retail_pct)
        self.assertEqual([day.date for day in capture.daily_akd_market], [D1, D2, D3, D4])
        d1_market = capture.daily_akd_market[0].by_code()
        self.assertEqual(d1_market["AAA"].total_size, Decimal("300"))
        self.assertEqual(d1_market["AAA"].total_cost, Decimal("10.0"))

    def test_daily_akd_row_missing_total_size_is_rejected(self):
        packet = _packet()
        row = packet["custody_analysis_daily"]["daily_akd_flow_in_provider_order"][0]["rows"][0]
        del row["total_size"]
        with self.assertRaisesRegex(PeriodCaptureError, "invalid_capture_field"):
            parse_period_capture(_capture(packet))

    def test_tampered_packet_is_rejected(self):
        document = _capture()
        document["packet"]["get_akd_period"]["rows_in_provider_order"][0]["net"]["size"] = 1
        with self.assertRaisesRegex(PeriodCaptureError, "packet_hash_mismatch"):
            parse_period_capture(document)

    def test_duplicate_code_in_a_day_is_rejected(self):
        packet = _packet()
        rows = packet["custody_analysis_daily"]["daily_akd_flow_in_provider_order"][0]["rows"]
        rows.append(copy.deepcopy(rows[0]))
        with self.assertRaisesRegex(PeriodCaptureError, "duplicate_code"):
            parse_period_capture(_capture(packet))

    def test_wrong_type_and_schema_are_rejected(self):
        packet = _packet()
        packet["get_custodian_diff_period"]["rows_in_provider_order"][0]["difference"] = "150"
        with self.assertRaisesRegex(PeriodCaptureError, "invalid_capture_field"):
            parse_period_capture(_capture(packet))
        document = _capture()
        document["capture_metadata"]["schema"] = "other"
        with self.assertRaisesRegex(PeriodCaptureError, "unsupported_schema"):
            parse_period_capture(document)

    def test_non_finite_number_in_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            path.write_text('{"capture_metadata": {"x": NaN}}', encoding="utf-8")
            with self.assertRaises(PeriodCaptureError):
                load_period_capture(path)


class PeriodFeatureTests(unittest.TestCase):
    def setUp(self):
        self.capture = parse_period_capture(_capture())

    def test_akd_reconciliation_statuses(self):
        status = {row.code: row for row in reconcile_daily_akd(self.capture)}
        self.assertEqual(status["AAA"].status, "match")
        self.assertEqual(status["AAA"].days_listed, 4)
        self.assertEqual(status["BBB"].status, "match")
        self.assertEqual(status["CCC"].status, "mismatch")
        self.assertEqual(status["CCC"].daily_sum, Decimal(5))
        self.assertEqual(status["DDD"].status, "missing_in_daily")

    def test_custodian_diff_checks_keep_absent_distinct_from_zero(self):
        check = check_custodian_diff(self.capture)
        self.assertTrue(check.range_aligned)
        rows = {row.code: row for row in check.rows}
        self.assertTrue(rows["AAA"].internally_consistent)
        self.assertEqual((rows["AAA"].first_status, rows["AAA"].last_status), ("match", "match"))
        self.assertFalse(rows["BBB"].internally_consistent)
        self.assertEqual(rows["EEE"].last_status, "absent_in_daily_panel")
        self.assertIsNone(rows["EEE"].daily_last_value)

    def test_flow_runs_split_on_sign_change_and_missing_day(self):
        series = {item.code: item for item in broker_flow_series(self.capture)}
        runs = [(run.start, run.end, run.direction, run.net_sum) for run in series["AAA"].runs]
        self.assertEqual(runs, [
            (D1, D2, "positive", Decimal(150)),
            (D3, D3, "negative", Decimal(-30)),
            (D4, D4, "positive", Decimal(20)),
        ])
        self.assertEqual(len(series["GGG"].runs), 2)  # D2 not listed breaks the run
        self.assertEqual(series["GGG"].points[1], (D2, None))

    def test_lagged_match_counts_and_pending_dates(self):
        analysis = lagged_akd_custody_match(self.capture, lags=(0, 2), detail_lag=2)
        lag2 = analysis.summaries[1]
        self.assertEqual((lag2.lag, lag2.comparable, lag2.exact_matches, lag2.sign_matches),
                         (2, 4, 3, 4))
        self.assertEqual(len(analysis.unmatched), 1)
        miss = analysis.unmatched[0]
        self.assertEqual((miss.code, miss.akd_date, miss.custody_date), ("BBB", D2, D4))
        self.assertEqual((miss.akd_net, miss.custody_change), (Decimal(-10), Decimal(-5)))
        self.assertEqual(analysis.pending_akd_dates, (D3, D4))
        with self.assertRaises(ValueError):
            lagged_akd_custody_match(self.capture, lags=(-1,), detail_lag=2)

    def test_panel_coverage_lists_unlisted_dates(self):
        coverage = {item.code: item for item in daily_panel_coverage(self.capture)}
        self.assertEqual(coverage["CCC"].custody_missing_dates, (D2,))
        self.assertEqual(coverage["AAA"].akd_missing_dates, ())


def _build_capture(
    server_dates: tuple,
    net_days: dict,
    market_days: dict,
) -> PeriodCapture:
    """Minimal direct-construction PeriodCapture for testing daily_market_proxy
    in isolation; fields unrelated to the proxy are filled with placeholders."""
    from fintr.data.period_capture import DatedCodeValues

    daily_akd_net = tuple(
        DatedCodeValues(day, tuple(net_days[day])) for day in sorted(net_days)
    )
    daily_akd_market = tuple(
        DatedMarketProxyValues(day, tuple(market_days[day])) for day in sorted(market_days)
    )
    return PeriodCapture(
        symbol="TEST", start=server_dates[0], end=server_dates[-1],
        captured_at_utc="2026-01-01T00:00:00Z", packet_sha256="deadbeef",
        server_dates=server_dates, period_akd_net=(),
        daily_akd_net=daily_akd_net, daily_akd_market=daily_akd_market,
        daily_custody_lots=(), custodian_diff_start=server_dates[0],
        custodian_diff_end=server_dates[-1], custodian_diff=(), metadata=(),
        brokerage_directory=BrokerageDirectoryInfo(present=False, row_count=0, index=None),
    )


class DailyMarketProxyTests(unittest.TestCase):
    """Synthetic-only: checks passing, failing, and None handling for a
    missing server date. Real market data is never used in tests."""

    def setUp(self):
        from fintr.data.period_capture import CodeValue

        # D1: both checks pass, VWAP within [5, 7].
        d1_net = [CodeValue("AAA", Decimal(10)), CodeValue("BBB", Decimal(-10))]
        d1_market = [
            DailyMarketProxyRow("AAA", Decimal(20), Decimal(5)),
            DailyMarketProxyRow("BBB", Decimal(20), Decimal(7)),
        ]
        # D2: net_sum_zero fails, total_size_even fails (odd sum=39),
        # net_within_total fails (AAA net 25 > total_size 20); VWAP stays
        # within [5, 9] since weights are non-negative.
        d2_net = [CodeValue("AAA", Decimal(25)), CodeValue("BBB", Decimal(-5))]
        d2_market = [
            DailyMarketProxyRow("AAA", Decimal(20), Decimal(5)),
            DailyMarketProxyRow("BBB", Decimal(19), Decimal(9)),
        ]
        # D4 (D3 is deliberately absent -> missing day): net_sum_zero passes,
        # total_size_even passes (sum=40), net_within_total fails (0 <= -10 is
        # False), vwap_within_range fails (weighted average pulled outside the
        # priced range by the negative-size row).
        d4_net = [CodeValue("AAA", Decimal(0)), CodeValue("BBB", Decimal(0))]
        d4_market = [
            DailyMarketProxyRow("AAA", Decimal(-10), Decimal(100)),
            DailyMarketProxyRow("BBB", Decimal(50), Decimal(2)),
        ]
        self.d1, self.d2, self.d3, self.d4 = (date(2026, 2, day) for day in (1, 2, 3, 4))
        self.capture = _build_capture(
            (self.d1, self.d2, self.d3, self.d4),
            {self.d1: d1_net, self.d2: d2_net, self.d4: d4_net},
            {self.d1: d1_market, self.d2: d2_market, self.d4: d4_market},
        )
        self.proxy = daily_market_proxy(self.capture)

    def test_label_is_unverified_derived_proxy(self):
        self.assertEqual(self.proxy.label, "derived_proxy_not_official_ohlc")
        self.assertEqual(self.proxy.label, DAILY_MARKET_PROXY_LABEL)

    def test_missing_server_date_yields_none_never_zero(self):
        days = {day.date: day for day in self.proxy.days}
        missing = days[self.d3]
        self.assertFalse(missing.has_rows)
        self.assertIsNone(missing.sum_total_size)
        self.assertIsNone(missing.implied_traded_lots)
        self.assertIsNone(missing.vwap_proxy)
        self.assertEqual(
            (missing.checks.net_sum_zero, missing.checks.total_size_even,
             missing.checks.net_within_total, missing.checks.vwap_within_range),
            (None, None, None, None),
        )

    def test_all_checks_pass_on_a_clean_day(self):
        days = {day.date: day for day in self.proxy.days}
        clean = days[self.d1]
        self.assertTrue(clean.has_rows)
        self.assertEqual(clean.sum_total_size, Decimal(40))
        self.assertEqual(clean.implied_traded_lots, Decimal(20))
        self.assertEqual(clean.vwap_proxy, Decimal(6))
        self.assertEqual(
            (clean.checks.net_sum_zero, clean.checks.total_size_even,
             clean.checks.net_within_total, clean.checks.vwap_within_range),
            (True, True, True, True),
        )

    def test_checks_fail_as_expected_on_dirty_days(self):
        days = {day.date: day for day in self.proxy.days}
        d2 = days[self.d2]
        self.assertEqual(
            (d2.checks.net_sum_zero, d2.checks.total_size_even,
             d2.checks.net_within_total, d2.checks.vwap_within_range),
            (False, False, False, True),
        )
        d4 = days[self.d4]
        self.assertEqual(
            (d4.checks.net_sum_zero, d4.checks.total_size_even,
             d4.checks.net_within_total, d4.checks.vwap_within_range),
            (True, True, False, False),
        )

    def test_period_summaries_exclude_missing_days_from_denominator(self):
        self.assertEqual(
            (self.proxy.net_sum_zero.evaluated_days, self.proxy.net_sum_zero.passed_days,
             self.proxy.net_sum_zero.failing_days, self.proxy.net_sum_zero.all_evaluated_days_passed),
            (3, 2, 1, False),
        )
        self.assertEqual(
            (self.proxy.total_size_even.evaluated_days, self.proxy.total_size_even.passed_days,
             self.proxy.total_size_even.failing_days),
            (3, 2, 1),
        )
        self.assertEqual(
            (self.proxy.net_within_total.evaluated_days, self.proxy.net_within_total.passed_days,
             self.proxy.net_within_total.failing_days),
            (3, 1, 2),
        )
        self.assertEqual(
            (self.proxy.vwap_within_range.evaluated_days, self.proxy.vwap_within_range.passed_days,
             self.proxy.vwap_within_range.failing_days),
            (3, 2, 1),
        )

    def test_formula_version_is_v2_and_documents_proxy_formulas(self):
        self.assertEqual(FORMULA_VERSION, "period_features_v2")
        self.assertIn("daily_market_proxy_assumptions", FORMULAS)
        self.assertIn("daily_market_proxy_values", FORMULAS)
        self.assertIn("daily_market_proxy_checks", FORMULAS)


class PeriodReportTests(unittest.TestCase):
    def setUp(self):
        self.capture = parse_period_capture(_capture())

    def test_report_is_json_ready_and_carries_formulas_and_constraints(self):
        report = build_period_report(self.capture, lags=(0, 1, 2), detail_lag=2, top_n=2)
        json.dumps(report, allow_nan=False)
        self.assertEqual(report["akd_reconciliation"]["status_counts"],
                         {"match": 3, "mismatch": 1, "missing_in_daily": 1})
        self.assertEqual([row["code"] for row in report["period_akd_net"]["top_positive"]],
                         ["AAA", "CCC"])
        self.assertEqual(report["period_akd_net"]["top_negative"][0]["code"], "BBB")
        self.assertEqual(report["custodian_diff_check"]["internally_inconsistent_codes"], ["BBB"])
        self.assertIn("lagged_match", report["formulas"])
        self.assertEqual(report["constraints"], list(REPORT_CONSTRAINTS))
        self.assertIs(report["provider_metadata"]["method_verified"], False)
        with self.assertRaises(ValueError):
            build_period_report(self.capture, lags=(2,), detail_lag=2, top_n=0)

    def test_daily_market_proxy_section_is_labelled_and_not_a_price_series(self):
        report = build_period_report(self.capture, lags=(0,), detail_lag=0, top_n=5)
        proxy = report["daily_market_proxy"]
        self.assertEqual(proxy["label"], "derived_proxy_not_official_ohlc")
        self.assertIn("not an official price/volume series", proxy["constraint"])
        self.assertEqual(len(proxy["days"]), 4)
        summary = proxy["check_summary"]
        self.assertEqual(
            summary["net_sum_zero"],
            {"evaluated_days": 4, "passed_days": 0, "failing_days": 4,
             "all_evaluated_days_passed": False},
        )
        for name in ("total_size_even", "net_within_total", "vwap_within_range"):
            self.assertEqual(
                summary[name],
                {"evaluated_days": 4, "passed_days": 4, "failing_days": 0,
                 "all_evaluated_days_passed": True},
            )
        self.assertTrue(any(
            "official" in constraint and "not" in constraint.lower()
            for constraint in report["constraints"]
        ))


class BrokerageDirectoryLabelTests(unittest.TestCase):
    """v1 captures carry no directory; v2 captures resolve by exact code only."""

    def test_v1_capture_has_no_directory_and_labels_stay_directory_not_in_capture(self):
        capture = parse_period_capture(_capture())
        self.assertFalse(capture.brokerage_directory.present)
        self.assertIsNone(capture.brokerage_directory.index)

        report = build_period_report(capture, lags=(0,), detail_lag=0, top_n=5)
        label = report["period_akd_net"]["top_positive"][0]["label"]
        self.assertEqual(
            label, {"status": "directory_not_in_capture", "title": None, "short_title": None},
        )

    def test_v2_capture_loads_directory_and_resolves_by_exact_code(self):
        capture = parse_period_capture(_capture(_v2_packet()))
        self.assertTrue(capture.brokerage_directory.present)
        self.assertEqual(capture.brokerage_directory.row_count, 3)

        report = build_period_report(capture, lags=(0,), detail_lag=0, top_n=5)
        net_labels = {
            row["code"]: row["label"]
            for row in (
                report["period_akd_net"]["top_positive"]
                + report["period_akd_net"]["top_negative"]
            )
        }
        self.assertEqual(net_labels["AAA"]["status"], "resolved")
        self.assertEqual(net_labels["AAA"]["title"], "AAA Synthetic Title")
        self.assertEqual(net_labels["AAA"]["short_title"], "AAA Short")
        self.assertEqual(
            net_labels["BBB"],
            {"status": "unresolved", "title": None, "short_title": None},
        )

        diff_labels = {
            row["code"]: row["label"]
            for row in (
                report["custodian_diff"]["largest_increases"]
                + report["custodian_diff"]["largest_decreases"]
            )
        }
        self.assertEqual(diff_labels["EEE"]["status"], "ambiguous")
        self.assertIsNone(diff_labels["EEE"]["title"])
        self.assertIsNone(diff_labels["EEE"]["short_title"])

        exception_labels = {
            row["code"]: row["label"]
            for row in report["custodian_diff_check"]["exceptions"]
        }
        self.assertEqual(exception_labels["EEE"]["status"], "ambiguous")

        flow_labels = {row["code"]: row["label"] for row in report["broker_flow_runs"]["codes"]}
        self.assertEqual(flow_labels["AAA"]["status"], "resolved")
        self.assertEqual(flow_labels["BBB"]["status"], "unresolved")

    def test_v2_directory_row_count_mismatch_is_rejected(self):
        packet = _v2_packet()
        packet["brokerage_directory"]["row_count"] = 99
        with self.assertRaisesRegex(
            PeriodCaptureError, "brokerage_directory_row_count_mismatch",
        ):
            parse_period_capture(_capture(packet))

    def test_unsupported_packet_schema_is_rejected(self):
        packet = _packet()
        packet["schema"] = "fintr_period_analysis_handoff_v3"
        document = _capture(packet)
        with self.assertRaisesRegex(PeriodCaptureError, "unsupported_schema"):
            parse_period_capture(document)

    def test_text_rendering_shows_short_title_only_when_resolved(self):
        capture = parse_period_capture(_capture(_v2_packet()))
        report = build_period_report(capture, lags=(0, 1, 2), detail_lag=2, top_n=5)
        text = analyze.render_text(report)
        self.assertIn("AAA (AAA Short)", text)
        self.assertNotIn("BBB (", text)
        self.assertNotIn("EEE (", text)


class AnalyzePeriodScriptTests(unittest.TestCase):
    def test_text_output_and_save_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-01-05_2026-01-08.json"
            path.write_text(smoke._compact_json(_capture()), encoding="utf-8")
            stdout, stderr = io.BytesIO(), io.StringIO()
            with redirect_stderr(stderr), patch.object(
                analyze.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
            ) as wrapped:
                self.assertEqual(analyze.main([str(path), "--save"]), 0)
                wrapped.flush()
            text = stdout.getvalue().decode("utf-8")
            self.assertIn("gecikme 2: 3/4 birebir", text)
            self.assertIn("EEE", text)
            self.assertIn("Günlük piyasa proxy kontrolleri", text)
            self.assertIn("net_sum_zero=0/4 geçti", text)
            self.assertIn("total_size_even=4/4 geçti", text)
            saved = path.with_name("2026-01-05_2026-01-08.report.json")
            report = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(report["run"]["parameters"]["detail_lag"], 2)
            self.assertEqual(len(report["run"]["input_file_sha256"]), 64)
            with redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                self.assertEqual(analyze.main([str(path), "--save"]), 1)
            self.assertIn("no overwrite", errors.getvalue())

    def test_invalid_capture_fails_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{}", encoding="utf-8")
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(analyze.main([str(path)]), 1)
            self.assertIn("Analysis failed", errors.getvalue())


def _write_capture(root: Path, symbol: str, start: str, end: str, document: dict) -> Path:
    directory = root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{start}_{end}.json"
    path.write_text(smoke._compact_json(document), encoding="utf-8")
    return path


class AnalyzePeriodBatchTests(unittest.TestCase):
    """--all discovers, validates, and indexes every capture under a root."""

    def _run(self, argv: list[str]) -> tuple[int, dict]:
        stdout, stderr = io.BytesIO(), io.StringIO()
        with redirect_stderr(stderr), patch.object(
            analyze.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
        ) as wrapped:
            code = analyze.main(argv)
            wrapped.flush()
        return code, json.loads(stdout.getvalue().decode("utf-8"))

    def test_discovery_ignores_non_matching_and_stray_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = _write_capture(root, "AAA", "2026-01-05", "2026-01-08", _capture())
            (root / "AAA" / "2026-01-05_2026-01-08.report.json").write_text("{}", encoding="utf-8")
            (root / "AAA" / "notes.txt").write_text("x", encoding="utf-8")
            (root / "AAA" / "badname.json").write_text("{}", encoding="utf-8")
            (root / "AAA" / ".tmpfile.json").write_text("{}", encoding="utf-8")
            (root / "stray.json").write_text("{}", encoding="utf-8")
            self.assertEqual(analyze.discover_captures(root), [good])

    def test_missing_root_returns_no_captures(self):
        self.assertEqual(analyze.discover_captures(Path("no-such-period-analysis-root")), [])

    def test_mixed_valid_and_invalid_captures_all_processed_and_exit_1(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_capture(root, "AAA", "2026-01-05", "2026-01-08", _capture())
            _write_capture(root, "BBB", "2026-01-05", "2026-01-08", _capture(_v2_packet()))
            broken_dir = root / "CCC"
            broken_dir.mkdir()
            (broken_dir / "2026-01-05_2026-01-08.json").write_text("{}", encoding="utf-8")

            code, index = self._run(["--all", str(root), "--format", "json"])
            self.assertEqual(code, 1)
            self.assertEqual(index["totals"], {
                "captures": 3, "ok": 2, "failed": 1, "reports_saved": 0,
                "reports_skipped": 0, "reports_not_saved": 2, "reports_failed": 0,
            })
            by_symbol = {e["symbol"]: e for e in index["captures"]}
            self.assertEqual(by_symbol["CCC"]["status"], "failed")
            self.assertIsNotNone(by_symbol["CCC"]["error"])
            self.assertEqual(by_symbol["AAA"]["status"], "ok")
            self.assertEqual(by_symbol["AAA"]["schema_version"], "v1")
            self.assertEqual(by_symbol["BBB"]["schema_version"], "v2")
            self.assertTrue(by_symbol["BBB"]["brokerage_directory_present"])
            self.assertEqual(by_symbol["AAA"]["custodian_diff_exception_count"], 1)
            lag2 = next(r for r in by_symbol["AAA"]["lag_summaries"] if r["lag"] == 2)
            self.assertEqual((lag2["comparable"], lag2["exact_matches"]), (4, 3))
            # Fixture AKD rows don't net to zero per day, so this flag is False
            # even though the fixture validates and loads cleanly.
            self.assertFalse(by_symbol["AAA"]["proxy_checks_passed"])
            self.assertIsNone(by_symbol["CCC"]["proxy_checks_passed"])

    def test_save_writes_reports_and_index_then_skips_existing_on_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_capture(root, "AAA", "2026-01-05", "2026-01-08", _capture())

            code, index = self._run(["--all", str(root), "--save", "--format", "json"])
            self.assertEqual(code, 0)
            self.assertEqual(index["totals"]["reports_saved"], 1)
            report_path = root / "AAA" / "2026-01-05_2026-01-08.report.json"
            self.assertTrue(report_path.exists())
            index_path = root / "index.json"
            self.assertTrue(index_path.exists())
            self.assertEqual(json.loads(index_path.read_text(encoding="utf-8")), index)

            code, index = self._run(["--all", str(root), "--save", "--format", "json"])
            self.assertEqual(code, 0)
            self.assertEqual(index["totals"]["reports_saved"], 0)
            self.assertEqual(index["totals"]["reports_skipped"], 1)
            self.assertEqual(index["captures"][0]["report"], "skipped")

    def test_capture_and_all_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_capture(root, "AAA", "2026-01-05", "2026-01-08", _capture())
            with redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                code = analyze.main([str(path), "--all", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("not both", errors.getvalue())

    def test_missing_capture_and_missing_all_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                analyze.main([])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_text_index_is_short_turkish_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_capture(root, "AAA", "2026-01-05", "2026-01-08", _capture())
            stdout, stderr = io.BytesIO(), io.StringIO()
            with redirect_stderr(stderr), patch.object(
                analyze.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
            ) as wrapped:
                code = analyze.main(["--all", str(root)])
                wrapped.flush()
            self.assertEqual(code, 0)
            text = stdout.getvalue().decode("utf-8")
            self.assertIn("Toplu dönem analizi", text)
            self.assertIn("AAA 2026-01-05..2026-01-08", text)
            self.assertIn("rapor=kaydedilmedi (--save yok)", text)


if __name__ == "__main__":
    unittest.main()
