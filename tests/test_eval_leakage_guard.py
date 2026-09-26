"""Synthetic-data-only checks for the eval leakage guard.

The guard rejects a case on symbol alone (company-level), regardless of its
observation period, if that symbol appears anywhere in the development
registry. Uses small, in-test registries and cases only; the real bundled
registry (``src/fintr/evaluation/dev_registry.json``) is exercised
separately to confirm it actually rejects the two known AKFIS development
items.
"""

from __future__ import annotations

from datetime import date
import hashlib
import unittest

from fintr.evaluation.leakage_guard import (
    DevRegistryEntry,
    ManifestError,
    find_leakage_violations,
    load_dev_registry,
    parse_dev_registry,
)
from fintr.evaluation.manifest import parse_eval_case


_SHA_A = hashlib.sha256(b"synthetic-capture-a").hexdigest()
_SHA_B = hashlib.sha256(b"synthetic-capture-b").hexdigest()


def _case_dict(**overrides) -> dict:
    base = {
        "case_id": "synthetic_case_0001",
        "split": "eval",
        "symbol": "ZZTEST",
        "data_families": ["depth"],
        "observation_period": {"start": "2030-01-01", "end": "2030-01-02"},
        "horizons": ["1d"],
        "source": {
            "path": "data/eval/cases/synthetic_case_0001/capture.json",
            "sha256": _SHA_A,
            "captured_at": "2030-01-02T10:00:00+03:00",
        },
        "permission": {
            "basis": "user_asserted_local_storage_approval",
            "asserted_on": "2030-01-02",
            "independently_verified": False,
        },
        "leakage_group": "ZZTEST|2030-01-01|2030-01-02",
        "notes": "sentetik: eksik veya celiski yok",
    }
    base.update(overrides)
    return base


def _registry(*entries: DevRegistryEntry) -> tuple[DevRegistryEntry, ...]:
    return tuple(entries)


_DEV_ENTRY = DevRegistryEntry(
    item_id="synthetic_dev_item",
    symbol="ZZTEST",
    start=date(2030, 1, 5),
    end=date(2030, 1, 10),
    source_path="data/interim/pilot/synthetic.json",
    classification="synthetic_development",
)


class LeakageGuardSyntheticTests(unittest.TestCase):
    def test_no_violation_for_symbol_not_in_registry(self):
        cases = (parse_eval_case(_case_dict(
            symbol="OTHR",
            observation_period={"start": "2030-01-06", "end": "2030-01-07"},
            leakage_group="OTHR|2030-01-06|2030-01-07",
        )),)
        violations = find_leakage_violations(cases, _registry(_DEV_ENTRY))
        self.assertEqual([v for v in violations if v.reason == "development_symbol"], [])

    def test_same_symbol_rejected_even_with_no_date_overlap(self):
        # Dev item covers 2030-01-05..2030-01-10; this case is decades away,
        # so under the old date-overlap rule it would have passed. Symbol
        # match alone must still reject it (company-level split rule).
        cases = (parse_eval_case(_case_dict(
            observation_period={"start": "2099-06-01", "end": "2099-06-02"},
            leakage_group="ZZTEST|2099-06-01|2099-06-02",
        )),)
        violations = find_leakage_violations(cases, _registry(_DEV_ENTRY))
        self.assertTrue(any(v.reason == "development_symbol" for v in violations))

    def test_symbol_match_is_case_insensitive(self):
        lowercase_entry = DevRegistryEntry(
            item_id="lowercase_dev_item", symbol="zztest",
            start=date(2030, 1, 5), end=date(2030, 1, 10),
            source_path="data/interim/pilot/synthetic.json",
            classification="synthetic_development",
        )
        cases = (parse_eval_case(_case_dict()),)
        violations = find_leakage_violations(cases, _registry(lowercase_entry))
        self.assertTrue(any(v.reason == "development_symbol" for v in violations))

    def test_development_only_source_path_rejected_regardless_of_dates(self):
        cases = (parse_eval_case(_case_dict(
            symbol="OTHR",
            observation_period={"start": "2099-01-01", "end": "2099-01-02"},
            leakage_group="OTHR|2099-01-01|2099-01-02",
            source={
                "path": "data/interim/period_analysis/OTHR/2099-01-01_2099-01-02.json",
                "sha256": _SHA_A,
                "captured_at": "2099-01-02T10:00:00+03:00",
            },
        )),)
        violations = find_leakage_violations(cases, _registry())
        self.assertTrue(any(v.reason == "development_material_path" for v in violations))

    def test_duplicate_case_id_rejected(self):
        cases = (
            parse_eval_case(_case_dict()),
            parse_eval_case(_case_dict(
                symbol="OTHR", observation_period={"start": "2031-01-01", "end": "2031-01-02"},
                leakage_group="OTHR|2031-01-01|2031-01-02",
                source={
                    "path": "data/eval/cases/other/capture.json", "sha256": _SHA_B,
                    "captured_at": "2031-01-02T10:00:00+03:00",
                },
            )),
        )
        violations = find_leakage_violations(cases, _registry())
        self.assertTrue(any(v.reason == "duplicate_case_id" for v in violations))

    def test_duplicate_capture_checksum_rejected(self):
        cases = (
            parse_eval_case(_case_dict()),
            parse_eval_case(_case_dict(
                case_id="synthetic_case_0002", symbol="OTHR",
                observation_period={"start": "2031-01-01", "end": "2031-01-02"},
                leakage_group="OTHR|2031-01-01|2031-01-02",
                source={
                    "path": "data/eval/cases/other/capture.json", "sha256": _SHA_A,
                    "captured_at": "2031-01-02T10:00:00+03:00",
                },
            )),
        )
        violations = find_leakage_violations(cases, _registry())
        self.assertTrue(any(v.reason == "duplicate_capture_checksum" for v in violations))

    def test_duplicate_leakage_group_rejected(self):
        cases = (
            parse_eval_case(_case_dict()),
            parse_eval_case(_case_dict(
                case_id="synthetic_case_0002",
                source={
                    "path": "data/eval/cases/other/capture.json", "sha256": _SHA_B,
                    "captured_at": "2030-01-02T10:00:00+03:00",
                },
            )),
        )
        violations = find_leakage_violations(cases, _registry())
        self.assertTrue(any(v.reason == "duplicate_leakage_group" for v in violations))


