"""``fintr_test_plan_v1``: the one-shot pre-registered holdout-test plan.

This is a second, narrower pre-registration artifact on top of a study
spec's own ``frozen``/``preregistration`` block (``fintr.research.
study_spec``): it names the test window (``test_start``..``test_end``), the
minimum post-window tradeable-bar requirement, the per-side transaction
cost, and -- for each spec the one-shot test runs -- the exact raw file
bytes and canonical (``spec_sha256``) hashes recorded at freeze time
(``docs/prereg/FREEZE.md``). ``scripts/run_study.py --split test`` refuses
to run unless the spec it is given matches one of these entries byte-for-
byte; nothing here recomputes a return statistic.

Two test scopes are declared, both read-only documentation plus the one
machine-checked field (``symbol_scope``) the CLI enforces:

- ``primary``: ``symbol_scope: "all_declared"`` -- every symbol the spec
  ever declared (20 development + 10 holdout); the confirmatory Holm
  decision is taken only from this scope's results (``docs/prereg/
  DECISIONS.md`` 2026-09-28).
- ``secondary``: ``symbol_scope: "holdout_only"`` -- only the spec's
  ``symbol_holdout``; a pre-registered robustness check, never
  confirmatory on its own.
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


TEST_PLAN_SCHEMA = "fintr_test_plan_v1"
SYMBOL_SCOPE_PRIMARY = "all_declared"
SYMBOL_SCOPE_SECONDARY = "holdout_only"
ALLOWED_TEST_SCOPES = ("primary", "secondary")

_SHA256_RE_LEN = 64


class TestPlanError(ValueError):
    """A test-plan document failed structural validation, or a spec did not
    match the plan's recorded hashes for it."""


def _sha256_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_RE_LEN or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise TestPlanError(f"{path}: expected a lowercase hex sha256 string")
    return value


