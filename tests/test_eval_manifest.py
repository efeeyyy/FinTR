"""Synthetic-data-only checks for the eval manifest and annotation formats."""

from __future__ import annotations

import hashlib
import unittest

from fintr.evaluation.manifest import (
    ManifestError,
    RUBRIC_DIMENSIONS,
    compute_annotation_checksum,
    freeze_annotation,
    leakage_group_key,
    parse_annotation,
    parse_eval_case,
    parse_manifest,
)


_SOURCE_SHA256 = hashlib.sha256(b"synthetic-test-capture").hexdigest()


def _case_dict(**overrides) -> dict:
    base = {
        "case_id": "synthetic_case_0001",
        "split": "eval",
        "symbol": "ZZTEST",
        "data_families": ["depth", "akd"],
        "observation_period": {"start": "2030-01-01", "end": "2030-01-02"},
        "horizons": ["1d", "7d"],
        "source": {
            "path": "data/eval/cases/synthetic_case_0001/capture.json",
            "sha256": _SOURCE_SHA256,
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


def _manifest_doc(*cases) -> dict:
    return {"schema": "fintr_eval_manifest_v1", "cases": list(cases)}


def _dimension_dict(name: str, applicable: bool = True) -> dict:
    if not applicable:
        return {
            "dimension": name, "applicable": False,
            "gold_facts": [], "allowed_calculations": [], "critical_errors": [],
        }
    return {
        "dimension": name, "applicable": True,
        "gold_facts": ["sentetik gold fact"],
        "allowed_calculations": ["sentetik allowed calc"],
        "critical_errors": [],
    }


def _annotation_dict(**overrides) -> dict:
    base = {
        "schema": "fintr_eval_annotation_v1",
        "case_id": "synthetic_case_0001",
        "annotator_id": "ann_synthetic_1",
        "created_at": "2030-01-03T09:00:00+03:00",
        "rubric_version": "market_microstructure_v0",
        "dimensions": [_dimension_dict(name) for name in RUBRIC_DIMENSIONS],
        "second_review": None,
        "frozen": False,
        "checksum": None,
    }
    base.update(overrides)
    return base


class EvalManifestParsingTests(unittest.TestCase):
    def test_valid_case_parses_and_round_trips_leakage_group(self):
        case = parse_eval_case(_case_dict())
        self.assertEqual(case.case_id, "synthetic_case_0001")
        self.assertEqual(case.symbol, "ZZTEST")
        self.assertEqual(
            case.leakage_group,
            leakage_group_key(case.symbol, case.period.start, case.period.end),
        )

    def test_valid_manifest_parses_one_case(self):
        cases = parse_manifest(_manifest_doc(_case_dict()))
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].case_id, "synthetic_case_0001")

    def test_empty_manifest_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^empty_manifest$"):
            parse_manifest(_manifest_doc())

    def test_unsupported_manifest_schema_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^unsupported_manifest_schema$"):
            parse_manifest({"schema": "wrong", "cases": [_case_dict()]})

    def test_split_must_be_eval(self):
        with self.assertRaisesRegex(ManifestError, "^invalid_split$"):
            parse_eval_case(_case_dict(split="train"))

    def test_symbol_must_be_uppercase(self):
        with self.assertRaisesRegex(ManifestError, "^symbol_not_uppercase$"):
            parse_eval_case(_case_dict(symbol="zztest"))

    def test_unknown_data_family_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^invalid_entry"):
            parse_eval_case(_case_dict(data_families=["depth", "not_a_family"]))

    def test_empty_data_families_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^empty_list"):
            parse_eval_case(_case_dict(data_families=[]))

    def test_duplicate_horizon_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^duplicate_entry"):
            parse_eval_case(_case_dict(horizons=["1d", "1d"]))

    def test_invalid_sha256_rejected(self):
        case = _case_dict()
        case["source"] = dict(case["source"], sha256="not-a-checksum")
        with self.assertRaisesRegex(ManifestError, "^invalid_source_sha256$"):
            parse_eval_case(case)

    def test_capture_time_requires_timezone(self):
        case = _case_dict()
        case["source"] = dict(case["source"], captured_at="2030-01-02T10:00:00")
        with self.assertRaisesRegex(ManifestError, "datetime_requires_timezone"):
            parse_eval_case(case)

    def test_independent_verification_claim_rejected(self):
        case = _case_dict()
        case["permission"] = dict(case["permission"], independently_verified=True)
        with self.assertRaisesRegex(ManifestError, "^independent_verification_not_supported$"):
            parse_eval_case(case)

    def test_leakage_group_mismatch_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^leakage_group_mismatch$"):
            parse_eval_case(_case_dict(leakage_group="WRONG|2030-01-01|2030-01-02"))

    def test_period_end_before_start_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^invalid_period_order$"):
            parse_eval_case(_case_dict(
                observation_period={"start": "2030-01-05", "end": "2030-01-01"},
            ))

    def test_missing_field_rejected(self):
        case = _case_dict()
        del case["notes"]
        with self.assertRaisesRegex(ManifestError, "^invalid_case_field"):
            parse_eval_case(case)

    def test_wrong_type_for_case_id_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^invalid_case_field"):
            parse_eval_case(_case_dict(case_id=12345))


