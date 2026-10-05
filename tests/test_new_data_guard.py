"""Offline tests for the no-new-data guard of scripts/run_daily_pipeline.py.

Fake price calendar, fake ``_runs`` and fake scanner output in a temp tree;
subprocess.run is mocked, so no network and no initData are involved.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fintr.collection import new_data_guard as guard
from fintr.collection.planner import RUN_RECORD_SCHEMA
from fintr.data.price_history import canonical_json as price_canonical_json
from fintr.evaluation.symbol_guard import load_active_guards

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_daily_pipeline_guard_t", ROOT / "scripts" / "run_daily_pipeline.py")
rdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdp)

SKIP_LINE = "yeni veri beklenmiyor (son işlem günü {last}); toplama atlandı"
STEP_INDEX = {"collect_daily.py": 0, "scan_observations.py": 1, "notify_observations.py": 2}
SAVED = os.path.join("data", "interim", "scanner_daily", "2026-10-07", "observations.json")

# Wed 2026-10-07 .. Tue 2026-10-13: the Thursday 10-08 bar is missing (fake holiday).
TRADING_DAYS = frozenset(date(2026, 10, d) for d in (1, 2, 5, 6, 7, 9))


def _development_symbols() -> list[str]:
    guards = load_active_guards()
    return [s for s in guards.split.development_symbols if not guards.is_excluded(s)]


def _write_runs(root: Path, last_end: str | None) -> None:
    runs = root / "data" / "interim" / "daily_capture" / "_runs"
    runs.mkdir(parents=True, exist_ok=True)
    if last_end is None:
        return
    record = {
        "schema": RUN_RECORD_SCHEMA,
        "symbols": [
            {"symbol": s, "status": "saved", "window": {"start": "2026-09-20", "end": last_end}}
            for s in _development_symbols()
        ],
    }
    (runs / "run1.json").write_text(json.dumps(record), encoding="utf-8")


def _write_scan(root: Path, unsettled: int | None) -> None:
    """Latest saved scanner output; ``unsettled=None`` writes no file at all."""
    if unsettled is None:
        return
    target = root / "data" / "interim" / "scanner_daily" / "2026-10-07"
    target.mkdir(parents=True, exist_ok=True)
    document = {"symbols": {
        s: {"composition": {"unsettled": {"day_count": 9, "in_window_day_count": unsettled}}}
        for s in _development_symbols()
    }}
    (target / "observations.json").write_text(json.dumps(document), encoding="utf-8")


def _run_pipeline(argv, *, last_end="2026-10-06", unsettled=0, calendar_end=date(2026, 10, 9)):
    """Run main() against a temp tree; returns (rc, called script names, stdout, stderr)."""
    calls: list[str] = []

    def run(cmd, **kwargs):
        name = Path(cmd[1]).name
        calls.append(name)
        err = f"Observations saved: {SAVED}\n" if name == "scan_observations.py" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=err)

    calendar = guard.PriceCalendar(TRADING_DAYS, calendar_end)
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        _write_runs(tmp, last_end)
        _write_scan(tmp, unsettled)
        with patch.object(rdp, "ROOT", tmp), patch.object(rdp.subprocess, "run", run), \
                patch.object(rdp, "load_price_calendar", lambda *a, **k: calendar), \
                redirect_stdout(out), redirect_stderr(err):
            rc = rdp.main(["--layout", "v2", *argv])
    return rc, calls, out.getvalue(), err.getvalue()


class PipelineGuardTests(unittest.TestCase):
    def assertSkipped(self, result, last):
        rc, calls, out, _ = result
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])  # no collect (initData / connection), no scan, no notify
        self.assertIn(SKIP_LINE.format(last=last), out.splitlines())

    def assertCollected(self, result):
        rc, calls, out, _ = result
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["collect_daily.py", "scan_observations.py", "notify_observations.py"])
        self.assertNotIn("yeni veri beklenmiyor", out)

    def test_saturday_skips(self):
        # R=Sun 2026-10-11 -> E=Sat 10-10
        self.assertSkipped(_run_pipeline(["--run-date", "2026-10-11"]), "2026-10-09")

    def test_sunday_skips(self):
        # R=Mon 2026-10-12 -> E=Sun 10-11
        self.assertSkipped(_run_pipeline(["--run-date", "2026-10-12"]), "2026-10-09")

    def test_holiday_skips(self):
        # R=Fri 2026-10-09 -> E=Thu 10-08: inside the price coverage, no tradeable bar
        self.assertSkipped(_run_pipeline(["--run-date", "2026-10-09"], last_end="2026-10-07"), "2026-10-07")

    def test_normal_day_collects(self):
        # R=Tue 2026-10-13 -> E=Mon 10-12, a weekday beyond the saved end and the coverage
        self.assertCollected(_run_pipeline(["--run-date", "2026-10-13"], last_end="2026-10-09"))

    def test_holiday_with_unsettled_day_still_collects(self):
        self.assertCollected(
            _run_pipeline(["--run-date", "2026-10-09"], last_end="2026-10-07", unsettled=1)
        )

    def test_force_collect_bypasses_the_guard(self):
        self.assertCollected(_run_pipeline(["--run-date", "2026-10-11", "--force-collect"]))

    def test_already_collected_day_skips_and_nothing_is_sent_again(self):
        # R=Wed 2026-10-07 -> E=Tue 10-06, saved through 10-06 and settled
        rc, calls, out, _ = _run_pipeline(["--run-date", "2026-10-07"], last_end="2026-10-06")
        self.assertEqual((rc, calls), (0, []))
        self.assertIn(SKIP_LINE.format(last="2026-10-06"), out.splitlines())

    def test_unknown_state_falls_back_to_collecting(self):
        # no scanner output (settlement unknown) / no run records: the guard never skips
        self.assertCollected(_run_pipeline(["--run-date", "2026-10-11"], unsettled=None))
        self.assertCollected(_run_pipeline(["--run-date", "2026-10-09"], last_end=None, calendar_end=None))

    def test_skip_collect_and_dry_run_are_not_guarded(self):
        _, calls, _, _ = _run_pipeline(["--run-date", "2026-10-11", "--skip-collect"])
        self.assertEqual(calls, ["scan_observations.py", "notify_observations.py"])
        _, calls, _, _ = _run_pipeline(["--run-date", "2026-10-11", "--dry-run"])
        self.assertEqual(calls, ["collect_daily.py"])


class DecideNewDataTests(unittest.TestCase):
    CAL = guard.PriceCalendar(TRADING_DAYS, date(2026, 10, 9))

    def decide(self, window_end, **kw):
        kw.setdefault("last_saved", date(2026, 10, 1))
        kw.setdefault("unsettled_pending", False)
        return guard.decide_new_data(window_end=window_end, calendar=self.CAL, **kw)

    def test_reasons(self):
        self.assertEqual(self.decide(date(2026, 10, 10)).reason, guard.REASON_WEEKEND)
        self.assertEqual(self.decide(date(2026, 10, 8)).reason, guard.REASON_HOLIDAY)
        self.assertEqual(self.decide(date(2026, 10, 12)).reason, guard.REASON_NEW_TRADING_DAY)
        self.assertEqual(self.decide(date(2026, 10, 6), last_saved=date(2026, 10, 6)).reason,
                         guard.REASON_ALREADY_COLLECTED)
        self.assertEqual(self.decide(date(2026, 10, 10), unsettled_pending=True).reason,
                         guard.REASON_UNSETTLED)
        self.assertEqual(self.decide(date(2026, 10, 10)).last_trading_day, date(2026, 10, 9))

    def test_a_weekday_beyond_price_coverage_is_never_called_a_holiday(self):
        self.assertTrue(self.decide(date(2026, 10, 12)).expect_new_data)


def _price_document(symbol: str, start: str, end: str, bars: list[dict]) -> dict:
    normalized = {
        "schema": "fintr_price_history_normalized_v1", "symbol": symbol, "currency": "TRY",
        "exchange_name": "IST", "exchange_timezone_name": "Europe/Istanbul", "gmtoffset": 10800,
        "timezone_abbreviation": "TRT", "date_source": "gmtoffset_fallback",
        "requested_start_date": start, "requested_end_date": end,
        "dropped_before_start_count": 0, "dropped_after_end_count": 0,
        "bar_count": len(bars), "bars": bars,
    }
    return {
        "capture_metadata": {
            "schema": "fintr_price_history_capture_v1", "source_id": "yahoo_finance_chart_v8",
            "request_url": f"https://example.invalid/{symbol}", "accessed_at_utc": "2026-10-01T00:00:00Z",
            "permission_provenance": {"status": "synthetic", "source_note": "synthetic test fixture"},
            "raw_response_sha256": "0" * 64,
            "normalized_sha256": hashlib.sha256(price_canonical_json(normalized).encode("utf-8")).hexdigest(),
        },
        "raw_response": {"synthetic": True},
        "normalized": normalized,
    }


class CalendarAndScanTests(unittest.TestCase):
    def test_calendar_comes_from_saved_price_files(self):
        def bar(day, volume):
            return {"date": day, "timestamp_utc": 0, "open": "5", "high": "5", "low": "5",
                    "close": "5", "adjusted_close": "5", "volume": volume}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AAA").mkdir()
            bars = [bar("2026-10-05", 100), bar("2026-10-06", 100), bar("2026-10-07", 0)]
            (root / "AAA" / "2026-10-01_2026-10-07.json").write_text(
                json.dumps(_price_document("AAA", "2026-10-01", "2026-10-07", bars)), encoding="utf-8")
            (root / "AAA" / "2026-10-01_2026-10-08.json").write_text("{not json", encoding="utf-8")
            calendar = guard.load_price_calendar(root, ["AAA", "MISSING"])
        # the zero-volume placeholder is not a trading day; the corrupt newer file is skipped
        self.assertEqual(calendar.trading_days, {date(2026, 10, 5), date(2026, 10, 6)})
        self.assertEqual(calendar.coverage_end, date(2026, 10, 7))
        self.assertEqual(guard.load_price_calendar(Path("/nonexistent"), ["AAA"]).coverage_end, None)

    def test_scanner_unsettled_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(guard.scanner_unsettled_pending(root))  # no output: unknown
            for count, expected in ((0, False), (2, True), (None, True)):
                _write_scan(root, count) if count is not None else None
                if count is None:  # window null (no rolling inputs): unknown
                    path = root / "data" / "interim" / "scanner_daily" / "2026-10-07" / "observations.json"
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    for block in doc["symbols"].values():
                        block["composition"]["unsettled"]["in_window_day_count"] = None
                    path.write_text(json.dumps(doc), encoding="utf-8")
                self.assertIs(
                    guard.scanner_unsettled_pending(root / "data" / "interim" / "scanner_daily"), expected)

    def test_last_saved_end_is_the_minimum_and_none_when_any_symbol_has_none(self):
        ends = {"A": date(2026, 10, 6), "B": date(2026, 10, 2)}
        self.assertEqual(guard.last_saved_end(["A", "B"], ends, {}), date(2026, 10, 2))
        self.assertEqual(guard.last_saved_end(["A", "B"], ends, {"B": date(2026, 10, 5)}), date(2026, 10, 5))
        self.assertIsNone(guard.last_saved_end(["A", "C"], ends, {}))

    def test_istanbul_today_uses_istanbul_calendar_date(self):
        from datetime import datetime, timezone
        self.assertEqual(guard.istanbul_today(now=datetime(2026, 10, 10, 22, 30, tzinfo=timezone.utc)),
                         date(2026, 10, 11))


if __name__ == "__main__":
    unittest.main()
