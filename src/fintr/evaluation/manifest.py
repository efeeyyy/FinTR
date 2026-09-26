"""Versioned eval case manifest and annotation record formats.

This module defines and strictly parses two JSON formats:

* ``fintr_eval_manifest_v1`` — one eval case's provenance (symbol, data
  families, observation period, horizons, source capture + checksum,
  capture time, permission provenance, leakage group key, notes). It never
  carries a gold judgment or a model answer.
* ``fintr_eval_annotation_v1`` — one human annotator's frozen judgment for
  one case, per rubric dimension, with second-review/disagreement fields and
  a freeze checksum.

Nothing here produces gold labels, scores, or verifies a permission claim
independently — ``permission.independently_verified`` must be ``False``
because this tooling has no way to confirm it. Real eval data lives under
the git-ignored ``data/eval/`` tree; only synthetic examples belong under
``eval/cases/`` or ``tests/``. See ``docs/evaluation_protocol.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import hashlib
import json
import re
from typing import Any

from fintr.data.parsers.common import (
    MarketDataParseError,
    require_date,
    require_field,
    require_list,
    require_mapping,
    require_string,
)


MANIFEST_SCHEMA_VERSION = "fintr_eval_manifest_v1"
ANNOTATION_SCHEMA_VERSION = "fintr_eval_annotation_v1"
REQUIRED_SPLIT = "eval"

ALLOWED_DATA_FAMILIES = ("depth", "trade", "akd", "custody", "stats", "price_volume")
ALLOWED_HORIZONS = ("intraday", "1d", "7d", "30d", "90d")

# One entry per rubric section in eval/rubrics/market_microstructure_v0.md.
RUBRIC_DIMENSIONS = (
    "numerical_faithfulness",
    "akd_custody_distinction",
    "multi_horizon_reasoning",
    "contradiction_detection",
    "missing_data_detection",
    "unnecessary_data_request",
    "causal_overclaim",
    "hallucination",
    "uncertainty_calibration",
    "unsupported_price_prediction",
)

_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """A fixed, payload-free reason a manifest or annotation record is invalid."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def leakage_group_key(symbol: str, start: date, end: date) -> str:
    """The leakage group for one symbol+observation-period; recomputed, not trusted."""
    return f"{symbol}|{start.isoformat()}|{end.isoformat()}"


def _require_tz_datetime(value: Any, path: str) -> datetime:
    text = require_string(value, path)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ManifestError(f"invalid_datetime: {path}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestError(f"datetime_requires_timezone: {path}")
    return parsed


def _require_tuple_subset(
    values: Any, allowed: tuple[str, ...], path: str,
) -> tuple[str, ...]:
    raw_items = require_list(values, path)
    if not raw_items:
        raise ManifestError(f"empty_list: {path}")
    items = tuple(require_string(item, f"{path}[{index}]") for index, item in enumerate(raw_items))
    if len(set(items)) != len(items):
        raise ManifestError(f"duplicate_entry: {path}")
    if any(item not in allowed for item in items):
        raise ManifestError(f"invalid_entry: {path}")
    return items


def _require_string_tuple(values: Any, path: str) -> tuple[str, ...]:
    raw_items = require_list(values, path)
    return tuple(require_string(item, f"{path}[{index}]") for index, item in enumerate(raw_items))


@dataclass(frozen=True, slots=True)
class ObservationPeriod:
    start: date
    end: date

    def __post_init__(self) -> None:
        if not isinstance(self.start, date) or not isinstance(self.end, date):
            raise ManifestError("invalid_period_type")
        if self.end < self.start:
            raise ManifestError("invalid_period_order")


@dataclass(frozen=True, slots=True)
class SourceCapture:
    """Provenance of the capture backing one case; never the financial payload itself."""

    path: str
    sha256: str
    captured_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ManifestError("invalid_source_path")
        if not isinstance(self.sha256, str) or not _SHA256_RE.match(self.sha256):
            raise ManifestError("invalid_source_sha256")
        if not isinstance(self.captured_at, datetime) or (
            self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None
        ):
            raise ManifestError("capture_time_requires_timezone")


@dataclass(frozen=True, slots=True)
class PermissionProvenance:
    """Always user-asserted; this tooling cannot and does not verify licenses."""

    basis: str
    asserted_on: date
    independently_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.basis, str) or not self.basis:
            raise ManifestError("invalid_permission_basis")
        if not isinstance(self.asserted_on, date):
            raise ManifestError("invalid_permission_asserted_on")
        if type(self.independently_verified) is not bool:
            raise ManifestError("invalid_permission_independently_verified")
        if self.independently_verified:
            raise ManifestError("independent_verification_not_supported")


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    split: str
    symbol: str
    data_families: tuple[str, ...]
    period: ObservationPeriod
    horizons: tuple[str, ...]
    source: SourceCapture
    permission: PermissionProvenance
    leakage_group: str
    notes: str

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id or any(
            char.isspace() for char in self.case_id
        ):
            raise ManifestError("invalid_case_id")
        if self.split != REQUIRED_SPLIT:
            raise ManifestError("invalid_split")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ManifestError("invalid_symbol")
        if self.symbol != self.symbol.upper():
            raise ManifestError("symbol_not_uppercase")
        if not isinstance(self.data_families, tuple) or len(set(self.data_families)) != len(
            self.data_families
        ) or any(family not in ALLOWED_DATA_FAMILIES for family in self.data_families) or not self.data_families:
            raise ManifestError("invalid_data_families")
        if not isinstance(self.period, ObservationPeriod):
            raise ManifestError("invalid_period")
        if not isinstance(self.horizons, tuple) or len(set(self.horizons)) != len(
            self.horizons
        ) or any(horizon not in ALLOWED_HORIZONS for horizon in self.horizons) or not self.horizons:
            raise ManifestError("invalid_horizons")
        if not isinstance(self.source, SourceCapture):
            raise ManifestError("invalid_source")
        if not isinstance(self.permission, PermissionProvenance):
            raise ManifestError("invalid_permission")
        expected_group = leakage_group_key(self.symbol, self.period.start, self.period.end)
        if self.leakage_group != expected_group:
            raise ManifestError("leakage_group_mismatch")
        if not isinstance(self.notes, str) or not self.notes:
            raise ManifestError("invalid_notes")


