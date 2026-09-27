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
ALLOWED_PREDICTED_DIRECTIONS = ("negative", "positive")
FAMILY_ROLE_PRIMARY = "primary"
FAMILY_ROLE_SECONDARY_DESCRIPTIVE = "secondary_descriptive"
ALLOWED_FAMILY_ROLES = (FAMILY_ROLE_PRIMARY, FAMILY_ROLE_SECONDARY_DESCRIPTIVE)
ALLOWED_MULTIPLE_COMPARISONS = ("holm",)


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
class BlockBootstrapConfig:
    """E1: moving block bootstrap over sorted distinct signal dates.

    Optional top-level ``block_bootstrap`` spec block; there is no default --
    a spec without it gets no block bootstrap (``None`` in the result), never
    a silently assumed block length, resample count or seed.
    """

    block_length_signal_dates: int
    resamples: int
    seed: int

    def __post_init__(self) -> None:
        for name in ("block_length_signal_dates", "resamples", "seed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise StudySpecError(f"invalid_block_bootstrap_{name}")
        if self.block_length_signal_dates < 1:
            raise StudySpecError("invalid_block_bootstrap_block_length_signal_dates")
        if self.resamples < 1:
            raise StudySpecError("invalid_block_bootstrap_resamples")


@dataclass(frozen=True, slots=True)
class PreregistrationInfo:
    """The machine-read subset of an optional ``preregistration`` block.

    Only these keys are validated and used (one-sided p direction, primary
    horizon, minimum-event rule, multiple-comparison family); every other
    key in the block stays documentation, but the whole block is still part
    of ``spec_sha256``. ``family_role == "primary"`` iff ``hypothesis_id`` is
    listed in ``primary_family_members``; a ``secondary_descriptive``
    hypothesis never enters the Holm family (``fintr.research.family``).
    """

    hypothesis_id: str
    family_id: str
    family_role: str
    primary_family_members: tuple[str, ...]
    family_size: int
    multiple_comparison: str
    familywise_alpha: Decimal
    predicted_direction: str
    primary_horizon_trading_days: int
    min_events_primary: int
    min_distinct_signal_dates_primary: int
    sesoi_difference: Decimal

    def __post_init__(self) -> None:
        if not self.hypothesis_id or not self.family_id:
            raise StudySpecError("invalid_preregistration_ids")
        if self.family_role not in ALLOWED_FAMILY_ROLES:
            raise StudySpecError("invalid_preregistration_family_role")
        members = self.primary_family_members
        if not members or len(set(members)) != len(members) or any(not m for m in members):
            raise StudySpecError("invalid_preregistration_primary_family_members")
        if self.family_size != len(members):
            raise StudySpecError("preregistration_family_size_mismatch")
        if (self.family_role == FAMILY_ROLE_PRIMARY) != (self.hypothesis_id in members):
            raise StudySpecError("preregistration_family_role_membership_mismatch")
        if self.multiple_comparison not in ALLOWED_MULTIPLE_COMPARISONS:
            raise StudySpecError("invalid_preregistration_multiple_comparison")
        if not (Decimal(0) < self.familywise_alpha < Decimal(1)):
            raise StudySpecError("invalid_preregistration_familywise_alpha")
        if self.predicted_direction not in ALLOWED_PREDICTED_DIRECTIONS:
            raise StudySpecError("invalid_preregistration_predicted_direction")
        for name in ("primary_horizon_trading_days", "min_events_primary",
                     "min_distinct_signal_dates_primary"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise StudySpecError(f"invalid_preregistration_{name}")
        if self.sesoi_difference < 0:
            raise StudySpecError("invalid_preregistration_sesoi_difference")


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
    block_bootstrap: BlockBootstrapConfig | None = None
    preregistration: PreregistrationInfo | None = None

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
        if (self.preregistration is not None
                and self.preregistration.primary_horizon_trading_days not in self.horizons_trading_days):
            raise StudySpecError("preregistration_primary_horizon_not_in_horizons")
        if self.preregistration is not None and self.block_bootstrap is None:
            # The pre-registered primary test is E1; no silent i.i.d. fallback.
            raise StudySpecError("preregistration_requires_block_bootstrap")

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


def _require_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StudySpecError(f"{path}: expected an integer")
    return value


def _parse_block_bootstrap(raw: Any) -> BlockBootstrapConfig | None:
    if raw is None:
        return None
    block = require_mapping(raw, "spec.block_bootstrap")
    if block.get("method") != "moving_block_by_signal_date":
        raise StudySpecError("unsupported_block_bootstrap_method")
    return BlockBootstrapConfig(
        block_length_signal_dates=require_field(
            block, "block_length_signal_dates", "spec.block_bootstrap"),
        resamples=require_field(block, "resamples", "spec.block_bootstrap"),
        seed=require_field(block, "seed", "spec.block_bootstrap"),
    )


def _parse_preregistration(raw: Any) -> PreregistrationInfo | None:
    if raw is None:
        return None
    path = "spec.preregistration"
    block = require_mapping(raw, path)

    def field(name: str) -> Any:
        return require_field(block, name, path)

    return PreregistrationInfo(
        hypothesis_id=require_string(field("hypothesis_id"), f"{path}.hypothesis_id"),
        family_id=require_string(field("family_id"), f"{path}.family_id"),
        family_role=require_string(field("family_role"), f"{path}.family_role"),
        primary_family_members=_string_tuple(
            field("primary_family_members"), f"{path}.primary_family_members"),
        family_size=_require_int(field("family_size"), f"{path}.family_size"),
        multiple_comparison=require_string(
            field("multiple_comparison"), f"{path}.multiple_comparison"),
        familywise_alpha=_require_decimal_string(
            field("familywise_alpha"), f"{path}.familywise_alpha"),
        predicted_direction=require_string(
            field("predicted_direction"), f"{path}.predicted_direction"),
        primary_horizon_trading_days=_require_int(
            field("primary_horizon_trading_days"), f"{path}.primary_horizon_trading_days"),
        min_events_primary=_require_int(
            field("min_events_primary"), f"{path}.min_events_primary"),
        min_distinct_signal_dates_primary=_require_int(
            field("min_distinct_signal_dates_primary"),
            f"{path}.min_distinct_signal_dates_primary"),
        sesoi_difference=_require_decimal_string(
            field("sesoi_difference"), f"{path}.sesoi_difference"),
    )


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
            block_bootstrap=_parse_block_bootstrap(root.get("block_bootstrap")),
            preregistration=_parse_preregistration(root.get("preregistration")),
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
