"""Offline, lookahead-safe evaluation of one scanner rule for one symbol.

Reuses ``fintr.research.features.truncate_period_capture``,
``fintr.research.conditions.evaluate_condition``/``evaluate_feature``,
``fintr.research.engine.load_symbol_data``/``detect_custody_scale_jumps``/
``drop_ratio_regime_signal_dates`` and
``fintr.evaluation.symbol_guard`` (loaded by the caller). No formula here is
recomputed: this module only truncates, evaluates and reports -- it builds
no forward-return event, no baseline, no score, rank or target. See
``docs/scanner.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fintr.data.period_capture import PeriodCapture, resolve_brokerage_label
from fintr.data.price_history import PriceHistoryCapture
from fintr.research.conditions import (
    BoolAnd,
    BoolNot,
    BoolOr,
    Comparison,
    ConditionNode,
    FeatureExpr,
    evaluate_condition,
)
from fintr.research.engine import (
    drop_ratio_regime_signal_dates,
    load_symbol_data,
    tradeable_price_bars,
)
from fintr.research.features import FEATURE_FORMULAS, evaluate_feature, truncate_period_capture
from fintr.scanner.rules import ScannerRule


NOTICE_TEXT_TR = (
    "Gözlem bildirimidir; yatırım tavsiyesi, al/sat sinyali, niyet veya manipülasyon "
    "iddiası değildir. Kurallar ön kayıtlı hipotezdir, holdout testi bekleniyor."
)

VALIDATION_STATUS = "preregistered_hypothesis_holdout_test_pending"

SUPPRESSED_RATIO_REGIME = "ratio_regime"
SUPPRESSED_CUSTODY_SCALE_JUMP = "custody_scale_jump"


def _leaf_features(node: ConditionNode) -> list[FeatureExpr]:
    if isinstance(node, Comparison):
        return [expr for expr in (node.left, node.right) if isinstance(expr, FeatureExpr)]
    if isinstance(node, (BoolAnd, BoolOr)):
        leaves: list[FeatureExpr] = []
        for term in node.terms:
            leaves.extend(_leaf_features(term))
        return leaves
    if isinstance(node, BoolNot):
        return _leaf_features(node.term)
    raise TypeError(f"unknown condition node type {type(node).__name__}")


def _dedup_leaves(leaves: list[FeatureExpr]) -> list[FeatureExpr]:
    seen: set[tuple[str, tuple]] = set()
    result: list[FeatureExpr] = []
    for leaf in leaves:
        key = (leaf.name, leaf.params)
        if key not in seen:
            seen.add(key)
            result.append(leaf)
    return result


def scanner_close_by_date(price: PriceHistoryCapture, as_of: date) -> dict[date, Decimal]:
    """Close prices visible as of ``as_of``: a holiday placeholder bar never
    supplies a close, and no date after ``as_of`` is included."""
    return {
        bar.trade_date: bar.close
        for bar in tradeable_price_bars(price)
        if bar.trade_date <= as_of
    }


def _institution_labels(capture: PeriodCapture, codes: tuple[str, ...]) -> dict[str, dict]:
    return {code: resolve_brokerage_label(capture.brokerage_directory, code) for code in codes}


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    name: str
    params: dict
    formula: str
    value: Decimal | None
    data_families: tuple[str, ...]
    min_date: date | None
    max_date: date | None
    institution_labels: dict[str, dict]


@dataclass(frozen=True, slots=True)
class RuleObservation:
    rule_id: str
    family_role: str
    as_of: date
    suppressed: str | None  # None | "ratio_regime" | "custody_scale_jump"
    condition_met: bool | None
    not_evaluable_reason: str | None
    features: tuple[FeatureObservation, ...]
    validation_status: str = VALIDATION_STATUS


def evaluate_rule_for_symbol(
    rule: ScannerRule, capture: PeriodCapture, price: PriceHistoryCapture, *, as_of: date,
    adjustment_ratio_epsilon: Decimal, custody_scale_jump_ratio: Decimal,
    ratio_regime_tolerance: Decimal,
) -> RuleObservation:
    """Evaluate ``rule.spec.condition`` for one symbol at one server date.

    Only ``truncate_period_capture(capture, as_of)`` and closes dated
    ``<= as_of`` are visible to the condition/features -- a change to data
    dated after ``as_of`` can never change the result returned here.
    """
    symbol_data = load_symbol_data(
        capture.symbol, capture, price,
        adjustment_ratio_epsilon=adjustment_ratio_epsilon,
        custody_scale_jump_ratio=custody_scale_jump_ratio,
        ratio_regime_tolerance=ratio_regime_tolerance,
    )
    _, dropped = drop_ratio_regime_signal_dates((as_of,), symbol_data.ratio_regime_dates)
    if dropped:
        return RuleObservation(
            rule_id=rule.rule_id, family_role=rule.family_role, as_of=as_of,
            suppressed=SUPPRESSED_RATIO_REGIME, condition_met=None,
            not_evaluable_reason=None, features=(),
        )

    truncated = truncate_period_capture(capture, as_of)
    close_by_date = scanner_close_by_date(price, as_of)
    result = evaluate_condition(rule.spec.condition, truncated, as_of, close_by_date=close_by_date)

    spans_jump = "custody" in result.data_families and any(
        result.min_date is not None and result.max_date is not None
        and result.min_date < jump_date <= result.max_date
        for jump_date in symbol_data.custody_scale_jump_dates
    )
    if spans_jump:
        return RuleObservation(
            rule_id=rule.rule_id, family_role=rule.family_role, as_of=as_of,
            suppressed=SUPPRESSED_CUSTODY_SCALE_JUMP, condition_met=None,
            not_evaluable_reason=None, features=(),
        )

    leaves = _dedup_leaves(_leaf_features(rule.spec.condition))
    feature_obs: list[FeatureObservation] = []
    for leaf in leaves:
        params_dict = leaf.params_dict()
        value = evaluate_feature(leaf.name, params_dict, truncated, as_of, close_by_date=close_by_date)
        codes = params_dict.get("codes", ())
        labels = _institution_labels(capture, tuple(codes)) if codes else {}
        feature_obs.append(FeatureObservation(
            name=leaf.name, params=params_dict, formula=FEATURE_FORMULAS[leaf.name],
            value=value.value, data_families=value.data_families,
            min_date=value.min_date, max_date=value.max_date, institution_labels=labels,
        ))

    not_evaluable_reason = None if result.value is not None else "missing_data"
    return RuleObservation(
        rule_id=rule.rule_id, family_role=rule.family_role, as_of=as_of,
        suppressed=None, condition_met=result.value, not_evaluable_reason=not_evaluable_reason,
        features=tuple(feature_obs),
    )
