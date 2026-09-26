"""Offline tests for ``fintr.data.corporate_actions``.

All symbols, dates, multipliers and URLs are synthetic fixtures; no real
corporate-action fact is used or reproduced. This project never fetches
kap.org.tr or any other network source for a corporate action -- these
tests only exercise the strict registry loader/validator.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from fintr.data.corporate_actions import (
    CorporateActionsError,
    REGISTRY_SCHEMA,
    action_to_json,
    load_corporate_action_registry,
    parse_corporate_action_registry,
    registry_to_json,
)


def _action(
    action_id="A1", type_="bonus_issue", lot_multiplier="6",
    ex_date="2026-05-04", custody_effective_date="2026-05-06",
    source_url="https://example.test/kap-notice", source_published_date="2026-05-01",
    source_file_sha256=None, notes="synthetic fixture",
) -> dict:
    return {
        "action_id": action_id,
        "type": type_,
        "lot_multiplier": lot_multiplier,
        "ex_date": ex_date,
        "custody_effective_date": custody_effective_date,
        "source_url": source_url,
        "source_published_date": source_published_date,
        "source_file_sha256": source_file_sha256,
        "entered_by": "user",
        "entered_at_utc": "2026-05-02T00:00:00Z",
        "verification": "user_asserted_from_primary_source",
        "notes": notes,
    }


def _document(symbol="TEST", actions=None) -> dict:
    return {"schema": REGISTRY_SCHEMA, "symbol": symbol, "actions": actions or [_action()]}


class RegistryValidationTests(unittest.TestCase):
    def test_valid_single_action_round_trips(self):
        registry = parse_corporate_action_registry(_document())
        self.assertEqual(registry.symbol, "TEST")
        self.assertEqual(len(registry.actions), 1)
        action = registry.actions[0]
        self.assertEqual(action.lot_multiplier, Decimal("6"))
        self.assertEqual(action.ex_date, date(2026, 5, 4))
        self.assertEqual(action.custody_effective_date, date(2026, 5, 6))
        # round-trip through registry_to_json/action_to_json
        redone = parse_corporate_action_registry(registry_to_json(registry))
        self.assertEqual(redone, registry)

    def test_wrong_schema_is_rejected(self):
        document = _document()
        document["schema"] = "not_the_schema"
        with self.assertRaises(CorporateActionsError):
            parse_corporate_action_registry(document)

    def test_non_positive_multiplier_is_rejected(self):
        for bad in ("0", "-1"):
            with self.assertRaises(CorporateActionsError):
                parse_corporate_action_registry(_document(actions=[_action(lot_multiplier=bad)]))

    def test_custody_effective_date_before_ex_date_is_rejected(self):
        with self.assertRaises(CorporateActionsError):
            parse_corporate_action_registry(_document(actions=[
                _action(ex_date="2026-05-10", custody_effective_date="2026-05-05"),
            ]))

    def test_custody_effective_date_equal_to_ex_date_is_accepted(self):
        registry = parse_corporate_action_registry(_document(actions=[
            _action(ex_date="2026-05-10", custody_effective_date="2026-05-10"),
        ]))
        self.assertEqual(registry.actions[0].custody_effective_date, date(2026, 5, 10))

    def test_invalid_type_is_rejected(self):
        with self.assertRaises(CorporateActionsError):
            parse_corporate_action_registry(_document(actions=[_action(type_="dividend")]))

    def test_wrong_entered_by_is_rejected(self):
        action = _action()
        action["entered_by"] = "system"
        with self.assertRaises(CorporateActionsError):
            parse_corporate_action_registry(_document(actions=[action]))

    def test_wrong_verification_is_rejected(self):
        action = _action()
        action["verification"] = "verified_by_provider"
        with self.assertRaises(CorporateActionsError):
            parse_corporate_action_registry(_document(actions=[action]))

    def test_duplicate_action_id_is_rejected(self):
        actions = [
            _action(action_id="A1", ex_date="2026-05-04", custody_effective_date="2026-05-06"),
            _action(action_id="A1", ex_date="2026-08-04", custody_effective_date="2026-08-06"),
        ]
        with self.assertRaises(CorporateActionsError):
            parse_corporate_action_registry(_document(actions=actions))

    def test_duplicate_ex_date_is_rejected(self):
        actions = [
            _action(action_id="A1", ex_date="2026-05-04", custody_effective_date="2026-05-06"),
            _action(action_id="A2", ex_date="2026-05-04", custody_effective_date="2026-05-07"),
        ]
        with self.assertRaises(CorporateActionsError):
            parse_corporate_action_registry(_document(actions=actions))

    def test_overlapping_windows_are_rejected(self):
        # A1's custody_effective_date (2026-08-10) falls after A2's ex_date
        # (2026-08-01): the multiplier to apply between those two dates is
        # ambiguous, so this must be rejected.
        actions = [
            _action(action_id="A1", ex_date="2026-05-04", custody_effective_date="2026-08-10"),
            _action(action_id="A2", ex_date="2026-08-01", custody_effective_date="2026-08-15"),
        ]
        with self.assertRaises(CorporateActionsError):
            parse_corporate_action_registry(_document(actions=actions))

    def test_non_overlapping_sequential_actions_are_accepted_and_sorted(self):
        actions = [
            _action(action_id="LATER", ex_date="2026-08-04", custody_effective_date="2026-08-06"),
            _action(action_id="EARLIER", ex_date="2026-05-04", custody_effective_date="2026-05-06"),
        ]
        registry = parse_corporate_action_registry(_document(actions=actions))
        self.assertEqual([a.action_id for a in registry.actions], ["EARLIER", "LATER"])

    def test_optional_fields_may_be_null(self):
        action = _action(source_url=None, source_published_date=None, source_file_sha256=None)
        action["notes"] = None
        registry = parse_corporate_action_registry(_document(actions=[action]))
        parsed = registry.actions[0]
        self.assertIsNone(parsed.source_url)
        self.assertIsNone(parsed.source_published_date)
        self.assertIsNone(parsed.source_file_sha256)
        self.assertEqual(parsed.notes, "")

    def test_action_to_json_keeps_exact_decimal_string(self):
        registry = parse_corporate_action_registry(_document(actions=[_action(lot_multiplier="1.5")]))
        self.assertEqual(action_to_json(registry.actions[0])["lot_multiplier"], "1.5")


class LoadCorporateActionRegistryTests(unittest.TestCase):
    def test_loads_a_saved_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TEST.json"
            path.write_text(
                __import__("json").dumps(_document()), encoding="utf-8",
            )
            registry = load_corporate_action_registry(path)
            self.assertEqual(registry.symbol, "TEST")

    def test_unreadable_or_malformed_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CorporateActionsError):
                load_corporate_action_registry(path)

    def test_missing_file_is_rejected(self):
        with self.assertRaises(CorporateActionsError):
            load_corporate_action_registry(Path("no-such-registry-file.json"))


if __name__ == "__main__":
    unittest.main()