class EvalAnnotationParsingTests(unittest.TestCase):
    def test_valid_unfrozen_annotation_parses(self):
        record = parse_annotation(_annotation_dict())
        self.assertFalse(record.frozen)
        self.assertIsNone(record.checksum)
        self.assertEqual(len(record.dimensions), len(RUBRIC_DIMENSIONS))

    def test_incomplete_rubric_dimensions_rejected(self):
        annotation = _annotation_dict(
            dimensions=[_dimension_dict(name) for name in RUBRIC_DIMENSIONS[:-1]]
        )
        with self.assertRaisesRegex(ManifestError, "^incomplete_rubric_dimensions$"):
            parse_annotation(annotation)

    def test_unsupported_rubric_version_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^unsupported_rubric_version$"):
            parse_annotation(_annotation_dict(rubric_version="other_v0"))

    def test_not_applicable_dimension_with_content_rejected(self):
        dims = [_dimension_dict(name) for name in RUBRIC_DIMENSIONS]
        dims[0] = {
            "dimension": RUBRIC_DIMENSIONS[0], "applicable": False,
            "gold_facts": ["should not be here"], "allowed_calculations": [], "critical_errors": [],
        }
        with self.assertRaisesRegex(ManifestError, "^not_applicable_dimension_has_content$"):
            parse_annotation(_annotation_dict(dimensions=dims))

    def test_applicable_dimension_without_gold_facts_rejected(self):
        dims = [_dimension_dict(name) for name in RUBRIC_DIMENSIONS]
        dims[0] = {
            "dimension": RUBRIC_DIMENSIONS[0], "applicable": True,
            "gold_facts": [], "allowed_calculations": [], "critical_errors": [],
        }
        with self.assertRaisesRegex(ManifestError, "^applicable_dimension_missing_gold_facts$"):
            parse_annotation(_annotation_dict(dimensions=dims))

    def test_disagreement_requires_notes_and_resolution(self):
        annotation = _annotation_dict(second_review={
            "reviewer_id": "ann_synthetic_2",
            "reviewed_at": "2030-01-04T09:00:00+03:00",
            "agreement": False,
            "disagreement_notes": None,
            "resolution": None,
        })
        with self.assertRaisesRegex(ManifestError, "^disagreement_missing_notes$"):
            parse_annotation(annotation)

    def test_agreement_forbids_disagreement_fields(self):
        annotation = _annotation_dict(second_review={
            "reviewer_id": "ann_synthetic_2",
            "reviewed_at": "2030-01-04T09:00:00+03:00",
            "agreement": True,
            "disagreement_notes": "should not be set",
            "resolution": None,
        })
        with self.assertRaisesRegex(ManifestError, "^agreement_has_disagreement_fields$"):
            parse_annotation(annotation)

    def test_frozen_without_checksum_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^frozen_requires_checksum$"):
            parse_annotation(_annotation_dict(frozen=True, checksum=None))

    def test_checksum_without_freeze_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^checksum_without_freeze$"):
            parse_annotation(_annotation_dict(frozen=False, checksum="a" * 64))

    def test_wrong_checksum_rejected(self):
        with self.assertRaisesRegex(ManifestError, "^checksum_mismatch$"):
            parse_annotation(_annotation_dict(frozen=True, checksum="0" * 64))

    def test_freeze_annotation_produces_verifiable_checksum(self):
        record = parse_annotation(_annotation_dict())
        frozen = freeze_annotation(record)
        self.assertTrue(frozen.frozen)
        self.assertIsNotNone(frozen.checksum)
        # The checksum is independent of frozen/checksum themselves.
        self.assertEqual(frozen.checksum, compute_annotation_checksum(record))
        # Round-trip through parsing must accept the frozen record as-is.
        reparsed = parse_annotation({
            "schema": "fintr_eval_annotation_v1",
            "case_id": frozen.case_id,
            "annotator_id": frozen.annotator_id,
            "created_at": frozen.created_at.isoformat(),
            "rubric_version": frozen.rubric_version,
            "dimensions": [_dimension_dict(item.dimension, item.applicable) for item in frozen.dimensions],
            "second_review": None,
            "frozen": True,
            "checksum": frozen.checksum,
        })
        self.assertEqual(reparsed.checksum, frozen.checksum)

    def test_editing_frozen_content_invalidates_checksum(self):
        record = parse_annotation(_annotation_dict())
        frozen = freeze_annotation(record)
        tampered = _annotation_dict(
            annotator_id="ann_someone_else", frozen=True, checksum=frozen.checksum,
        )
        with self.assertRaisesRegex(ManifestError, "^checksum_mismatch$"):
            parse_annotation(tampered)


if __name__ == "__main__":
    unittest.main()