class DevRegistryParsingTests(unittest.TestCase):
    def test_unsupported_schema_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^unsupported_registry_schema$"):
            parse_dev_registry({"schema": "wrong", "items": []})

    def test_empty_registry_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^empty_registry$"):
            parse_dev_registry({"schema": "fintr_dev_registry_v1", "items": []})

    def test_duplicate_item_id_rejected(self):
        item = {
            "item_id": "dup", "symbol": "ZZTEST", "start": "2030-01-01", "end": "2030-01-02",
            "source_path": "data/interim/pilot/x.json", "classification": "synthetic",
        }
        with self.assertRaisesRegex(ManifestError, "^duplicate_registry_item_id$"):
            parse_dev_registry({"schema": "fintr_dev_registry_v1", "items": [item, dict(item)]})

    def test_invalid_period_order_rejected(self):
        item = {
            "item_id": "bad", "symbol": "ZZTEST", "start": "2030-01-10", "end": "2030-01-01",
            "source_path": "data/interim/pilot/x.json", "classification": "synthetic",
        }
        with self.assertRaisesRegex(ManifestError, "^invalid_registry_period_order$"):
            parse_dev_registry({"schema": "fintr_dev_registry_v1", "items": [item]})


class BundledDevRegistryTests(unittest.TestCase):
    """The shipped registry must reject AKFIS at the company level, any date."""

    def test_bundled_registry_rejects_akfis_pilot_date(self):
        registry = load_dev_registry()
        cases = (parse_eval_case(_case_dict(
            symbol="AKFIS",
            observation_period={"start": "2026-09-23", "end": "2026-09-23"},
            leakage_group="AKFIS|2026-09-23|2026-09-23",
        )),)
        violations = find_leakage_violations(cases, registry)
        self.assertTrue(any(v.reason == "development_symbol" for v in violations))

    def test_bundled_registry_rejects_akfis_period_capture_range(self):
        registry = load_dev_registry()
        cases = (parse_eval_case(_case_dict(
            symbol="AKFIS",
            observation_period={"start": "2026-09-10", "end": "2026-09-12"},
            leakage_group="AKFIS|2026-09-10|2026-09-12",
        )),)
        violations = find_leakage_violations(cases, registry)
        self.assertTrue(any(v.reason == "development_symbol" for v in violations))

    def test_bundled_registry_rejects_akfis_even_far_from_any_registered_date(self):
        # Company-level rule: no date, however distant, makes AKFIS safe.
        registry = load_dev_registry()
        cases = (parse_eval_case(_case_dict(
            symbol="AKFIS",
            observation_period={"start": "2099-01-01", "end": "2099-01-02"},
            leakage_group="AKFIS|2099-01-01|2099-01-02",
        )),)
        violations = find_leakage_violations(cases, registry)
        self.assertTrue(any(v.reason == "development_symbol" for v in violations))

    def test_bundled_registry_allows_unrelated_symbol(self):
        registry = load_dev_registry()
        cases = (parse_eval_case(_case_dict()),)
        violations = find_leakage_violations(cases, registry)
        self.assertEqual([v for v in violations if v.reason == "development_symbol"], [])


if __name__ == "__main__":
    unittest.main()
