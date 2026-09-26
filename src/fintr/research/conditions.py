"""Condition AST for study specs: comparisons/boolean logic over named,
deterministic features only.

There is no free-text expression and no ``eval``/model call anywhere in this
module: a condition is a small, closed JSON grammar parsed into the frozen
dataclasses below, and every leaf is either a literal ``Decimal`` constant or
a call to one of the fixed feature names in
``fintr.research.features.FEATURE_FORMULAS``. Unknown operators or feature
names are rejected at parse time (``ConditionError``), before any capture is
loaded.

Grammar (JSON)::

    condition := comparison | {"op": "and"|"or", "terms": [condition, ...]}
               | {"op": "not", "term": condition}
    comparison := {"op": "gt"|"ge"|"lt"|"le"|"eq"|"ne",
                   "left": expr, "right": expr}
    expr := {"const": "<decimal string>"} | {"feature": "<name>", "params": {...}}

Evaluation returns ``True``/``False``/``None`` for each server date; ``None``
means "not evaluable that day" (a missing input), and propagates strictly
through ``and``/``or``/``not`` -- it is never defaulted to ``True`` or
``False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fintr.data.period_capture import PeriodCapture
from fintr.research.features import FeatureValue, evaluate_feature, validate_feature_params


class ConditionError(ValueError):
    """A study spec's ``condition`` field is not a valid condition."""


_COMPARISON_OPS = ("gt", "ge", "lt", "le", "eq", "ne")
_BOOL_OPS = ("and", "or", "not")


@dataclass(frozen=True, slots=True)
class ConstExpr:
    value: Decimal


@dataclass(frozen=True, slots=True)
class FeatureExpr:
    name: str
    params: tuple[tuple[str, Any], ...]  # sorted (key, value) pairs; JSON-safe values only

    def params_dict(self) -> dict[str, Any]:
        return dict(self.params)


Expr = ConstExpr | FeatureExpr


@dataclass(frozen=True, slots=True)
class Comparison:
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True, slots=True)
class BoolAnd:
    terms: tuple["ConditionNode", ...]


@dataclass(frozen=True, slots=True)
class BoolOr:
    terms: tuple["ConditionNode", ...]


@dataclass(frozen=True, slots=True)
class BoolNot:
    term: "ConditionNode"


ConditionNode = Comparison | BoolAnd | BoolOr | BoolNot


