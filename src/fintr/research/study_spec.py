"""``fintr_study_spec_v1``: a pre-registered, versioned event-study condition.

A study spec declares, before any test-period result is looked at, exactly
what is being tested: which symbols, the train/test time cutoff, an optional
symbol hold-out list, the entry/horizon/adjustment-boundary rules, the
bootstrap seed, and the condition itself (see ``fintr.research.conditions``
for its closed grammar -- no free text, no model call). ``frozen`` is the
pre-registration gate: ``fintr.research.engine`` refuses to report
test-period results for a spec whose ``frozen`` is ``false``. The spec's
SHA-256 (over the exact decoded document, see ``spec_sha256``) is recorded
in every result so a result can be tied back to the exact spec that produced
it, even if the spec file is later edited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any

from fintr.data.parsers.common import (
    MarketDataParseError,
    require_date,
    require_field,
    require_list,
    require_mapping,
    require_string,
)
from fintr.data.period_capture import canonical_json
from fintr.research.conditions import ConditionNode, parse_condition


SPEC_SCHEMA = "fintr_study_spec_v1"

ALLOWED_ENTRY_TIMINGS = ("next_open", "next_close")
ALLOWED_ADJUSTMENT_POLICIES = ("use_adjusted", "drop")


class StudySpecError(ValueError):
    """A study spec document failed structural or semantic validation."""


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    seed: int
    resamples: int
    confidence_level: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise StudySpecError("invalid_bootstrap_seed")
        if isinstance(self.resamples, bool) or not isinstance(self.resamples, int) or self.resamples < 1:
            raise StudySpecError("invalid_bootstrap_resamples")
        if not isinstance(self.confidence_level, Decimal) or not (
            Decimal(0) < self.confidence_level < Decimal(1)
        ):
            raise StudySpecError("invalid_bootstrap_confidence_level")


@dataclass(frozen=True, slots=True)
class StudySpec:
    spec_id: str
    description: str
    frozen: bool
    symbols: tuple[str, ...]
    symbol_holdout: tuple[str, ...]
    train_test_cutoff: date
    entry_timing: str
    horizons_trading_days: tuple[int, ...]
    min_event_warning_threshold: int
    adjustment_boundary_policy: str
    bootstrap: BootstrapConfig
    condition: ConditionNode
    spec_sha256: str
    raw_document: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.spec_id, str) or not self.spec_id:
            raise StudySpecError("invalid_spec_id")
        if type(self.frozen) is not bool:
            raise StudySpecError("invalid_frozen")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise StudySpecError("invalid_symbols")
        if len(set(self.symbol_holdout)) != len(self.symbol_holdout):
            raise StudySpecError("duplicate_symbol_holdout")
        if set(self.symbol_holdout) & set(self.symbols):
            raise StudySpecError("symbol_holdout_overlaps_symbols")
        if self.entry_timing not in ALLOWED_ENTRY_TIMINGS:
            raise StudySpecError("invalid_entry_timing")
        if not self.horizons_trading_days or any(
            isinstance(h, bool) or not isinstance(h, int) or h < 1 for h in self.horizons_trading_days
        ) or len(set(self.horizons_trading_days)) != len(self.horizons_trading_days):
            raise StudySpecError("invalid_horizons_trading_days")
        if isinstance(self.min_event_warning_threshold, bool) or not isinstance(
            self.min_event_warning_threshold, int
        ) or self.min_event_warning_threshold < 1:
            raise StudySpecError("invalid_min_event_warning_threshold")
        if self.adjustment_boundary_policy not in ALLOWED_ADJUSTMENT_POLICIES:
            raise StudySpecError("invalid_adjustment_boundary_policy")

    def all_declared_symbols(self) -> tuple[str, ...]:
        """Every symbol the spec ever needs data for (train + holdout)."""
        return tuple(sorted(set(self.symbols) | set(self.symbol_holdout)))


def _require_decimal_string(value: Any, path: str) -> Decimal:
    if not isinstance(value, str):
        raise StudySpecError(f"{path}: expected a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise StudySpecError(f"{path}: invalid decimal string") from None
    if not result.is_finite():
        raise StudySpecError(f"{path}: must be finite")
    return result


def _string_tuple(raw: Any, path: str) -> tuple[str, ...]:
    items = require_list(raw, path)
    return tuple(require_string(item, f"{path}[{index}]") for index, item in enumerate(items))


def _int_tuple(raw: Any, path: str) -> tuple[int, ...]:
    items = require_list(raw, path)
    result = []
    for index, item in enumerate(items):
        if isinstance(item, bool) or not isinstance(item, int):
            raise StudySpecError(f"{path}[{index}]: expected an integer")
        result.append(item)
    return tuple(result)


def parse_study_spec(document: Any) -> StudySpec:
    """Validate a decoded study-spec document; ``spec_sha256`` covers exactly
    this decoded document (canonical JSON over it), so any byte-for-byte
    difference in the saved spec file changes the recorded hash."""
    if not isinstance(document, dict):
        raise StudySpecError("expected_object")
    try:
        root = require_mapping(document, "spec")
        if root.get("schema") != SPEC_SCHEMA:
            raise StudySpecError("unsupported_spec_schema")
        bootstrap_raw = require_mapping(require_field(root, "bootstrap", "spec"), "spec.bootstrap")
        frozen = require_field(root, "frozen", "spec")
        if type(frozen) is not bool:
            raise StudySpecError("invalid_frozen")

        spec_sha256 = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()

        return StudySpec(
            spec_id=require_string(require_field(root, "spec_id", "spec"), "spec.spec_id"),
            description=require_string(require_field(root, "description", "spec"), "spec.description"),
            frozen=frozen,
            symbols=_string_tuple(require_field(root, "symbols", "spec"), "spec.symbols"),
            symbol_holdout=_string_tuple(root.get("symbol_holdout", []), "spec.symbol_holdout"),
            train_test_cutoff=require_date(
                require_field(root, "train_test_cutoff", "spec"), "spec.train_test_cutoff",
            ),
            entry_timing=require_string(
                require_field(root, "entry_timing", "spec"), "spec.entry_timing",
            ),
            horizons_trading_days=_int_tuple(
                require_field(root, "horizons_trading_days", "spec"), "spec.horizons_trading_days",
            ),
            min_event_warning_threshold=require_field(
                root, "min_event_warning_threshold", "spec",
            ),
            adjustment_boundary_policy=require_string(
                require_field(root, "adjustment_boundary_policy", "spec"),
                "spec.adjustment_boundary_policy",
            ),
            bootstrap=BootstrapConfig(
                seed=require_field(bootstrap_raw, "seed", "spec.bootstrap"),
                resamples=require_field(bootstrap_raw, "resamples", "spec.bootstrap"),
                confidence_level=_require_decimal_string(
                    require_field(bootstrap_raw, "confidence_level", "spec.bootstrap"),
                    "spec.bootstrap.confidence_level",
                ),
            ),
            condition=parse_condition(require_field(root, "condition", "spec"), "spec.condition"),
            spec_sha256=spec_sha256,
            raw_document=document,
        )
    except MarketDataParseError as exc:
        raise StudySpecError(f"invalid_spec_field: {exc.field_path or 'unknown'}") from None


def load_study_spec(path: Path) -> StudySpec:
    """Read one study spec file. Numbers are decoded as plain JSON types
    (``bootstrap.seed``/``resamples``/horizons are integers; every other
    numeric-ish field is required to be a decimal *string* -- see
    ``_require_decimal_string`` -- so no float-rounding of a threshold ever
    happens silently)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise StudySpecError("unreadable_spec") from None
    return parse_study_spec(document)
