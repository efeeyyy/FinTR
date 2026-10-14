"""Provenance fields of the LLM note writer (Efe kararı 2026-10-14): late-generation marker."""
from __future__ import annotations

import importlib.util
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("llm_note_script", ROOT / "scripts" / "llm_note.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


if __name__ == "__main__":
    unittest.main()
