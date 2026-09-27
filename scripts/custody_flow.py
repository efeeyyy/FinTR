"""Run the custody-vs-AKD residual / same-day candidate pairs / code-set lot
series (``fintr.features.custody_flow``) on one saved period capture.

Prints a JSON object with the three results. No network access, no model
call. Same-day pairs are printed as candidates only -- see
``fintr.features.custody_flow`` module docstring: a match is not evidence of
a transfer/virman or of beneficial ownership. There is no built-in code set;
pass ``--code-set`` with a JSON file ({"name", "version", "codes"}) to get a
code-set lot series, otherwise that section is omitted. ``--missing-akd-policy``
controls how a code absent from an otherwise-present AKD day panel is
treated for the residual (default ``zero``; see
``fintr.features.custody_flow`` module docstring and ``docs/custody_flow.md``).
Also always prints an estimated retail/institutional lot series
(``estimated_retail_lots``), a computed value derived from the provider's own
percentage split -- see ``fintr.features.custody_flow.RETAIL_LOT_LABEL`` and
``docs/custody_flow.md``; ``--provisional-last-custody-days`` controls how
many of the most recent server dates it flags as provisional.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import Any

from fintr.data.period_capture import PeriodCapture, PeriodCaptureError, load_period_capture
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.evaluation.symbol_split import NO_SPLIT_GUARD_WARNING
from fintr.features.custody_flow import (
    DEFAULT_MISSING_AKD_POLICY,
    DEFAULT_PAIR_TOLERANCE,
    FORMULAS,
    MISSING_AKD_POLICIES,
    MISSING_AKD_ZERO_EVIDENCE,
    compute_code_set_series,
    compute_custody_residual,
    compute_estimated_retail_lots,
    find_same_day_pairs,
    load_code_set,
)
from fintr.features.period import DEFAULT_PROVISIONAL_LAST_CUSTODY_DAYS


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


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int: {raw!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
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
        "--missing-akd-policy", choices=MISSING_AKD_POLICIES, default=DEFAULT_MISSING_AKD_POLICY,
        help=(
            "How to treat a code absent from an otherwise-present AKD day panel: "
            "'zero' (default; treat as net 0 for that day, per the 2026-09-26 "
            "decision -- see fintr.features.custody_flow.MISSING_AKD_ZERO_EVIDENCE) "
            "or 'unknown' (previous behaviour: treat as None). A day whose AKD "
            "panel is entirely missing is always None under either policy."
        ),
    )
    parser.add_argument(
        "--code-set", type=Path, default=None, metavar="CODE_SET_JSON",
        help="Optional path to a {'name','version','codes'} JSON file for the code-set lot series.",
    )
    parser.add_argument(
        "--provisional-last-custody-days", type=_non_negative_int,
        default=DEFAULT_PROVISIONAL_LAST_CUSTODY_DAYS,
        help=(
            "How many of the most recent server dates the estimated "
            "retail/institutional lot series flags as provisional "
            f"(default: {DEFAULT_PROVISIONAL_LAST_CUSTODY_DAYS}; 0 disables "
            "marking); same policy as fintr.features.period."
        ),
    )
    parser.add_argument(
        "--no-split-guard", action="store_true",
        help=(
            "Disable the frozen BIST 30 holdout-split guard (by default a holdout "
            "symbol's capture is refused). Only for non-BIST 30 exploratory work; "
            "the output's split_guard block says so loudly. The active split comes "
            "only from configs/splits/current.json. Excluded symbols "
            "(configs/exclusions/symbols.json) are always refused; no override."
        ),
    )
    return parser


def _residual_to_json(result) -> dict[str, Any]:
    return {
        "formula_version": result.formula_version,
        "lag": result.lag,
        "missing_akd_policy": result.missing_akd_policy,
        "missing_akd_policy_evidence": (
            MISSING_AKD_ZERO_EVIDENCE if result.missing_akd_policy == "zero" else None
        ),
        "formula": FORMULAS["custody_residual"],
        "days": [
            {
                "date": _day(day.date),
                "previous_date": _day(day.previous_date),
                "akd_date": _day(day.akd_date),
                "residuals": [
                    {
                        "code": row.code,
                        "residual": _num(row.residual),
                        "akd_defaulted": row.akd_defaulted,
                    }
                    for row in day.rows
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


def _retail_lots_to_json(result) -> dict[str, Any]:
    return {
        "formula_version": result.formula_version,
        "label": result.label,
        "validation_status": result.validation_status,
        "validation_study_file": result.validation_study_file,
        "validation_note": result.validation_note,
        "provisional_last_custody_days": result.provisional_last_custody_days,
        "formula": FORMULAS["estimated_retail_lots"],
        "days": [
            {
                "date": _day(day.date),
                "total_custody_lots": _num(day.total_custody_lots),
                "retail_pct": _num(day.retail_pct),
                "institutional_pct": _num(day.institutional_pct),
                "estimated_retail_lots": _num(day.estimated_retail_lots),
                "estimated_institutional_lots": _num(day.estimated_institutional_lots),
                "provisional": day.provisional,
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

    try:
        guards = load_active_guards(bypass_split=args.no_split_guard)
    except SymbolGuardError as exc:
        parser.error(str(exc))
        return 2
    if guards.is_excluded(capture.symbol):
        parser.error(
            f"symbol {capture.symbol} is excluded "
            f"({guards.exclusion_reason(capture.symbol)})"
        )
        return 2
    if guards.blocks_holdout(capture.symbol):
        parser.error(
            f"symbol {capture.symbol} is a holdout symbol of the frozen split "
            f"{guards.report()['split_file']} (sha256={guards.split.split_sha256})"
        )
        return 2
    if guards.split_guard_bypassed:
        print(NO_SPLIT_GUARD_WARNING, file=sys.stderr)
    split_guard = guards.report(processed_symbols=[capture.symbol])

    residual_result = compute_custody_residual(
        capture, lag=args.lag, missing_akd_policy=args.missing_akd_policy,
    )
    pair_result = find_same_day_pairs(residual_result, tolerance=args.pair_tolerance)
    retail_lots_result = compute_estimated_retail_lots(
        capture, provisional_last_custody_days=args.provisional_last_custody_days,
    )

    output: dict[str, Any] = {
        "symbol": capture.symbol,
        "split_guard": split_guard,
        "custody_residual": _residual_to_json(residual_result),
        "same_day_pairs": _pairs_to_json(pair_result),
        "estimated_retail_lots": _retail_lots_to_json(retail_lots_result),
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
