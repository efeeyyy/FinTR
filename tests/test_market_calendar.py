import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from fintr.data.market_calendar import (
    DEFAULT_MARKET_CALENDAR_PATH,
    MarketCalendarError,
    OutsideCoverageError,
    load_market_calendar,
    parse_market_calendar,
)

ROOT = Path(__file__).resolve().parents[1]


def _day(day: str, weekday: str, kind: str, *, end=None, status="official", source="s1") -> dict:
    return {
        "date": day, "weekday": weekday, "kind": kind, "session_end_local": end,
        "name_tr": f"fixture {day}", "source_id": source, "status": status, "note": "",
    }


# Hypothetical fixture (year 2030), not the real config.
# 2030-03-04 Mon half day, 03-05 Tue full closure, 03-09 Sat listed weekend closure.
FIXTURE = {
    "schema_version": 1,
    "status": "approved",
    "verified_by": "fixture",
    "verified_on": "2030-01-01",
    "market": "fixture",
    "coverage": {"start": "2030-01-01", "end": "2030-12-31"},
    "generated_utc": "2030-01-01",
    "notes": ["fixture"],
    "sources": [{"id": "s1", "url": "https://example.invalid/x", "accessed_utc": "2030-01-01T00:00:00Z",
                 "title": "fixture"}],
    "days": [
        _day("2030-03-04", "Monday", "half_day", end="13:00"),
        _day("2030-03-05", "Tuesday", "full_closure"),
        _day("2030-03-09", "Saturday", "full_closure", status="uncertain"),
    ],
}


def _with(**changes):
    doc = copy.deepcopy(FIXTURE)
    doc.update(changes)
    return doc


def _with_days(*days):
    return _with(days=list(days))


class TradingDayTests(unittest.TestCase):
    def setUp(self):
        self.cal = parse_market_calendar(FIXTURE)

    def test_full_closure_is_not_a_trading_day(self):
        self.assertTrue(self.cal.is_full_closure(date(2030, 3, 5)))
        self.assertFalse(self.cal.is_trading_day(date(2030, 3, 5)))

    def test_half_day_is_a_trading_day(self):
        self.assertTrue(self.cal.is_half_day(date(2030, 3, 4)))
        self.assertFalse(self.cal.is_full_closure(date(2030, 3, 4)))
        self.assertTrue(self.cal.is_trading_day(date(2030, 3, 4)))

    def test_ordinary_weekday_trades(self):
        self.assertTrue(self.cal.is_trading_day(date(2030, 3, 6)))
        self.assertFalse(self.cal.is_full_closure(date(2030, 3, 6)))

    def test_unlisted_weekend_is_not_a_trading_day_but_not_a_listed_closure(self):
        self.assertFalse(self.cal.is_trading_day(date(2030, 3, 10)))  # Sunday
        self.assertFalse(self.cal.is_full_closure(date(2030, 3, 10)))

    def test_listed_weekend_closure(self):
        self.assertTrue(self.cal.is_full_closure(date(2030, 3, 9)))
        self.assertFalse(self.cal.is_trading_day(date(2030, 3, 9)))

    def test_next_and_previous_trading_day_skip_weekend_and_closure(self):
        self.assertEqual(self.cal.next_trading_day(date(2030, 3, 4)), date(2030, 3, 6))  # over the closure
        self.assertEqual(self.cal.next_trading_day(date(2030, 3, 8)), date(2030, 3, 11))  # Fri -> Mon
        self.assertEqual(self.cal.next_trading_day(date(2030, 3, 9)), date(2030, 3, 11))  # from a weekend
        self.assertEqual(self.cal.previous_trading_day(date(2030, 3, 6)), date(2030, 3, 4))
        self.assertEqual(self.cal.previous_trading_day(date(2030, 3, 11)), date(2030, 3, 8))

    def test_outside_coverage_raises_everywhere(self):
        for day in (date(2029, 12, 31), date(2031, 1, 1)):
            for call in (self.cal.is_full_closure, self.cal.is_half_day, self.cal.is_trading_day,
                         self.cal.next_trading_day, self.cal.previous_trading_day, self.cal.listed_day):
                with self.assertRaises(OutsideCoverageError):
                    call(day)

    def test_neighbour_search_leaving_coverage_raises(self):
        self.assertEqual(self.cal.next_trading_day(date(2030, 12, 30)), date(2030, 12, 31))
        with self.assertRaises(OutsideCoverageError):
            self.cal.next_trading_day(date(2030, 12, 31))
        with self.assertRaises(OutsideCoverageError):
            self.cal.previous_trading_day(date(2030, 1, 1))  # nothing before 2030-01-01

    def test_coverage_boundaries_are_inclusive(self):
        self.assertTrue(self.cal.covers(date(2030, 1, 1)))
        self.assertTrue(self.cal.covers(date(2030, 12, 31)))


