"""Run the custody-vs-AKD residual / same-day candidate pairs / code-set lot
series (``fintr.features.custody_flow``) on one saved period capture.

Prints a JSON object with the three results. No network access, no model
call. Same-day pairs are printed as candidates only -- see
``fintr.features.custody_flow`` module docstring: a match is not evidence of
a transfer/virman or of beneficial ownership. There is no built-in code set;
pass ``--code-set`` with a JSON file ({"name", "version", "codes"}) to get a
code-set lot series, otherwise that section is omitted.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any

from fintr.data.period_capture import PeriodCapture, PeriodCaptureError, load_period_capture
from fintr.features.custody_flow import (
    DEFAULT_PAIR_TOLERANCE,
    FORMULAS,
    compute_code_set_series,
    compute_custody_residual,
    find_same_day_pairs,
    load_code_set,
)


def _num(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _day(value) -> str:
    return value.isoformat()


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int: {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive int")
    return value


def _non_negative_decimal(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"invalid decimal: {raw!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Custody-vs-AKD residual, same-day candidate pairs, and an "
            "optional named code-set lot series over a saved period capture. "
            "Read-only, offline; prints JSON only."
        ),
    )
    parser.add_argument("capture", type=Path, help="Path to a saved period-analysis capture JSON.")
    parser.add_argument(
        "--lag", type=_positive_int, default=2,
        help="Server-date steps between the custody-change window and the AKD anchor date (default: 2).",
    )
    parser.add_argument(
        "--pair-tolerance", type=_non_negative_decimal, default=DEFAULT_PAIR_TOLERANCE,
        help=f"Same-day pair cancellation tolerance, a non-negative Decimal (default: {DEFAULT_PAIR_TOLERANCE}).",
    )
    parser.add_argument(
        "--code-set", type=Path, default=None, metavar="CODE_SET_JSON",
        help="Optional path to a {'name','version','codes'} JSON file for the code-set lot series.",
    )
    return parser


def _residual_to_json(result) -> dict[str, Any]:
    return {
        "formula_version": result.formula_version,
        "lag": result.lag,
        "formula": FORMULAS["custody_residual"],
        "days": [
            {
                "date": _day(day.date),
                "previous_date": _day(day.previous_date),
                "akd_date": _day(day.akd_date),
                "residuals": [
                    {"code": row.code, "residual": _num(row.residual)} for row in day.rows
                ],
            }
            for day in result.days
        ],
    }


def _pairs_to_json(result) -> dict[str, Any]:
    return {
        "formula_version": result.formula_version,
        "tolerance": _num(result.tolerance),
        "formula": FORMULAS["same_day_pairs"],
        "note": (
            "Candidates only. A matching pair is not evidence of a "
            "transfer/virman or of beneficial ownership."
        ),
        "pairs": [
            {
                "date": _day(pair.date),
                "code_a": pair.code_a, "residual_a": _num(pair.residual_a),
                "code_b": pair.code_b, "residual_b": _num(pair.residual_b),
            }
            for pair in result.pairs
        ],
    }


def _code_set_to_json(result) -> dict[str, Any]:
    return {
        "formula_version": result.formula_version,
        "code_set_name": result.code_set_name,
        "code_set_version": result.code_set_version,
        "codes": list(result.codes),
        "formula": FORMULAS["code_set_series"],
        "days": [
            {
                "date": _day(day.date),
                "total_lots": _num(day.total_lots),
                "listed_count": day.listed_count,
                "missing_count": day.missing_count,
                "missing_codes": list(day.missing_codes),
                "change": _num(day.change),
            }
            for day in result.days
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        capture: PeriodCapture = load_period_capture(args.capture)
    except PeriodCaptureError as exc:
        parser.error(f"invalid capture: {exc}")
        return 2

    residual_result = compute_custody_residual(capture, lag=args.lag)
    pair_result = find_same_day_pairs(residual_result, tolerance=args.pair_tolerance)

    output: dict[str, Any] = {
        "symbol": capture.symbol,
        "custody_residual": _residual_to_json(residual_result),
        "same_day_pairs": _pairs_to_json(pair_result),
    }

    if args.code_set is not None:
        try:
            code_set = load_code_set(args.code_set)
        except (ValueError, OSError) as exc:
            parser.error(f"--code-set: {exc}")
            return 2
        code_set_result = compute_code_set_series(capture, code_set)
        output["code_set_series"] = _code_set_to_json(code_set_result)

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
