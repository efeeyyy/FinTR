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
    PeriodCaptureError,
    canonical_json,
    load_period_capture,
    parse_period_capture,
)
from fintr.features.period import (
    broker_flow_series,
    check_custodian_diff,
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
                    {"broker_code": code, "net_size": value} for code, value in AKD[day].items()
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


if __name__ == "__main__":
    unittest.main()