class ValidationTests(unittest.TestCase):
    def assertInvalid(self, document):
        with self.assertRaises(MarketCalendarError):
            parse_market_calendar(document)

    def test_fixture_is_valid(self):
        parse_market_calendar(FIXTURE)

    def test_not_an_object_or_wrong_schema(self):
        self.assertInvalid([])
        self.assertInvalid(_with(schema_version=2))
        self.assertInvalid(_with(schema_version=True))
        self.assertInvalid(_with(schema_version="1"))

    def test_draft_or_unapproved_is_refused(self):
        self.assertInvalid(_with(status="draft_unverified"))
        self.assertInvalid(_with(verified_by=None))
        self.assertInvalid(_with(verified_by=""))
        self.assertInvalid(_with(verified_on="2030-1-1"))

    def test_unknown_and_missing_top_keys(self):
        self.assertInvalid(_with(extra=1))
        doc = copy.deepcopy(FIXTURE)
        del doc["sources"]
        self.assertInvalid(doc)

    def test_coverage(self):
        self.assertInvalid(_with(coverage={"start": "2030-01-01"}))
        self.assertInvalid(_with(coverage={"start": "2030-01-01", "end": "2030-12-31", "x": 1}))
        self.assertInvalid(_with(coverage={"start": "2031-01-01", "end": "2030-12-31"}))
        self.assertInvalid(_with(coverage={"start": "2030-01-01", "end": "not-a-date"}))

    def test_empty_days_or_sources(self):
        self.assertInvalid(_with(days=[]))
        self.assertInvalid(_with(sources=[]))
        self.assertInvalid(_with(notes=[]))

    def test_day_keys_exact(self):
        day = _day("2030-03-05", "Tuesday", "full_closure")
        extra = dict(day, extra=1)
        self.assertInvalid(_with_days(extra))
        missing = dict(day)
        del missing["note"]
        self.assertInvalid(_with_days(missing))

    def test_dates_sorted_and_unique(self):
        a = _day("2030-03-05", "Tuesday", "full_closure")
        b = _day("2030-03-06", "Wednesday", "full_closure")
        self.assertInvalid(_with_days(b, a))
        self.assertInvalid(_with_days(a, a))

    def test_day_outside_declared_coverage(self):
        self.assertInvalid(_with_days(_day("2031-03-05", "Wednesday", "full_closure")))

    def test_weekday_must_match(self):
        self.assertInvalid(_with_days(_day("2030-03-05", "Monday", "full_closure")))

    def test_kind_and_status(self):
        self.assertInvalid(_with_days(_day("2030-03-05", "Tuesday", "holiday")))
        self.assertInvalid(_with_days(_day("2030-03-05", "Tuesday", "full_closure", status="maybe")))

    def test_session_end(self):
        self.assertInvalid(_with_days(_day("2030-03-05", "Tuesday", "full_closure", end="13:00")))
        self.assertInvalid(_with_days(_day("2030-03-04", "Monday", "half_day", end="25:00")))
        self.assertInvalid(_with_days(_day("2030-03-04", "Monday", "half_day", end=1300)))
        parse_market_calendar(_with_days(_day("2030-03-04", "Monday", "half_day", end=None)))

    def test_source_reference_must_exist_and_ids_be_unique(self):
        self.assertInvalid(_with_days(_day("2030-03-05", "Tuesday", "full_closure", source="nope")))
        source = FIXTURE["sources"][0]
        self.assertInvalid(_with(sources=[source, dict(source)]))

    def test_unreadable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaises(MarketCalendarError):
                load_market_calendar(missing)
            bad = Path(directory) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(MarketCalendarError):
                load_market_calendar(bad)


class RealConfigTests(unittest.TestCase):
    """Facts below come from the approved config itself (docs/research/bist_holiday_calendar_draft.md)."""

    def setUp(self):
        self.cal = load_market_calendar(ROOT / DEFAULT_MARKET_CALENDAR_PATH)

    def test_coverage_and_counts(self):
        self.assertEqual((self.cal.coverage_start, self.cal.coverage_end),
                         (date(2023, 1, 1), date(2027, 3, 31)))
        self.assertEqual(len(self.cal.days), 77)

    def test_known_days(self):
        self.assertTrue(self.cal.is_full_closure(date(2026, 4, 23)))  # Thursday, official
        self.assertFalse(self.cal.is_trading_day(date(2026, 4, 23)))
        self.assertTrue(self.cal.is_half_day(date(2026, 10, 28)))
        self.assertTrue(self.cal.is_trading_day(date(2026, 10, 28)))
        self.assertTrue(self.cal.is_full_closure(date(2026, 10, 29)))
        self.assertEqual(self.cal.next_trading_day(date(2026, 4, 22)), date(2026, 4, 24))
        self.assertEqual(self.cal.next_trading_day(date(2026, 10, 28)), date(2026, 10, 30))

    def test_approval_metadata(self):
        document = json.loads((ROOT / DEFAULT_MARKET_CALENDAR_PATH).read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "approved")
        self.assertEqual(document["verified_by"], "Efe")
        self.assertEqual(document["verified_on"], "2026-10-16")


if __name__ == "__main__":
    unittest.main()