def parse_eval_case(raw: Any, path: str = "case") -> EvalCase:
    try:
        case = require_mapping(raw, path)
        period_raw = require_mapping(
            require_field(case, "observation_period", path), f"{path}.observation_period"
        )
        source_raw = require_mapping(require_field(case, "source", path), f"{path}.source")
        permission_raw = require_mapping(
            require_field(case, "permission", path), f"{path}.permission"
        )
        return EvalCase(
            case_id=require_string(require_field(case, "case_id", path), f"{path}.case_id"),
            split=require_string(require_field(case, "split", path), f"{path}.split"),
            symbol=require_string(require_field(case, "symbol", path), f"{path}.symbol"),
            data_families=_require_tuple_subset(
                require_field(case, "data_families", path), ALLOWED_DATA_FAMILIES,
                f"{path}.data_families",
            ),
            period=ObservationPeriod(
                start=require_date(
                    require_field(period_raw, "start", f"{path}.observation_period"),
                    f"{path}.observation_period.start",
                ),
                end=require_date(
                    require_field(period_raw, "end", f"{path}.observation_period"),
                    f"{path}.observation_period.end",
                ),
            ),
            horizons=_require_tuple_subset(
                require_field(case, "horizons", path), ALLOWED_HORIZONS, f"{path}.horizons"
            ),
            source=SourceCapture(
                path=require_string(
                    require_field(source_raw, "path", f"{path}.source"), f"{path}.source.path"
                ),
                sha256=require_string(
                    require_field(source_raw, "sha256", f"{path}.source"), f"{path}.source.sha256"
                ),
                captured_at=_require_tz_datetime(
                    require_field(source_raw, "captured_at", f"{path}.source"),
                    f"{path}.source.captured_at",
                ),
            ),
            permission=PermissionProvenance(
                basis=require_string(
                    require_field(permission_raw, "basis", f"{path}.permission"),
                    f"{path}.permission.basis",
                ),
                asserted_on=require_date(
                    require_field(permission_raw, "asserted_on", f"{path}.permission"),
                    f"{path}.permission.asserted_on",
                ),
                independently_verified=require_field(
                    permission_raw, "independently_verified", f"{path}.permission"
                ),
            ),
            leakage_group=require_string(
                require_field(case, "leakage_group", path), f"{path}.leakage_group"
            ),
            notes=require_string(require_field(case, "notes", path), f"{path}.notes"),
        )
    except MarketDataParseError as exc:
        raise ManifestError(f"invalid_case_field: {exc.field_path or 'unknown'}") from None


