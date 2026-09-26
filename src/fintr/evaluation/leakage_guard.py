"""Reject eval cases that could leak development-only material or duplicate coverage.

The registry is a small, versioned JSON file (``dev_registry.json``) listing
symbols (plus a documented date range and source path, kept for lineage) of
material known to have been used for prompt or output development. It never
carries financial values.

The symbol check is company-level, not date-level: any eval case for a
symbol that appears in the registry is rejected outright, regardless of its
observation period. Custody/settlement data reconciles on a T+2 basis and
spans weekends, so a same-symbol eval case a few calendar days after a
registered development period can still carry information from that
development period; per AGENTS.md's split rules (no leakage across document
family, company, report period, or event), the whole company is excluded
once any of its data has been used for development. The registry's own
``start``/``end`` fields are documentation of what was used, not a boundary
this guard reasons about.

All checks here are structural and return fixed, payload-free reasons;
nothing about a rejected case's content is echoed back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
import json

from fintr.data.parsers.common import (
    MarketDataParseError,
    require_date,
    require_field,
    require_list,
    require_mapping,
    require_string,
)
from fintr.evaluation.manifest import EvalCase, ManifestError
from fintr.evaluation.symbol_exclusions import SymbolExclusions, is_excluded
from fintr.evaluation.symbol_split import SymbolSplit, is_holdout


REGISTRY_SCHEMA_VERSION = "fintr_dev_registry_v1"
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "dev_registry.json"

# Known development-only capture directories; an eval case sourced from
# either can never be accepted regardless of what its dates claim.
DEVELOPMENT_ONLY_PATH_PREFIXES = ("data/interim/pilot/", "data/interim/period_analysis/")


@dataclass(frozen=True, slots=True)
class DevRegistryEntry:
    item_id: str
    symbol: str
    start: date
    end: date
    source_path: str
    classification: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id:
            raise ManifestError("invalid_registry_item_id")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ManifestError("invalid_registry_symbol")
        if not isinstance(self.start, date) or not isinstance(self.end, date):
            raise ManifestError("invalid_registry_period_type")
        if self.end < self.start:
            raise ManifestError("invalid_registry_period_order")
        if not isinstance(self.source_path, str) or not self.source_path:
            raise ManifestError("invalid_registry_source_path")
        if not isinstance(self.classification, str) or not self.classification:
            raise ManifestError("invalid_registry_classification")


@dataclass(frozen=True, slots=True)
class LeakageViolation:
    case_id: str
    reason: str


class LeakageError(ValueError):
    """A fixed, payload-free reason an eval case cannot be accepted."""


def _normalized_path(path: str) -> str:
    return path.replace("\\", "/")


def parse_dev_registry(document: Any) -> tuple[DevRegistryEntry, ...]:
    try:
        root = require_mapping(document, "registry")
        if root.get("schema") != REGISTRY_SCHEMA_VERSION:
            raise ManifestError("unsupported_registry_schema")
        raw_items = require_list(require_field(root, "items", "registry"), "registry.items")
    except MarketDataParseError as exc:
        raise ManifestError(f"invalid_registry_field: {exc.field_path or 'unknown'}") from None
    if not raw_items:
        raise ManifestError("empty_registry")

    entries: list[DevRegistryEntry] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_items):
        item_path = f"registry.items[{index}]"
        try:
            item = require_mapping(raw, item_path)
            item_id = require_string(require_field(item, "item_id", item_path), f"{item_path}.item_id")
            entry = DevRegistryEntry(
                item_id=item_id,
                symbol=require_string(
                    require_field(item, "symbol", item_path), f"{item_path}.symbol"
                ),
                start=require_date(require_field(item, "start", item_path), f"{item_path}.start"),
                end=require_date(require_field(item, "end", item_path), f"{item_path}.end"),
                source_path=_normalized_path(require_string(
                    require_field(item, "source_path", item_path), f"{item_path}.source_path",
                )),
                classification=require_string(
                    require_field(item, "classification", item_path),
                    f"{item_path}.classification",
                ),
            )
        except MarketDataParseError as exc:
            raise ManifestError(f"invalid_registry_field: {exc.field_path or 'unknown'}") from None
        if item_id in seen_ids:
            raise ManifestError("duplicate_registry_item_id")
        seen_ids.add(item_id)
        entries.append(entry)
    return tuple(entries)


def load_dev_registry(path: Path = DEFAULT_REGISTRY_PATH) -> tuple[DevRegistryEntry, ...]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ManifestError("unreadable_registry") from None
    return parse_dev_registry(document)


def find_leakage_violations(
    cases: tuple[EvalCase, ...], registry: tuple[DevRegistryEntry, ...],
    *, split: SymbolSplit | None = None, exclusions: SymbolExclusions | None = None,
) -> tuple[LeakageViolation, ...]:
    """Return every violation found; does not stop at the first one.

    Checked, in order per case: development-only source path, development
    registry symbol match (company-level, regardless of date), duplicate
    case_id, duplicate capture checksum, duplicate leakage group, and (only
    when the corresponding argument is supplied -- both are optional and
    off by default) a frozen BIST30 holdout-split violation
    (``not_in_holdout_split``, from ``scripts/validate_eval_manifest.py
    --split-file``) or a symbol-exclusions violation (``excluded_symbol``,
    unconditional, no override flag).
    """
    if not isinstance(cases, tuple) or any(not isinstance(case, EvalCase) for case in cases):
        raise ManifestError("invalid_cases")
    if not isinstance(registry, tuple) or any(
        not isinstance(entry, DevRegistryEntry) for entry in registry
    ):
        raise ManifestError("invalid_registry")
    if split is not None and not isinstance(split, SymbolSplit):
        raise ManifestError("invalid_split")
    if exclusions is not None and not isinstance(exclusions, SymbolExclusions):
        raise ManifestError("invalid_exclusions")

    violations: list[LeakageViolation] = []
    seen_case_ids: set[str] = set()
    seen_checksums: set[str] = set()
    seen_leakage_groups: set[str] = set()

    for case in cases:
        normalized_source = _normalized_path(case.source.path)
        if any(normalized_source.startswith(prefix) for prefix in DEVELOPMENT_ONLY_PATH_PREFIXES):
            violations.append(LeakageViolation(case.case_id, "development_material_path"))

        if any(entry.symbol.casefold() == case.symbol.casefold() for entry in registry):
            violations.append(LeakageViolation(case.case_id, "development_symbol"))

        if case.case_id in seen_case_ids:
            violations.append(LeakageViolation(case.case_id, "duplicate_case_id"))
        seen_case_ids.add(case.case_id)

        if case.source.sha256 in seen_checksums:
            violations.append(LeakageViolation(case.case_id, "duplicate_capture_checksum"))
        seen_checksums.add(case.source.sha256)

        if case.leakage_group in seen_leakage_groups:
            violations.append(LeakageViolation(case.case_id, "duplicate_leakage_group"))
        seen_leakage_groups.add(case.leakage_group)

        if split is not None and not is_holdout(split, case.symbol):
            violations.append(LeakageViolation(case.case_id, "not_in_holdout_split"))

        if exclusions is not None and is_excluded(exclusions, case.symbol):
            violations.append(LeakageViolation(case.case_id, "excluded_symbol"))

    return tuple(violations)


def assert_no_leakage(
    cases: tuple[EvalCase, ...], registry: tuple[DevRegistryEntry, ...],
    *, split: SymbolSplit | None = None, exclusions: SymbolExclusions | None = None,
) -> None:
    """Raise on the first violation found; callers wanting a full report use
    ``find_leakage_violations`` instead."""
    violations = find_leakage_violations(cases, registry, split=split, exclusions=exclusions)
    if violations:
        raise LeakageError(violations[0].reason)