def _parse_params(raw: Any, path: str) -> tuple[tuple[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ConditionError(f"{path}.params: expected an object")
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ConditionError(f"{path}.params: keys must be strings")
        if isinstance(value, list):
            if any(not isinstance(item, str) for item in value):
                raise ConditionError(f"{path}.params.{key}: list entries must be strings")
        elif not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ConditionError(f"{path}.params.{key}: expected a string, integer or list of strings")
    return tuple(sorted((key, tuple(value) if isinstance(value, list) else value)
                        for key, value in raw.items()))


def _parse_expr(raw: Any, path: str) -> Expr:
    if not isinstance(raw, dict):
        raise ConditionError(f"{path}: expected an object")
    if "const" in raw:
        if "feature" in raw:
            raise ConditionError(f"{path}: expected exactly one of 'const'/'feature'")
        text = raw["const"]
        if not isinstance(text, str):
            raise ConditionError(f"{path}.const: expected a decimal string, not a bare number")
        try:
            value = Decimal(text)
        except InvalidOperation:
            raise ConditionError(f"{path}.const: invalid decimal string") from None
        if not value.is_finite():
            raise ConditionError(f"{path}.const: must be finite")
        return ConstExpr(value)
    if "feature" in raw:
        name = raw["feature"]
        if not isinstance(name, str) or not name:
            raise ConditionError(f"{path}.feature: expected a non-empty string")
        params = _parse_params(raw.get("params"), path)
        params_dict = dict(params)
        validate_feature_params(name, params_dict, f"{path}.feature[{name}]")
        return FeatureExpr(name, params)
    raise ConditionError(f"{path}: expected 'const' or 'feature'")


def parse_condition(raw: Any, path: str = "condition") -> ConditionNode:
    if not isinstance(raw, dict):
        raise ConditionError(f"{path}: expected an object")
    op = raw.get("op")
    if not isinstance(op, str):
        raise ConditionError(f"{path}.op: expected a string")
    if op in _COMPARISON_OPS:
        if set(raw) - {"op", "left", "right"}:
            raise ConditionError(f"{path}: comparison takes only 'op', 'left', 'right'")
        if "left" not in raw or "right" not in raw:
            raise ConditionError(f"{path}: comparison requires 'left' and 'right'")
        return Comparison(op, _parse_expr(raw["left"], f"{path}.left"), _parse_expr(raw["right"], f"{path}.right"))
    if op in ("and", "or"):
        if set(raw) - {"op", "terms"}:
            raise ConditionError(f"{path}: '{op}' takes only 'op', 'terms'")
        terms_raw = raw.get("terms")
        if not isinstance(terms_raw, list) or len(terms_raw) < 2:
            raise ConditionError(f"{path}.terms: expected a list of at least 2 conditions")
        terms = tuple(parse_condition(item, f"{path}.terms[{index}]") for index, item in enumerate(terms_raw))
        return BoolAnd(terms) if op == "and" else BoolOr(terms)
    if op == "not":
        if set(raw) - {"op", "term"}:
            raise ConditionError(f"{path}: 'not' takes only 'op', 'term'")
        if "term" not in raw:
            raise ConditionError(f"{path}.term: required")
        return BoolNot(parse_condition(raw["term"], f"{path}.term"))
    raise ConditionError(
        f"{path}.op: unknown operator '{op}' "
        f"(allowed: {', '.join(_COMPARISON_OPS + _BOOL_OPS)})"
    )


@dataclass(frozen=True, slots=True)
class ConditionEvalResult:
    """A condition's truth value at one date, plus the union of every leaf
    feature's data-family/date-range provenance actually read to compute it
    (every leaf is always evaluated -- no short-circuiting -- so provenance
    is complete regardless of which branch decided the boolean result)."""

    value: bool | None
    data_families: tuple[str, ...]
    min_date: date | None
    max_date: date | None


def _merge(values: list[FeatureValue]) -> tuple[tuple[str, ...], date | None, date | None]:
    families: set[str] = set()
    min_date: date | None = None
    max_date: date | None = None
    for item in values:
        families.update(item.data_families)
        if item.min_date is not None and (min_date is None or item.min_date < min_date):
            min_date = item.min_date
        if item.max_date is not None and (max_date is None or item.max_date > max_date):
            max_date = item.max_date
    return tuple(sorted(families)), min_date, max_date


def _eval_expr(expr: Expr, capture: PeriodCapture, t: date, close_by_date: dict[date, Decimal]) -> tuple[Decimal | None, FeatureValue | None]:
    if isinstance(expr, ConstExpr):
        return expr.value, None
    feature_value = evaluate_feature(expr.name, expr.params_dict(), capture, t, close_by_date=close_by_date)
    return feature_value.value, feature_value


def _compare(op: str, left: Decimal, right: Decimal) -> bool:
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    raise ConditionError(f"unknown operator '{op}'")  # unreachable after parse_condition


def evaluate_condition(
    node: ConditionNode, capture: PeriodCapture, t: date, *, close_by_date: dict[date, Decimal],
) -> ConditionEvalResult:
    """Evaluate ``node`` against a caller-truncated ``capture`` at date ``t``.

    ``capture`` must already be truncated to ``t`` (see
    ``fintr.research.features.truncate_period_capture``); this function does
    not truncate on its own, so a caller that forgets to truncate would not
    be caught here -- the engine always truncates before calling this.
    """
    if isinstance(node, Comparison):
        left_value, left_feature = _eval_expr(node.left, capture, t, close_by_date)
        right_value, right_feature = _eval_expr(node.right, capture, t, close_by_date)
        touched = [item for item in (left_feature, right_feature) if item is not None]
        families, min_date, max_date = _merge(touched)
        if left_value is None or right_value is None:
            return ConditionEvalResult(None, families, min_date, max_date)
        return ConditionEvalResult(_compare(node.op, left_value, right_value), families, min_date, max_date)
    if isinstance(node, (BoolAnd, BoolOr)):
        results = [evaluate_condition(term, capture, t, close_by_date=close_by_date) for term in node.terms]
        # Provenance is unconditionally the union over every term (no
        # short-circuiting), so a custody-scale-jump check downstream never
        # under-counts what this condition actually read.
        families = tuple(sorted({family for item in results for family in item.data_families}))
        min_dates = [item.min_date for item in results if item.min_date is not None]
        max_dates = [item.max_date for item in results if item.max_date is not None]
        min_date = min(min_dates) if min_dates else None
        max_date = max(max_dates) if max_dates else None
        values = [item.value for item in results]
        if any(value is None for value in values):
            return ConditionEvalResult(None, families, min_date, max_date)
        value = all(values) if isinstance(node, BoolAnd) else any(values)
        return ConditionEvalResult(value, families, min_date, max_date)
    if isinstance(node, BoolNot):
        inner = evaluate_condition(node.term, capture, t, close_by_date=close_by_date)
        value = None if inner.value is None else not inner.value
        return ConditionEvalResult(value, inner.data_families, inner.min_date, inner.max_date)
    raise ConditionError(f"unknown condition node type {type(node).__name__}")
