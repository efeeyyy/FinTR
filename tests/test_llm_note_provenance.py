"""Provenance fields of the LLM note writer (Efe kararı 2026-10-14): late-generation marker."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("llm_note_script", ROOT / "scripts" / "llm_note.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULE = "next_trading_day_bist_calendar_v1"


def _fixture_calendar(closures: list[tuple[str, str]], *, start="2030-01-01", end="2030-12-31") -> dict:
    """Hypothetical calendar (year 2030), not the real config."""
    return {
        "schema_version": 1, "status": "approved", "verified_by": "fixture", "verified_on": "2030-01-01",
        "market": "fixture", "coverage": {"start": start, "end": end}, "generated_utc": "2030-01-01",
        "notes": ["fixture"],
        "sources": [{"id": "s1", "url": "https://example.invalid/x", "accessed_utc": "2030-01-01T00:00:00Z",
                     "title": "fixture"}],
        "days": [
            {"date": d, "weekday": w, "kind": "full_closure", "session_end_local": None,
             "name_tr": "fixture", "source_id": "s1", "status": "official", "note": ""}
            for d, w in closures
        ],
    }


class GenerationProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.fn = _load().generation_provenance
        self.as_of = date(2026, 10, 9)

    def test_next_day_run_is_not_late(self):
        out = self.fn(self.as_of, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc))
        self.assertFalse(out["generated_late"])
        self.assertEqual(out["generated_on_local"], "2026-10-10")

    def test_friday_as_of_monday_not_late_tuesday_late(self):
        # 2026-10-09 is a Friday
        self.assertFalse(self.fn(self.as_of, datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc))["generated_late"])
        self.assertTrue(self.fn(self.as_of, datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc))["generated_late"])

    def test_thursday_as_of_saturday_is_late(self):
        thursday = date(2026, 10, 8)
        self.assertFalse(self.fn(thursday, datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc))["generated_late"])
        self.assertTrue(self.fn(thursday, datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc))["generated_late"])

    def test_istanbul_date_used(self):
        # 21:30 UTC on 10-12 is already 10-13 in Istanbul (UTC+3): Friday as_of -> late
        out = self.fn(self.as_of, datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc))
        self.assertEqual(out["generated_on_local"], "2026-10-13")
        self.assertTrue(out["generated_late"])


class CalendarRuleTest(unittest.TestCase):
    """``generated_late`` follows the BIST calendar's next trading day (holidays count)."""

    def setUp(self):
        self.module = _load()
        self.fn = self.module.generation_provenance
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, document) -> Path:
        path = Path(self.tmp.name) / "cal.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def _run(self, as_of, utc, path):
        return self.fn(as_of, utc, path)

    def test_holiday_between_pushes_the_deadline(self):
        # fixture: Tue 2030-03-05 closed. as_of Mon 03-04 -> next trading day Wed 03-06.
        path = self._path(_fixture_calendar([("2030-03-05", "Tuesday")]))
        as_of = date(2030, 3, 4)
        on_tuesday = self._run(as_of, datetime(2030, 3, 5, 9, 0, tzinfo=timezone.utc), path)
        on_wednesday = self._run(as_of, datetime(2030, 3, 6, 9, 0, tzinfo=timezone.utc), path)
        on_thursday = self._run(as_of, datetime(2030, 3, 7, 9, 0, tzinfo=timezone.utc), path)
        self.assertEqual([on_tuesday["generated_late"], on_wednesday["generated_late"],
                          on_thursday["generated_late"]], [False, False, True])
        self.assertEqual({o["generated_late_rule"] for o in (on_tuesday, on_wednesday, on_thursday)}, {RULE})

    def test_without_a_holiday_the_same_dates_are_late_earlier(self):
        path = self._path(_fixture_calendar([("2030-12-25", "Wednesday")]))
        out = self._run(date(2030, 3, 4), datetime(2030, 3, 6, 9, 0, tzinfo=timezone.utc), path)
        self.assertTrue(out["generated_late"])  # next trading day is Tue 03-05
        self.assertEqual(out["generated_late_rule"], RULE)

    def test_real_calendar_across_a_holiday(self):
        # 2026-04-23 (Thu) is an official full closure: as_of Wed 04-22 -> next trading day Fri 04-24.
        as_of = date(2026, 4, 22)
        friday = self.fn(as_of, datetime(2026, 4, 24, 9, 0, tzinfo=timezone.utc))
        saturday = self.fn(as_of, datetime(2026, 4, 25, 9, 0, tzinfo=timezone.utc))
        self.assertFalse(friday["generated_late"])  # the old weekday-only rule said True
        self.assertTrue(saturday["generated_late"])
        self.assertEqual(friday["generated_late_rule"], RULE)

    def test_as_of_outside_coverage_falls_back_to_next_weekday_and_says_so(self):
        path = self._path(_fixture_calendar([("2030-03-05", "Tuesday")]))
        as_of = date(2031, 3, 4)  # Tuesday, after the coverage end
        out = self._run(as_of, datetime(2031, 3, 5, 9, 0, tzinfo=timezone.utc), path)
        self.assertEqual(out["generated_late_rule"], "next_weekday_outside_calendar_coverage")
        self.assertFalse(out["generated_late"])
        self.assertTrue(self._run(as_of, datetime(2031, 3, 6, 9, 0, tzinfo=timezone.utc), path)["generated_late"])

    def test_next_trading_day_beyond_coverage_end_falls_back(self):
        path = self._path(_fixture_calendar([("2030-03-05", "Tuesday")], end="2030-12-31"))
        as_of = date(2030, 12, 31)  # Tuesday, last covered day
        out = self._run(as_of, datetime(2031, 1, 1, 9, 0, tzinfo=timezone.utc), path)
        self.assertEqual(out["generated_late_rule"], "next_weekday_outside_calendar_coverage")

    def test_unreadable_calendar_falls_back_and_says_so(self):
        out = self._run(date(2030, 3, 4), datetime(2030, 3, 5, 9, 0, tzinfo=timezone.utc),
                        Path(self.tmp.name) / "missing.json")
        self.assertEqual(out["generated_late_rule"], "next_weekday_calendar_unreadable")
        self.assertFalse(out["generated_late"])

    def test_invalid_calendar_is_treated_as_unreadable_not_trusted(self):
        draft = _fixture_calendar([("2030-03-05", "Tuesday")])
        draft["status"] = "draft_unverified"
        out = self._run(date(2030, 3, 4), datetime(2030, 3, 6, 9, 0, tzinfo=timezone.utc), self._path(draft))
        self.assertEqual(out["generated_late_rule"], "next_weekday_calendar_unreadable")


if __name__ == "__main__":
    unittest.main()
