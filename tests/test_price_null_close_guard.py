"""Offline tests for the null-close last-bar guard (Yahoo has not published the end
day's close): the fetch tools do not save such a file, the offline calendar and the
daily pipeline notice the gap, and ``repair_incomplete_prices.py`` sets old files
aside. Synthetic data only (tests/fixtures/market/price_history_chart.json)."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fintr.collection import new_data_guard as guard
from fintr.data.price_history import last_bar_incomplete

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "market" / "price_history_chart.json"
START, END = date(2026, 1, 5), date(2026, 1, 9)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_nullclose", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch = _load_script("fetch_price_history_batch")
single = _load_script("fetch_price_history")
repair = _load_script("repair_incomplete_prices")


def _chart(*, null_last_close: bool = False, null_last_adjclose: bool = False) -> bytes:
    document = json.loads(FIXTURE.read_bytes())
    result = document["chart"]["result"][0]
    if null_last_close:
        result["indicators"]["quote"][0]["close"][-1] = None
    if null_last_adjclose:
        result["indicators"]["adjclose"][0]["adjclose"][-1] = None
    return json.dumps(document).encode("utf-8")


def _normalized(raw: bytes, end: date = END) -> dict:
    capture = single.build_capture(
        symbol="AKFIS", start=START, end=end, request_url="https://example.invalid/x",
        raw_bytes=raw, accessed_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )
    return capture["normalized"]


def _write_capture(price_dir: Path, symbol: str, raw: bytes, end: date = END) -> Path:
    capture = single.build_capture(
        symbol=symbol, start=START, end=end, request_url="https://example.invalid/x",
        raw_bytes=raw, accessed_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )
    path = price_dir / symbol / f"{START.isoformat()}_{end.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(capture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class LastBarIncompleteTests(unittest.TestCase):
    def test_null_close_on_the_end_day_is_incomplete(self):
        self.assertTrue(last_bar_incomplete(_normalized(_chart(null_last_close=True))))

    def test_complete_last_bar_is_not_incomplete(self):
        self.assertFalse(last_bar_incomplete(_normalized(_chart())))

    def test_null_adjusted_close_alone_is_incomplete_when_earlier_bars_have_one(self):
        self.assertTrue(last_bar_incomplete(_normalized(_chart(null_last_adjclose=True))))

    def test_response_without_any_adjclose_is_not_incomplete(self):
        document = json.loads(_chart())
        del document["chart"]["result"][0]["indicators"]["adjclose"]
        self.assertFalse(last_bar_incomplete(_normalized(json.dumps(document).encode())))

    def test_end_day_without_a_bar_is_a_holiday_not_incomplete(self):
        # Requested end 2026-01-10 (no bar that day); the last bar, 01-09, is complete.
        self.assertFalse(last_bar_incomplete(_normalized(_chart(), end=date(2026, 1, 10))))
        # Even a null close on an older last bar is history, not a pending end day.
        self.assertFalse(last_bar_incomplete(
            _normalized(_chart(null_last_close=True), end=date(2026, 1, 10))))

    def test_completely_empty_last_bar_is_a_holiday_placeholder_not_incomplete(self):
        document = json.loads(_chart())
        result = document["chart"]["result"][0]
        for key in ("open", "high", "low", "close", "volume"):
            result["indicators"]["quote"][0][key][-1] = None
        result["indicators"]["adjclose"][0]["adjclose"][-1] = None
        self.assertFalse(last_bar_incomplete(_normalized(json.dumps(document).encode())))

    def test_open_high_low_volume_filled_but_close_null_is_incomplete(self):
        document = json.loads(_chart())
        result = document["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        for key, value in (("open", 10.5), ("high", 10.6), ("low", 10.4), ("volume", 1000)):
            quote[key][-1] = value
        quote["close"][-1] = None
        result["indicators"]["adjclose"][0]["adjclose"][-1] = None
        self.assertTrue(last_bar_incomplete(_normalized(json.dumps(document).encode())))

    def test_empty_or_odd_payload_is_not_incomplete(self):
        self.assertFalse(last_bar_incomplete({}))
        self.assertFalse(last_bar_incomplete({"bars": [], "requested_end_date": "2026-01-09"}))


class BatchFetchTests(unittest.TestCase):
    def _run(self, project_root: Path, raw: bytes, symbols=("AKFIS",)):
        with patch.object(batch.fetch, "PROJECT_ROOT", project_root), \
             patch.object(batch.fetch, "_fetch_raw", return_value=raw), \
             patch.object(batch, "time"):
            return batch.run_symbols(
                list(symbols), START, END, exclusions=_NoExclusions(), save=True,
                pacing_seconds=1.5, timeout=5.0,
            )

    def test_null_close_last_bar_is_not_saved_and_a_later_run_saves_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            target = project_root / "data/interim/price_history/AKFIS" / f"{START}_{END}.json"
            [first] = self._run(project_root, _chart(null_last_close=True))
            self.assertEqual(first["status"], "skipped_incomplete_last_bar")
            self.assertEqual(first["reason"], "last_bar_close_missing")
            self.assertFalse(target.exists())
            [second] = self._run(project_root, _chart())
            self.assertEqual(second["status"], "saved")
            self.assertTrue(target.is_file())

    def test_complete_file_still_saves_and_holiday_end_day_saves(self):
        with tempfile.TemporaryDirectory() as temporary:
            [result] = self._run(Path(temporary), _chart())
            self.assertEqual(result["status"], "saved")
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            with patch.object(batch.fetch, "PROJECT_ROOT", project_root), \
                 patch.object(batch.fetch, "_fetch_raw", return_value=_chart()), \
                 patch.object(batch, "time"):
                [holiday] = batch.run_symbols(
                    ["AKFIS"], START, date(2026, 1, 10), exclusions=_NoExclusions(), save=True,
                    pacing_seconds=1.5, timeout=5.0,
                )
            self.assertEqual(holiday["status"], "saved")


class _NoExclusions:
    def is_excluded(self, _symbol: str) -> bool:
        return False


class SingleFetchTests(unittest.TestCase):
    def test_save_with_null_close_last_bar_saves_nothing_and_exits_1(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            out = io.StringIO()
            with patch.object(single, "PROJECT_ROOT", project_root), \
                 patch.object(single, "_today", return_value=date(2026, 1, 10)), \
                 patch.object(single, "_fetch_raw", return_value=_chart(null_last_close=True)), \
                 redirect_stdout(out):
                code = single.main(["--symbol", "AKFIS", "--start-date", START.isoformat(),
                                    "--end-date", END.isoformat(), "--save"])
            self.assertEqual(code, 1)
            self.assertIn("skipped_incomplete_last_bar", out.getvalue())
            self.assertFalse((project_root / "data").exists())


class CalendarTests(unittest.TestCase):
    def test_saved_null_close_file_is_listed_as_incomplete_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            price_dir = Path(temporary)
            _write_capture(price_dir, "AKFIS", _chart(null_last_close=True))
            calendar = guard.load_price_calendar(price_dir, ["AKFIS"])
        self.assertEqual(calendar.incomplete_ends, frozenset({END}))
        self.assertNotIn(END, calendar.trading_days)

    def test_complete_file_has_no_incomplete_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            price_dir = Path(temporary)
            _write_capture(price_dir, "AKFIS", _chart())
            calendar = guard.load_price_calendar(price_dir, ["AKFIS"])
        self.assertEqual(calendar.incomplete_ends, frozenset())

    def test_missing_close_day_rules(self):
        trading = frozenset({date(2026, 10, 1), date(2026, 10, 2)})
        e = date(2026, 10, 5)  # Monday
        asof = date(2026, 10, 2)

        def check(calendar, window_end=e, as_of=asof):
            return guard.missing_close_day(window_end=window_end, calendar=calendar, as_of=as_of)

        self.assertTrue(check(guard.PriceCalendar(trading, date(2026, 10, 2))))        # no file for E yet
        self.assertFalse(check(guard.PriceCalendar(frozenset(), None)))                 # no price history at all
        self.assertTrue(check(guard.PriceCalendar(trading, e, frozenset({e}))))         # legacy null file
        self.assertFalse(check(guard.PriceCalendar(trading, e)))                        # holiday: covered, no bar
        self.assertFalse(check(guard.PriceCalendar(trading | {e}, e)))                  # E has a close
        self.assertFalse(check(guard.PriceCalendar(trading, date(2026, 10, 3)),
                               window_end=date(2026, 10, 3)))                           # Saturday
        self.assertFalse(check(guard.PriceCalendar(trading, None), as_of=e))            # scan reached E


class RepairTests(unittest.TestCase):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = repair.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _tree(self, price_dir: Path):
        bad = _write_capture(price_dir, "AKFIS", _chart(null_last_close=True))
        good = _write_capture(price_dir, "ZZZZZ", _chart())
        (price_dir / "BROKEN").mkdir()
        (price_dir / "BROKEN" / f"{START}_{END}.json").write_text("{not json", encoding="utf-8")
        return bad, good

    def test_dry_run_is_the_default_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            price_dir = Path(temporary)
            bad, good = self._tree(price_dir)
            before = {p: p.read_bytes() for p in (bad, good)}
            code, out, _err = self._run("--price-dir", str(price_dir))
            self.assertEqual(code, 0)
            self.assertIn("AKFIS", out)
            self.assertNotIn("ZZZZZ", out)
            self.assertIn("Incomplete files: 1; unreadable (skipped): 1", out)
            self.assertIn("dry-run", out)
            self.assertEqual({p: p.read_bytes() for p in (bad, good)}, before)
            self.assertFalse((price_dir / "_incomplete").exists())
            code, _out, _err = self._run("--price-dir", str(price_dir), "--dry-run")
            self.assertEqual(code, 0)
            self.assertTrue(bad.exists())

    def test_apply_moves_without_deleting_and_a_second_apply_finds_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            price_dir = Path(temporary)
            bad, good = self._tree(price_dir)
            original = bad.read_bytes()
            code, out, _err = self._run("--price-dir", str(price_dir), "--apply",
                                        "--run-date", "2026-10-06")
            self.assertEqual(code, 0, out)
            moved = price_dir / "_incomplete" / "2026-10-06" / "AKFIS" / bad.name
            self.assertFalse(bad.exists())
            self.assertEqual(moved.read_bytes(), original)
            self.assertTrue(good.exists())
            code, out, _err = self._run("--price-dir", str(price_dir), "--apply",
                                        "--run-date", "2026-10-06")
            self.assertEqual(code, 0)
            self.assertIn("Incomplete files: 0", out)

    def test_apply_never_overwrites_an_existing_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            price_dir = Path(temporary)
            bad = _write_capture(price_dir, "AKFIS", _chart(null_last_close=True))
            target = price_dir / "_incomplete" / "2026-10-06" / "AKFIS" / bad.name
            target.parent.mkdir(parents=True)
            target.write_text("keep", encoding="utf-8")
            code, _out, err = self._run("--price-dir", str(price_dir), "--apply",
                                        "--run-date", "2026-10-06")
            self.assertEqual(code, 1)
            self.assertIn("target exists", err)
            self.assertTrue(bad.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_apply_and_dry_run_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._run("--apply", "--dry-run")

    def test_missing_price_dir_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, out, _err = self._run("--price-dir", str(Path(temporary) / "nope"))
        self.assertEqual(code, 0)
        self.assertIn("Incomplete files: 0", out)


if __name__ == "__main__":
    unittest.main()
