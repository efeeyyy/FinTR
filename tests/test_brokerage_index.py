"""Offline tests for the in-memory brokerage code resolver."""

import unittest

from fintr.data import (
    BrokerageLabel,
    BrokerageResolutionStatus,
    LiveBrokerageIndex,
    LiveBrokerageRow,
    LiveBrokeragesResponse,
)


def response(*rows: tuple[str, str, str]) -> LiveBrokeragesResponse:
    return LiveBrokeragesResponse(tuple(
        LiveBrokerageRow(code=code, title=title, short_title=short_title)
        for code, title, short_title in rows
    ))


class LiveBrokerageIndexTests(unittest.TestCase):
    def test_exact_code_returns_both_candidate_labels_without_repr_leak(self):
        index = LiveBrokerageIndex.from_response(response((
            "SYNTH-CODE-01", "Synthetic Long Label", "Synthetic Short",
        )))

        result = index.resolve("SYNTH-CODE-01")

        self.assertIs(result.status, BrokerageResolutionStatus.RESOLVED)
        self.assertIsInstance(result.label, BrokerageLabel)
        self.assertEqual(result.label.title, "Synthetic Long Label")
        self.assertEqual(result.label.short_title, "Synthetic Short")
        rendered = repr(index) + repr(result) + repr(result.label)
        for sensitive in (
            "SYNTH-CODE-01", "Synthetic Long Label", "Synthetic Short",
        ):
            self.assertNotIn(sensitive, rendered)

    def test_unknown_and_non_exact_codes_are_explicitly_unresolved(self):
        index = LiveBrokerageIndex.from_response(response((
            "SYNTH-CODE-02", "Synthetic Label", "Synthetic",
        )))

        for unknown in ("missing-code", "synth-code-02", " SYNTH-CODE-02"):
            with self.subTest(code=unknown):
                result = index.resolve(unknown)
                self.assertIs(result.status, BrokerageResolutionStatus.UNRESOLVED)
                self.assertIsNone(result.label)

    def test_identical_duplicate_rows_collapse_without_order_dependence(self):
        index = LiveBrokerageIndex.from_response(response(
            ("SYNTH-DUP", "Synthetic Label", "Synthetic"),
            ("SYNTH-DUP", "Synthetic Label", "Synthetic"),
        ))

        result = index.resolve("SYNTH-DUP")

        self.assertIs(result.status, BrokerageResolutionStatus.RESOLVED)
        self.assertEqual(result.label.short_title, "Synthetic")

    def test_conflicting_names_for_one_code_are_explicitly_ambiguous(self):
        conflict_sets = (
            (
                ("SYNTH-CONFLICT", "First Synthetic Name", "Same Short"),
                ("SYNTH-CONFLICT", "Second Synthetic Name", "Same Short"),
            ),
            (
                ("SYNTH-CONFLICT", "Same Long Name", "First Short"),
                ("SYNTH-CONFLICT", "Same Long Name", "Second Short"),
            ),
        )
        for rows in conflict_sets:
            with self.subTest(conflicting_labels=len(rows)):
                result = LiveBrokerageIndex.from_response(
                    LiveBrokeragesResponse(tuple(
                        LiveBrokerageRow(code, title, short_title)
                        for code, title, short_title in rows
                    ))
                ).resolve("SYNTH-CONFLICT")
                self.assertIs(result.status, BrokerageResolutionStatus.AMBIGUOUS)
                self.assertIsNone(result.label)
                rendered = repr(result)
                self.assertNotIn("First Synthetic Name", rendered)
                self.assertNotIn("Second Synthetic Name", rendered)
                self.assertNotIn("First Short", rendered)
                self.assertNotIn("Second Short", rendered)

    def test_index_is_immutable_and_bad_lookup_type_is_redacted(self):
        index = LiveBrokerageIndex.from_response(response((
            "SYNTH-CODE-03", "Synthetic Label", "Synthetic",
        )))
        with self.assertRaises(TypeError):
            index._by_code["SYNTH-CODE-04"] = ()
        with self.assertRaises(TypeError) as captured:
            index.resolve(123)
        self.assertEqual(str(captured.exception), "brokerage code must be a string")
        self.assertNotIn("123", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