def parse_manifest(document: Any) -> tuple[EvalCase, ...]:
    """Parse a manifest file's decoded JSON into cases; no cross-case checks here.

    Cross-case checks (duplicate ids/checksums/leakage groups, development
    registry symbol match) belong to ``fintr.evaluation.leakage_guard``.
    """
    try:
        root = require_mapping(document, "manifest")
        if root.get("schema") != MANIFEST_SCHEMA_VERSION:
            raise ManifestError("unsupported_manifest_schema")
        raw_cases = require_list(require_field(root, "cases", "manifest"), "manifest.cases")
    except MarketDataParseError as exc:
        raise ManifestError(f"invalid_manifest_field: {exc.field_path or 'unknown'}") from None
    if not raw_cases:
        raise ManifestError("empty_manifest")
    return tuple(
        parse_eval_case(raw, f"manifest.cases[{index}]") for index, raw in enumerate(raw_cases)
    )


# --- Annotation records ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DimensionJudgment:
    """Gold judgment for one rubric dimension; N/A dimensions carry no content."""

    dimension: str
    applicable: bool
    gold_facts: tuple[str, ...]
    allowed_calculations: tuple[str, ...]
    critical_errors: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.dimension not in RUBRIC_DIMENSIONS:
            raise ManifestError("invalid_dimension")
        if type(self.applicable) is not bool:
            raise ManifestError("invalid_dimension_applicable")
        for field_name, value in (
            ("gold_facts", self.gold_facts),
            ("allowed_calculations", self.allowed_calculations),
            ("critical_errors", self.critical_errors),
        ):
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise ManifestError(f"invalid_dimension_{field_name}")
        if not self.applicable and (
            self.gold_facts or self.allowed_calculations or self.critical_errors
        ):
            raise ManifestError("not_applicable_dimension_has_content")
        if self.applicable and not self.gold_facts:
            raise ManifestError("applicable_dimension_missing_gold_facts")


@dataclass(frozen=True, slots=True)
class DisagreementResolution:
    reviewer_id: str
    reviewed_at: datetime
    agreement: bool
    disagreement_notes: str | None
    resolution: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.reviewer_id, str) or not _OPAQUE_ID_RE.match(self.reviewer_id):
            raise ManifestError("invalid_reviewer_id")
        if not isinstance(self.reviewed_at, datetime) or (
            self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None
        ):
            raise ManifestError("review_time_requires_timezone")
        if type(self.agreement) is not bool:
            raise ManifestError("invalid_agreement")
        if self.agreement:
            if self.disagreement_notes is not None or self.resolution is not None:
                raise ManifestError("agreement_has_disagreement_fields")
        else:
            if not isinstance(self.disagreement_notes, str) or not self.disagreement_notes:
                raise ManifestError("disagreement_missing_notes")
            if not isinstance(self.resolution, str) or not self.resolution:
                raise ManifestError("disagreement_missing_resolution")


@dataclass(frozen=True, slots=True)
class AnnotationRecord:
    case_id: str
    annotator_id: str
    created_at: datetime
    rubric_version: str
    dimensions: tuple[DimensionJudgment, ...]
    second_review: DisagreementResolution | None
    frozen: bool
    checksum: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id or any(
            char.isspace() for char in self.case_id
        ):
            raise ManifestError("invalid_case_id")
        if not isinstance(self.annotator_id, str) or not _OPAQUE_ID_RE.match(self.annotator_id):
            raise ManifestError("invalid_annotator_id")
        if not isinstance(self.created_at, datetime) or (
            self.created_at.tzinfo is None or self.created_at.utcoffset() is None
        ):
            raise ManifestError("created_at_requires_timezone")
        if self.rubric_version != "market_microstructure_v0":
            raise ManifestError("unsupported_rubric_version")
        if not isinstance(self.dimensions, tuple) or any(
            not isinstance(item, DimensionJudgment) for item in self.dimensions
        ):
            raise ManifestError("invalid_dimensions")
        names = tuple(item.dimension for item in self.dimensions)
        if set(names) != set(RUBRIC_DIMENSIONS) or len(names) != len(RUBRIC_DIMENSIONS):
            raise ManifestError("incomplete_rubric_dimensions")
        if self.second_review is not None and not isinstance(
            self.second_review, DisagreementResolution
        ):
            raise ManifestError("invalid_second_review")
        if type(self.frozen) is not bool:
            raise ManifestError("invalid_frozen")
        if self.frozen:
            if not isinstance(self.checksum, str) or not _SHA256_RE.match(self.checksum):
                raise ManifestError("frozen_requires_checksum")
            if self.checksum != compute_annotation_checksum(self):
                raise ManifestError("checksum_mismatch")
        else:
            if self.checksum is not None:
                raise ManifestError("checksum_without_freeze")


