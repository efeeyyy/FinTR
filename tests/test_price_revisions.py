"""Offline tests for fintr.data.price_revisions and scripts/diff_price_history.py
(synthetic temp capture files; no network, no real data)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from fintr.data.price_history import canonical_json
from fintr.data.price_revisions import (
    PriceRevisionError,
    compare_symbol,
    exit_code_for,
    load_capture,
    run_pair,
    run_price_dir,
)

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("diff_price_history_t", ROOT / "scripts" / "diff_price_history.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

# Test tags -> valid capture file names (the module only reads <start>_<end>.json files).
NAMES = {
    "old.json": "2000-01-01_2000-01-01.json", "new.json": "2000-01-01_2000-01-02.json",
    "a.json": "2000-01-01_2000-01-01.json", "b.json": "2000-01-01_2000-01-02.json",
    "c.json": "2000-01-01_2000-01-03.json",
    "d_earlier_end.json": "2000-01-01_2000-01-04.json", "e_later_end.json": "2000-01-01_2000-01-05.json",
}


def bar(day: str, *, o="10", h="11", l="9", c="10.5", adj="10.4", v=1000) -> dict:
    return {"date": day, "timestamp_utc": 0, "open": o, "high": h, "low": l, "close": c,
            "adjusted_close": adj, "volume": v}


def trading(day: str, **kw) -> dict:
    return bar(day, **kw)


def placeholder(day: str, price="10.5") -> dict:
    return bar(day, o=price, h=price, l=price, c=price, adj=price, v=0)


def no_data(day: str) -> dict:
    return bar(day, o=None, h=None, l=None, c=None, adj=None, v=None)


def write_capture(root: Path, symbol: str, start: str, end: str, bars: list[dict],
                  accessed: str, name: str | None = None) -> Path:
    normalized = {
        "schema": "fintr_price_history_normalized_v1", "symbol": f"{symbol}.IS", "currency": "TRY",
        "exchange_name": "IST", "exchange_timezone_name": "Europe/Istanbul", "gmtoffset": 10800,
        "timezone_abbreviation": "TRT", "date_source": "gmtoffset_fallback",
        "requested_start_date": start, "requested_end_date": end,
        "dropped_before_start_count": 0, "dropped_after_end_count": 0,
        "bar_count": len(bars), "bars": bars,
    }
    document = {
        "capture_metadata": {
            "schema": "fintr_price_history_capture_v1", "source_id": "yahoo_finance_chart_v8",
            "request_url": "https://example.invalid/x", "accessed_at_utc": accessed,
            "permission_provenance": {"status": "synthetic", "source_note": "test"},
            "raw_response_sha256": "0" * 64,
            "normalized_sha256": hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest(),
        },
        "raw_response": {"synthetic": True},
        "normalized": normalized,
    }
    directory = root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / NAMES.get(name, name or f"{start}_{end}.json")
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


BASE_BARS = [trading("2026-01-05"), trading("2026-01-06"), placeholder("2026-01-07"),
             no_data("2026-01-08"), trading("2026-01-09")]


def replace(bars: list[dict], day: str, new: dict | None) -> list[dict]:
    out = [b for b in bars if b["date"] != day]
    if new is not None:
        out.append(new)
    return sorted(out, key=lambda b: b["date"])


class CompareSymbolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "price_history"
        self.addCleanup(self._tmp.cleanup)

    def compare(self, new_bars: list[dict], tolerance=Decimal("1e-6"), old_bars=None):
        write_capture(self.root, "AAA", "2026-01-05", "2026-01-09",
                      BASE_BARS if old_bars is None else old_bars, "2026-01-10T10:00:00Z", "old.json")
        write_capture(self.root, "AAA", "2026-01-05", "2026-01-09", new_bars,
                      "2026-01-11T10:00:00Z", "new.json")
        return compare_symbol(self.root, "AAA", tolerance)

    @staticmethod
    def rows(entry):
        return {r["date"]: r for r in entry["changes"]}

    def test_identical_files_have_no_changes(self):
        entry = self.compare(list(BASE_BARS))
        self.assertEqual(entry["changes"], [])
        self.assertEqual(entry["status"], "compared")

    def test_removed_marker_rows_are_info_and_removed_trading_bar_is_high(self):
        bars = replace(replace(replace(BASE_BARS, "2026-01-07", None), "2026-01-08", None),
                       "2026-01-06", None)
        rows = self.rows(self.compare(bars))
        self.assertEqual(rows["2026-01-07"]["change_type"], "removed")
        self.assertEqual((rows["2026-01-07"]["severity"], rows["2026-01-07"]["old_status"]), ("info", "placeholder"))
        self.assertEqual((rows["2026-01-08"]["severity"], rows["2026-01-08"]["old_status"]), ("info", "no_data_bar"))
        self.assertEqual((rows["2026-01-06"]["severity"], rows["2026-01-06"]["old_status"]), ("high", "trading"))
        self.assertIsNone(rows["2026-01-06"]["new_status"])

    def test_added_rows(self):
        old = replace(replace(BASE_BARS, "2026-01-07", None), "2026-01-06", None)
        entry = self.compare(list(BASE_BARS), old_bars=old)
        rows = self.rows(entry)
        self.assertEqual(rows["2026-01-07"]["change_type"], "added")
        self.assertEqual(rows["2026-01-07"]["severity"], "info")
        self.assertEqual(rows["2026-01-06"]["severity"], "high")
        self.assertEqual(entry["counts"]["added"], 2)

    def test_marker_status_changed(self):
        rows = self.rows(self.compare(replace(BASE_BARS, "2026-01-07", no_data("2026-01-07"))))
        row = rows["2026-01-07"]
        self.assertEqual((row["change_type"], row["old_status"], row["new_status"]),
                         ("marker_status_changed", "placeholder", "no_data_bar"))
        self.assertEqual(row["severity"], "medium")
        self.assertIn("close", row["null_flip_fields"])

    def test_trading_to_marker_is_high(self):
        row = self.rows(self.compare(replace(BASE_BARS, "2026-01-06", placeholder("2026-01-06"))))["2026-01-06"]
        self.assertEqual((row["change_type"], row["severity"]), ("marker_status_changed", "high"))

    def test_raw_ohlcv_changed_on_trading_bar_is_high_and_reports_max_rel(self):
        row = self.rows(self.compare(replace(BASE_BARS, "2026-01-05", trading("2026-01-05", c="10.0"))))["2026-01-05"]
        self.assertEqual((row["change_type"], row["severity"]), ("raw_ohlcv_changed", "high"))
        self.assertEqual(row["fields_changed"], ["close"])
        self.assertEqual(row["max_rel_diff_raw_ohlcv"], "4.762E-2")  # |10.5-10|/10.5

    def test_volume_change_counts_as_raw(self):
        row = self.rows(self.compare(replace(BASE_BARS, "2026-01-05", trading("2026-01-05", v=900))))["2026-01-05"]
        self.assertEqual((row["change_type"], row["fields_changed"]), ("raw_ohlcv_changed", ["volume"]))

    def test_raw_change_on_marker_bar_is_medium(self):
        row = self.rows(self.compare(replace(BASE_BARS, "2026-01-07", placeholder("2026-01-07", "10.6"))))["2026-01-07"]
        self.assertEqual((row["change_type"], row["severity"]), ("raw_ohlcv_changed", "medium"))

    def test_adjusted_close_only_is_medium_with_max_rel(self):
        entry = self.compare(replace(BASE_BARS, "2026-01-05", trading("2026-01-05", adj="10.2")))
        row = self.rows(entry)["2026-01-05"]
        self.assertEqual((row["change_type"], row["severity"]), ("adjusted_close_changed", "medium"))
        self.assertEqual(row["fields_changed"], ["adjusted_close"])
        self.assertEqual(row["max_rel_diff_adjusted_close"], "1.923E-2")  # 0.2/10.4
        self.assertEqual(entry["severity_counts"], {"high": 0, "medium": 1, "info": 0})

    def test_float_noise_below_tolerance_is_ignored_but_counted(self):
        noisy = replace(BASE_BARS, "2026-01-05", trading("2026-01-05", adj="10.4000001", c="10.5000001"))
        entry = self.compare(noisy)
        self.assertEqual(entry["changes"], [])
        self.assertEqual(entry["noise_ignored_dates"], 1)

    def test_tolerance_is_a_parameter(self):
        noisy = replace(BASE_BARS, "2026-01-05", trading("2026-01-05", adj="10.4000001"))
        self.assertEqual(len(self.compare(noisy, tolerance=Decimal("1e-9"))["changes"]), 1)
        self.assertEqual(self.compare(noisy, tolerance=Decimal("1e-3"))["changes"], [])

    def test_zero_tolerance_still_ignores_exactly_equal_values(self):
        self.assertEqual(self.compare(list(BASE_BARS), tolerance=Decimal(0))["changes"], [])

    def test_only_the_overlapping_range_is_compared(self):
        write_capture(self.root, "BBB", "2026-01-05", "2026-01-07", BASE_BARS[:3], "2026-01-10T00:00:00Z", "a.json")
        write_capture(self.root, "BBB", "2026-01-06", "2026-01-09", BASE_BARS[1:], "2026-01-11T00:00:00Z", "b.json")
        entry = compare_symbol(self.root, "BBB")
        self.assertEqual(entry["changes"], [])
        self.assertEqual(entry["overlap"], {"start": "2026-01-06", "end": "2026-01-07"})

    def test_disjoint_files_report_no_overlap(self):
        write_capture(self.root, "CCC", "2026-01-05", "2026-01-06", BASE_BARS[:2], "2026-01-10T00:00:00Z", "a.json")
        write_capture(self.root, "CCC", "2026-01-08", "2026-01-09", BASE_BARS[3:], "2026-01-11T00:00:00Z", "b.json")
        self.assertEqual(compare_symbol(self.root, "CCC")["status"], "no_overlap")

    def test_ordering_is_by_fetch_time_not_end_date(self):
        # The file with the LATER end date was fetched EARLIER (the 2026-10-09 situation).
        later_end_early_fetch = write_capture(self.root, "DDD", "2026-01-05", "2026-01-09", BASE_BARS,
                                              "2026-01-10T10:00:00Z", "e_later_end.json")
        earlier_end_late_fetch = write_capture(
            self.root, "DDD", "2026-01-05", "2026-01-08", replace(BASE_BARS[:4], "2026-01-07", None),
            "2026-01-11T10:00:00Z", "d_earlier_end.json")
        entry = compare_symbol(self.root, "DDD")
        self.assertEqual([r["change_type"] for r in entry["changes"]], ["removed"])
        self.assertEqual(entry["old_file"]["sha256"], hashlib.sha256(later_end_early_fetch.read_bytes()).hexdigest())
        self.assertEqual(entry["new_file"]["sha256"], hashlib.sha256(earlier_end_late_fetch.read_bytes()).hexdigest())

    def test_only_newest_two_files_are_compared(self):
        write_capture(self.root, "EEE", "2026-01-05", "2026-01-09", replace(BASE_BARS, "2026-01-05", trading("2026-01-05", c="99")),
                      "2026-01-09T00:00:00Z", "a.json")
        write_capture(self.root, "EEE", "2026-01-05", "2026-01-09", BASE_BARS, "2026-01-10T00:00:00Z", "b.json")
        write_capture(self.root, "EEE", "2026-01-05", "2026-01-09", BASE_BARS, "2026-01-11T00:00:00Z", "c.json")
        entry = compare_symbol(self.root, "EEE")
        self.assertEqual(entry["changes"], [])
        self.assertEqual(entry["file_count"], 3)

    def test_single_file_is_not_compared(self):
        write_capture(self.root, "FFF", "2026-01-05", "2026-01-09", BASE_BARS, "2026-01-10T00:00:00Z")
        self.assertEqual(compare_symbol(self.root, "FFF")["status"], "single_file")


class RunPriceDirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "price_history"
        for symbol in ("AAA", "BBB"):
            write_capture(self.root, symbol, "2026-01-05", "2026-01-09", BASE_BARS, "2026-01-10T00:00:00Z", "old.json")
            write_capture(self.root, symbol, "2026-01-05", "2026-01-09", BASE_BARS, "2026-01-11T00:00:00Z", "new.json")

    def test_incomplete_directory_is_excluded(self):
        write_capture(self.root / "_incomplete" / "2026-01-11", "AAA", "2026-01-05", "2026-01-09", BASE_BARS,
                      "2026-01-12T00:00:00Z")
        report = run_price_dir(self.root)
        self.assertEqual([e["symbol"] for e in report["symbols"]], ["AAA", "BBB"])

    def test_symbols_filter_and_unknown_symbol_is_an_error(self):
        report = run_price_dir(self.root, ["BBB", "ZZZ"])
        self.assertEqual([e["symbol"] for e in report["symbols"]], ["BBB"])
        self.assertEqual(report["errors"][0]["symbol"], "ZZZ")
        self.assertEqual(exit_code_for(report), 1)

    def test_corrupt_file_is_recorded_as_error_not_crash(self):
        (self.root / "AAA" / NAMES["old.json"]).write_text("{not json", encoding="utf-8")
        report = run_price_dir(self.root)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual([e["symbol"] for e in report["symbols"]], ["BBB"])

    def test_normalized_hash_mismatch_is_an_error(self):
        path = self.root / "AAA" / NAMES["new.json"]
        document = json.loads(path.read_text(encoding="utf-8"))
        document["normalized"]["bars"][0]["close"] = "99"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(PriceRevisionError):
            load_capture(path)

    def test_missing_price_dir(self):
        with self.assertRaises(PriceRevisionError):
            run_price_dir(self.root / "nope")

    def test_run_pair_symbol_mismatch(self):
        a = write_capture(self.root, "CCC", "2026-01-05", "2026-01-09", BASE_BARS, "2026-01-10T00:00:00Z")
        b = self.root / "AAA" / NAMES["new.json"]
        with self.assertRaises(PriceRevisionError):
            run_pair(a, b)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "price_history"
        self.old = write_capture(self.root, "AAA", "2026-01-05", "2026-01-09", BASE_BARS, "2026-01-10T00:00:00Z", "old.json")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def add_new(self, bars):
        return write_capture(self.root, "AAA", "2026-01-05", "2026-01-09", bars, "2026-01-11T00:00:00Z", "new.json")

    def test_exit_0_when_no_changes_and_report_schema(self):
        self.add_new(list(BASE_BARS))
        out_path = self.tmp / "out" / "r.json"
        code, stdout, _ = self.run_cli("--price-dir", str(self.root), "--out", str(out_path))
        self.assertEqual(code, 0)
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["schema_version"], "fintr_price_revisions_report_v1")
        self.assertEqual(report["totals"]["changes"], 0)
        self.assertEqual(len(report["symbols"][0]["old_file"]["sha256"]), 64)
        self.assertIn("PRICE_DIFF: changes=0", stdout)

    def test_exit_2_when_changes_found_and_rows_in_report(self):
        self.add_new(replace(BASE_BARS, "2026-01-07", None))
        out_path = self.tmp / "r.json"
        code, stdout, _ = self.run_cli("--price-dir", str(self.root), "--out", str(out_path))
        self.assertEqual(code, 2)
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["symbols"][0]["changes"][0]["change_type"], "removed")
        self.assertEqual(report["totals"]["by_severity"]["info"], 1)
        self.assertIn("PRICE_DIFF: changes=1 high=0 medium=0 info=1", stdout)
        # no price value leaks into the report
        self.assertNotIn("10.5", out_path.read_text(encoding="utf-8"))

    def test_refuses_to_overwrite_existing_report(self):
        self.add_new(list(BASE_BARS))
        out_path = self.tmp / "r.json"
        out_path.write_text("keep", encoding="utf-8")
        code, _, err = self.run_cli("--price-dir", str(self.root), "--out", str(out_path))
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(out_path.read_text(encoding="utf-8"), "keep")

    def test_exit_1_on_error(self):
        code, _, err = self.run_cli("--price-dir", str(self.tmp / "missing"), "--out", str(self.tmp / "r.json"))
        self.assertEqual(code, 1)
        self.assertIn("price_dir_not_found", err)
        self.assertFalse((self.tmp / "r.json").exists())

    def test_exit_1_when_a_symbol_errors_but_report_is_written(self):
        self.add_new(list(BASE_BARS))
        out_path = self.tmp / "r.json"
        code, _, _ = self.run_cli("--price-dir", str(self.root), "--symbols", "AAA,ZZZ", "--out", str(out_path))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out_path.read_text(encoding="utf-8"))["errors"][0]["symbol"], "ZZZ")

    def test_symbols_filter(self):
        self.add_new(replace(BASE_BARS, "2026-01-07", None))
        write_capture(self.root, "BBB", "2026-01-05", "2026-01-09", BASE_BARS, "2026-01-10T00:00:00Z", "old.json")
        write_capture(self.root, "BBB", "2026-01-05", "2026-01-09", replace(BASE_BARS, "2026-01-07", None),
                      "2026-01-11T00:00:00Z", "new.json")
        out_path = self.tmp / "r.json"
        code, _, _ = self.run_cli("--price-dir", str(self.root), "--symbols", "BBB", "--out", str(out_path))
        self.assertEqual(code, 2)
        self.assertEqual([e["symbol"] for e in json.loads(out_path.read_text(encoding="utf-8"))["symbols"]], ["BBB"])

    def test_explicit_pair_mode_ignores_fetch_order(self):
        new = self.add_new(replace(BASE_BARS, "2026-01-05", trading("2026-01-05", c="10.0")))
        out_path = self.tmp / "r.json"
        code, _, _ = self.run_cli("--old", str(self.old), "--new", str(new), "--out", str(out_path))
        self.assertEqual(code, 2)
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["parameters"]["mode"], "explicit_pair")
        self.assertEqual(report["symbols"][0]["changes"][0]["severity"], "high")

    def test_old_without_new_is_an_error(self):
        code, _, err = self.run_cli("--old", str(self.old), "--out", str(self.tmp / "r.json"))
        self.assertEqual(code, 1)
        self.assertIn("together", err)

    def test_tolerance_option(self):
        self.add_new(replace(BASE_BARS, "2026-01-05", trading("2026-01-05", adj="10.4000001")))
        code, _, _ = self.run_cli("--price-dir", str(self.root), "--out", str(self.tmp / "a.json"))
        self.assertEqual(code, 0)
        code, _, _ = self.run_cli("--price-dir", str(self.root), "--tolerance", "1e-9", "--out", str(self.tmp / "b.json"))
        self.assertEqual(code, 2)

    def test_invalid_tolerance_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli.main(["--tolerance", "abc", "--out", str(self.tmp / "r.json")])


if __name__ == "__main__":
    unittest.main()