def _require_decimal_string(value: Any, path: str) -> Decimal:
    if not isinstance(value, str):
        raise TestPlanError(f"{path}: expected a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise TestPlanError(f"{path}: invalid decimal string") from None
    if not result.is_finite():
        raise TestPlanError(f"{path}: must be finite")
    return result


def _require_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TestPlanError(f"{path}: expected an integer")
    return value


@dataclass(frozen=True, slots=True)
class SpecRef:
    path: str
    spec_id: str
    raw_sha256: str
    canonical_sha256: str


@dataclass(frozen=True, slots=True)
class PrimaryScope:
    symbol_scope: str
    family_id: str
    primary_family_members: tuple[str, ...]
    multiple_comparison: str

    def __post_init__(self) -> None:
        if self.symbol_scope != SYMBOL_SCOPE_PRIMARY:
            raise TestPlanError("invalid_primary_symbol_scope")
        if not self.family_id:
            raise TestPlanError("invalid_primary_family_id")
        if not self.primary_family_members:
            raise TestPlanError("invalid_primary_family_members")


@dataclass(frozen=True, slots=True)
class SecondaryScope:
    symbol_scope: str

    def __post_init__(self) -> None:
        if self.symbol_scope != SYMBOL_SCOPE_SECONDARY:
            raise TestPlanError("invalid_secondary_symbol_scope")


@dataclass(frozen=True, slots=True)
class TestPlan:
    plan_id: str
    test_start: date
    test_end: date
    min_tradeable_bars_after_end: int
    cost_bps_per_side: Decimal
    primary: PrimaryScope
    secondary: SecondaryScope
    specs: tuple[SpecRef, ...]
    plan_sha256: str
    raw_document: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise TestPlanError("invalid_plan_id")
        if self.test_end <= self.test_start:
            raise TestPlanError("test_end_not_after_test_start")
        if self.min_tradeable_bars_after_end < 1:
            raise TestPlanError("invalid_min_tradeable_bars_after_end")
        if self.cost_bps_per_side < 0:
            raise TestPlanError("invalid_cost_bps_per_side")
        if not self.specs:
            raise TestPlanError("no_specs")
        ids = [ref.spec_id for ref in self.specs]
        if len(set(ids)) != len(ids):
            raise TestPlanError("duplicate_spec_id")
        paths = [ref.path for ref in self.specs]
        if len(set(paths)) != len(paths):
            raise TestPlanError("duplicate_spec_path")

    def spec_by_id(self, spec_id: str) -> SpecRef | None:
        return next((ref for ref in self.specs if ref.spec_id == spec_id), None)

    def scope(self, test_scope: str):
        if test_scope == "primary":
            return self.primary
        if test_scope == "secondary":
            return self.secondary
        raise TestPlanError("invalid_test_scope")


def _parse_spec_ref(raw: Any, path: str) -> SpecRef:
    item = require_mapping(raw, path)
    return SpecRef(
        path=require_string(require_field(item, "path", path), f"{path}.path"),
        spec_id=require_string(require_field(item, "spec_id", path), f"{path}.spec_id"),
        raw_sha256=_sha256_string(require_field(item, "raw_sha256", path), f"{path}.raw_sha256"),
        canonical_sha256=_sha256_string(
            require_field(item, "canonical_sha256", path), f"{path}.canonical_sha256",
        ),
    )


def parse_test_plan(document: Any, *, plan_sha256: str | None = None) -> TestPlan:
    try:
        root = require_mapping(document, "test_plan")
        if root.get("schema") != TEST_PLAN_SCHEMA:
            raise TestPlanError("unsupported_test_plan_schema")
        primary_raw = require_mapping(require_field(root, "primary", "test_plan"), "test_plan.primary")
        secondary_raw = require_mapping(
            require_field(root, "secondary", "test_plan"), "test_plan.secondary",
        )
        specs = tuple(
            _parse_spec_ref(raw, f"test_plan.specs[{index}]")
            for index, raw in enumerate(require_list(require_field(root, "specs", "test_plan"), "test_plan.specs"))
        )
        return TestPlan(
            plan_id=require_string(require_field(root, "plan_id", "test_plan"), "test_plan.plan_id"),
            test_start=require_date(require_field(root, "test_start", "test_plan"), "test_plan.test_start"),
            test_end=require_date(require_field(root, "test_end", "test_plan"), "test_plan.test_end"),
            min_tradeable_bars_after_end=_require_int(
                require_field(root, "min_tradeable_bars_after_end", "test_plan"),
                "test_plan.min_tradeable_bars_after_end",
            ),
            cost_bps_per_side=_require_decimal_string(
                require_field(root, "cost_bps_per_side", "test_plan"), "test_plan.cost_bps_per_side",
            ),
            primary=PrimaryScope(
                symbol_scope=require_string(
                    require_field(primary_raw, "symbol_scope", "test_plan.primary"),
                    "test_plan.primary.symbol_scope",
                ),
                family_id=require_string(
                    require_field(primary_raw, "family_id", "test_plan.primary"),
                    "test_plan.primary.family_id",
                ),
                primary_family_members=tuple(
                    require_string(item, f"test_plan.primary.primary_family_members[{i}]")
                    for i, item in enumerate(require_list(
                        require_field(primary_raw, "primary_family_members", "test_plan.primary"),
                        "test_plan.primary.primary_family_members",
                    ))
                ),
                multiple_comparison=require_string(
                    require_field(primary_raw, "multiple_comparison", "test_plan.primary"),
                    "test_plan.primary.multiple_comparison",
                ),
            ),
            secondary=SecondaryScope(
                symbol_scope=require_string(
                    require_field(secondary_raw, "symbol_scope", "test_plan.secondary"),
                    "test_plan.secondary.symbol_scope",
                ),
            ),
            specs=specs,
            plan_sha256=plan_sha256,
            raw_document=document,
        )
    except MarketDataParseError as exc:
        raise TestPlanError(f"invalid_test_plan_field: {exc.field_path or 'unknown'}") from None


def load_test_plan(path: Path) -> TestPlan:
    try:
        raw_bytes = Path(path).read_bytes()
        document = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise TestPlanError("unreadable_test_plan") from None
    return parse_test_plan(document, plan_sha256=hashlib.sha256(raw_bytes).hexdigest())