def _dimension_signature(item: DimensionJudgment) -> dict[str, Any]:
    return {
        "dimension": item.dimension,
        "applicable": item.applicable,
        "gold_facts": list(item.gold_facts),
        "allowed_calculations": list(item.allowed_calculations),
        "critical_errors": list(item.critical_errors),
    }


def _second_review_signature(review: DisagreementResolution | None) -> dict[str, Any] | None:
    if review is None:
        return None
    return {
        "reviewer_id": review.reviewer_id,
        "reviewed_at": review.reviewed_at.isoformat(),
        "agreement": review.agreement,
        "disagreement_notes": review.disagreement_notes,
        "resolution": review.resolution,
    }


def _annotation_signature(record: AnnotationRecord) -> dict[str, Any]:
    """Content the freeze checksum binds to; excludes ``frozen``/``checksum`` themselves."""
    return {
        "case_id": record.case_id,
        "annotator_id": record.annotator_id,
        "created_at": record.created_at.isoformat(),
        "rubric_version": record.rubric_version,
        "dimensions": [
            _dimension_signature(item)
            for item in sorted(record.dimensions, key=lambda item: item.dimension)
        ],
        "second_review": _second_review_signature(record.second_review),
    }


def compute_annotation_checksum(record: AnnotationRecord) -> str:
    """SHA-256 over the canonical, checksum-excluded content of ``record``."""
    return hashlib.sha256(_canonical_json(_annotation_signature(record))).hexdigest()


def freeze_annotation(record: AnnotationRecord) -> AnnotationRecord:
    """Return ``record`` with ``frozen=True`` and a matching checksum set."""
    unfrozen = replace(record, frozen=False, checksum=None) if record.frozen else record
    checksum = compute_annotation_checksum(unfrozen)
    return replace(unfrozen, frozen=True, checksum=checksum)


def _parse_dimension(raw: Any, path: str) -> DimensionJudgment:
    entry = require_mapping(raw, path)
    return DimensionJudgment(
        dimension=require_string(require_field(entry, "dimension", path), f"{path}.dimension"),
        applicable=require_field(entry, "applicable", path),
        gold_facts=_require_string_tuple(entry.get("gold_facts", []), f"{path}.gold_facts"),
        allowed_calculations=_require_string_tuple(
            entry.get("allowed_calculations", []), f"{path}.allowed_calculations"
        ),
        critical_errors=_require_string_tuple(
            entry.get("critical_errors", []), f"{path}.critical_errors"
        ),
    )


def _parse_second_review(raw: Any, path: str) -> DisagreementResolution | None:
    if raw is None:
        return None
    entry = require_mapping(raw, path)
    return DisagreementResolution(
        reviewer_id=require_string(
            require_field(entry, "reviewer_id", path), f"{path}.reviewer_id"
        ),
        reviewed_at=_require_tz_datetime(
            require_field(entry, "reviewed_at", path), f"{path}.reviewed_at"
        ),
        agreement=require_field(entry, "agreement", path),
        disagreement_notes=entry.get("disagreement_notes"),
        resolution=entry.get("resolution"),
    )


def parse_annotation(document: Any, path: str = "annotation") -> AnnotationRecord:
    try:
        root = require_mapping(document, path)
        if root.get("schema") != ANNOTATION_SCHEMA_VERSION:
            raise ManifestError("unsupported_annotation_schema")
        raw_dimensions = require_list(
            require_field(root, "dimensions", path), f"{path}.dimensions"
        )
        dimensions = tuple(
            _parse_dimension(raw, f"{path}.dimensions[{index}]")
            for index, raw in enumerate(raw_dimensions)
        )
        return AnnotationRecord(
            case_id=require_string(require_field(root, "case_id", path), f"{path}.case_id"),
            annotator_id=require_string(
                require_field(root, "annotator_id", path), f"{path}.annotator_id"
            ),
            created_at=_require_tz_datetime(
                require_field(root, "created_at", path), f"{path}.created_at"
            ),
            rubric_version=require_string(
                require_field(root, "rubric_version", path), f"{path}.rubric_version"
            ),
            dimensions=dimensions,
            second_review=_parse_second_review(root.get("second_review"), f"{path}.second_review"),
            frozen=require_field(root, "frozen", path),
            checksum=root.get("checksum"),
        )
    except MarketDataParseError as exc:
        raise ManifestError(f"invalid_annotation_field: {exc.field_path or 'unknown'}") from None
