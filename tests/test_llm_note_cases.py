"""Golden good/bad corpus for the daily-note validator (``fintr.llm_note.validate``).

``tests/fixtures/llm_note_cases/packet.json`` is one synthetic input packet
(no real market data) and ``cases/*.json`` are hand-written note outputs, each
with ``expected`` (``accept``/``reject``), ``expected_reason`` (the validator
category it should give) and a ``description``. Every case is run through the
real ``validate_note``.

A case the validator currently gets wrong is never dropped: it carries
``known_gap: true``, ``known_gap_reason`` and ``current_result`` (what the
validator does today). The test asserts that current behaviour, so any change
-- a gap closed or a regression -- is noticed; when a gap is closed the case is
flipped by deleting its three ``known_gap*`` keys. Known gaps are listed on
stderr by ``test_known_gaps_are_listed``.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

from fintr.llm_note.validate import validate_note


CASES_DIR = Path(__file__).resolve().parent / "fixtures" / "llm_note_cases"
CASE_KEYS = {"case_id", "description", "expected", "expected_reason", "note"}
GAP_KEYS = {"known_gap", "known_gap_reason", "current_result"}
ACCEPTED_REASONS = {
    "schema", "assessment_value", "coverage", "evidence_path", "forbidden_phrase", "number_word",
    "number_not_in_input", "number_format", "sensitive_content",
}


def _load_cases() -> list[tuple[str, dict]]:
    return [(path.name, json.loads(path.read_text(encoding="utf-8")))
            for path in sorted((CASES_DIR / "cases").glob("*.json"))]


PACKET = json.loads((CASES_DIR / "packet.json").read_text(encoding="utf-8"))
CASES = _load_cases()


class CaseFileTests(unittest.TestCase):
    def test_corpus_is_well_formed(self):
        ids = [case["case_id"] for _, case in CASES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(CASES), 20)
        for name, case in CASES:
            with self.subTest(name):
                self.assertTrue(CASE_KEYS <= set(case) <= CASE_KEYS | GAP_KEYS, set(case))
                self.assertEqual(name.split("_", 1)[1], case["case_id"] + ".json")
                self.assertIn(case["expected"], ("accept", "reject"))
                if case["expected"] == "accept":
                    self.assertIsNone(case["expected_reason"])
                    self.assertNotIn("known_gap", case)
                if case["expected_reason"] is not None:
                    self.assertIn(case["expected_reason"], ACCEPTED_REASONS)
                if "known_gap" in case:
                    self.assertTrue(GAP_KEYS <= set(case))
                    self.assertIs(case["known_gap"], True)
                    self.assertTrue(case["known_gap_reason"].strip())
                    self.assertEqual(set(case["current_result"]), {"ok", "reason"})

    def test_corpus_mixes_good_and_bad_notes(self):
        accepts = [c for _, c in CASES if c["expected"] == "accept"]
        self.assertTrue(4 <= len(accepts) <= 8, len(accepts))
        self.assertGreaterEqual(len(CASES) - len(accepts), 15)

    def test_packet_is_synthetic_and_has_a_single_required_assessment(self):
        self.assertEqual(PACKET["schema"], "fintr_llm_note_packet_v1")
        self.assertEqual(list(PACKET["symbols"]), ["SYNA"])
        self.assertEqual(len(PACKET["required_assessments"]), 1)


class GoldenCaseTests(unittest.TestCase):
    def test_every_case_matches_the_validator(self):
        for name, case in CASES:
            with self.subTest(name):
                result = validate_note(case["note"], PACKET)
                current = (result.ok, result.category)
                if case.get("known_gap"):
                    gap = case["current_result"]
                    self.assertEqual(current, (gap["ok"], gap["reason"]),
                                     "the validator's behaviour on a known gap changed: update the case "
                                     "(gap closed -> delete the known_gap* keys) and the docs")
                    wanted = (case["expected"] == "accept", case["expected_reason"])
                    self.assertNotEqual(current[0], wanted[0], "a known gap must be a wrong accept/reject")
                elif case["expected"] == "accept":
                    self.assertEqual(current, (True, None), result)
                else:
                    self.assertEqual(current, (False, case["expected_reason"]), result)

    def test_known_gaps_are_listed(self):
        gaps = [(case["case_id"], case["known_gap_reason"]) for _, case in CASES if case.get("known_gap")]
        print(f"\nllm_note_cases: {len(CASES)} cases, {len(gaps)} known gap(s):", file=sys.stderr)
        for case_id, reason in gaps:
            print(f"  - {case_id}: {reason}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
