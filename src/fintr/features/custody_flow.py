"""Custody-vs-AKD residual, same-day candidate pairs, and named code-set lot series.

Reproduces, as auditable production code, three offline observations that
were previously computed ad hoc against a saved
``fintr.data.period_capture.PeriodCapture``:

* ``compute_custody_residual`` -- the gap between a code's custody-lot change
  and its AKD net a fixed number of server-date steps earlier.
* ``find_same_day_pairs`` -- same-day residual pairs that cancel out exactly
  (within a tolerance), listed only as **candidates**. A matching pair is
  NOT proof of a transfer ("virman") or of beneficial ownership; it is only
  an arithmetic coincidence between two independently unverified series.
* ``compute_code_set_series`` -- a custody-lot series summed over a caller
  -supplied, named and versioned code set (e.g. a three-brokerage grouping
  the caller defines). There is no built-in "retail" or similar code set;
  the caller must pass one in explicitly (see ``CodeSet``/``load_code_set``).

All three treat "code not listed for this date" as an unknown, never as
zero: a residual whose custody lot, previous custody lot, or AKD net input
is missing for that code/date is ``None``, not ``0``. This differs from an
earlier ad hoc scratch script that computed the same residual over the same
capture (AKFIS, 2026-06-29..2026-09-25) with a missing-AKD-code treated as
0 net for that date -- documented here as a deliberate correction, not
adopted, per this project's rule against filling a missing financial input
with an invented value.

See ``FORMULAS`` for exact definitions and ``FORMULA_VERSION`` for the
version tag carried on every result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from fintr.data.period_capture import PeriodCapture


FORMULA_VERSION = "custody_flow_v1"

# Exact-equality by default: the scratch script this reproduces used exact
# cancellation (v2 == -v1). Exposed as a parameter, never a hidden default,
# for callers whose lot values carry float-ish noise.
DEFAULT_PAIR_TOLERANCE = Decimal(0)

FORMULAS: dict[str, str] = {
    "custody_residual": (
        "For server-date index i >= lag, with t = server_dates[i], "
        "p = server_dates[i-1], a = server_dates[i-lag]: "
        "residual(code, t) = (lot[code, t] - lot[code, p]) - akd_net[code, a]. "
        "residual(code, t) is None (never 0) if lot[code, t], lot[code, p], "
        "or akd_net[code, a] is not listed for that code on that date. "
        "Computed for the union of codes listed in custody at t or at p."
    ),
    "same_day_pairs": (
        "For one custody date t, over codes with a non-None, non-zero "
        "residual(code, t): a candidate pair (code_a, code_b) exists when "
        "|residual(code_a, t) + residual(code_b, t)| <= tolerance (default "
        "0, i.e. exact cancellation). Codes are sorted, each code is used "
        "in at most one pair per date, and pairing is greedy left-to-right "
        "over the sorted, unmatched codes -- deterministic given the same "
        "input. A pair is a candidate only: it is not evidence of a "
        "transfer/virman or of beneficial ownership."
    ),
    "code_set_series": (
        "For a caller-supplied CodeSet (name, version, codes) and each "
        "server date t: total_lots(t) = sum of lot[code, t] over codes in "
        "the set that ARE listed at t (a missing code contributes nothing "
        "and is counted separately, never treated as a 0 lot balance); "
        "listed_count/missing_count partition the code set for that date; "
        "change(t) = total_lots(t) - total_lots(previous server date), "
        "None on the first server date."
    ),
}


@dataclass(frozen=True, slots=True)
class CodeResidual:
    code: str
    residual: Decimal | None


@dataclass(frozen=True, slots=True)
class CustodyResidualDay:
    """One custody date's residual row, per ``FORMULAS['custody_residual']``."""

    date: date
    previous_date: date
    akd_date: date
    rows: tuple[CodeResidual, ...]

    def by_code(self) -> dict[str, Decimal | None]:
        return {row.code: row.residual for row in self.rows}


@dataclass(frozen=True, slots=True)
class CustodyResidualResult:
    formula_version: str
    lag: int
    days: tuple[CustodyResidualDay, ...]


def compute_custody_residual(capture: PeriodCapture, *, lag: int = 2) -> CustodyResidualResult:
    """See ``FORMULAS['custody_residual']``. ``lag`` counts server-date steps."""
    if not isinstance(capture, PeriodCapture):
        raise TypeError("capture must be a PeriodCapture")
    if not isinstance(lag, int) or isinstance(lag, bool) or lag < 1:
        raise ValueError("lag must be a positive int")

    dates = capture.server_dates
    custody_by_date = {day.date: day.by_code() for day in capture.daily_custody_lots}
    akd_by_date = {day.date: day.by_code() for day in capture.daily_akd_net}

    days: list[CustodyResidualDay] = []
    for i in range(lag, len(dates)):
        t, p, a = dates[i], dates[i - 1], dates[i - lag]
        lot_t = custody_by_date.get(t, {})
        lot_p = custody_by_date.get(p, {})
        net_a = akd_by_date.get(a, {})
        rows = tuple(
            CodeResidual(
                code,
                None if code not in lot_t or code not in lot_p or code not in net_a
                else (lot_t[code] - lot_p[code]) - net_a[code],
            )
            for code in sorted(set(lot_t) | set(lot_p))
        )
        days.append(CustodyResidualDay(date=t, previous_date=p, akd_date=a, rows=rows))
    return CustodyResidualResult(formula_version=FORMULA_VERSION, lag=lag, days=tuple(days))


@dataclass(frozen=True, slots=True)
class SameDayPair:
    date: date
    code_a: str
    residual_a: Decimal
    code_b: str
    residual_b: Decimal


@dataclass(frozen=True, slots=True)
class SameDayPairResult:
    formula_version: str
    tolerance: Decimal
    pairs: tuple[SameDayPair, ...]


def find_same_day_pairs(
    residual_result: CustodyResidualResult, *, tolerance: Decimal = DEFAULT_PAIR_TOLERANCE,
) -> SameDayPairResult:
    """See ``FORMULAS['same_day_pairs']``. Candidates only -- see module docstring."""
    if not isinstance(residual_result, CustodyResidualResult):
        raise TypeError("residual_result must be a CustodyResidualResult")
    if not isinstance(tolerance, Decimal) or tolerance < 0:
        raise ValueError("tolerance must be a non-negative Decimal")

    pairs: list[SameDayPair] = []
    for day in residual_result.days:
        entries = sorted(
            (row.code, row.residual) for row in day.rows
            if row.residual is not None and row.residual != 0
        )
        matched: set[str] = set()
        for index, (code_a, residual_a) in enumerate(entries):
            if code_a in matched:
                continue
            for code_b, residual_b in entries[index + 1:]:
                if code_b in matched:
                    continue
                if abs(residual_a + residual_b) <= tolerance:
                    pairs.append(SameDayPair(day.date, code_a, residual_a, code_b, residual_b))
                    matched.add(code_a)
                    matched.add(code_b)
                    break
    return SameDayPairResult(formula_version=FORMULA_VERSION, tolerance=tolerance, pairs=tuple(pairs))


@dataclass(frozen=True, slots=True)
class CodeSet:
    """A caller-supplied, named and versioned group of brokerage codes.

    There is no built-in code set (e.g. no "retail" default); the caller
    must construct or load one explicitly.
    """

    name: str
    version: str
    codes: tuple[str, ...]


def load_code_set(path: Path) -> CodeSet:
    """Load a ``{"name", "version", "codes"}`` JSON file into a ``CodeSet``.

    Strict: ``codes`` must be a non-empty list of distinct, non-empty
    strings; ``name``/``version`` must be non-empty strings.
    """
    document: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("code set document must be a JSON object")
    name = document.get("name")
    version = document.get("version")
    codes = document.get("codes")
    if not isinstance(name, str) or not name:
        raise ValueError("code set 'name' must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise ValueError("code set 'version' must be a non-empty string")
    if not isinstance(codes, list) or not codes:
        raise ValueError("code set 'codes' must be a non-empty list")
    clean_codes: list[str] = []
    for code in codes:
        if not isinstance(code, str) or not code:
            raise ValueError("code set 'codes' entries must be non-empty strings")
        if code in clean_codes:
            raise ValueError(f"duplicate_code in code set: {code}")
        clean_codes.append(code)
    return CodeSet(name=name, version=version, codes=tuple(clean_codes))


@dataclass(frozen=True, slots=True)
class CodeSetDay:
    date: date
    total_lots: Decimal
    listed_count: int
    missing_count: int
    missing_codes: tuple[str, ...]
    change: Decimal | None


@dataclass(frozen=True, slots=True)
class CodeSetSeriesResult:
    formula_version: str
    code_set_name: str
    code_set_version: str
    codes: tuple[str, ...]
    days: tuple[CodeSetDay, ...]


def compute_code_set_series(capture: PeriodCapture, code_set: CodeSet) -> CodeSetSeriesResult:
    """See ``FORMULAS['code_set_series']``."""
    if not isinstance(capture, PeriodCapture):
        raise TypeError("capture must be a PeriodCapture")
    if not isinstance(code_set, CodeSet):
        raise TypeError("code_set must be a CodeSet")

    custody_by_date = {day.date: day.by_code() for day in capture.daily_custody_lots}
    days: list[CodeSetDay] = []
    previous_total: Decimal | None = None
    for t in capture.server_dates:
        lots = custody_by_date.get(t, {})
        listed = [code for code in code_set.codes if code in lots]
        missing = tuple(code for code in code_set.codes if code not in lots)
        total = sum((lots[code] for code in listed), Decimal(0))
        change = None if previous_total is None else total - previous_total
        days.append(CodeSetDay(
            date=t, total_lots=total, listed_count=len(listed),
            missing_count=len(missing), missing_codes=missing, change=change,
        ))
        previous_total = total
    return CodeSetSeriesResult(
        formula_version=FORMULA_VERSION, code_set_name=code_set.name,
        code_set_version=code_set.version, codes=code_set.codes, days=tuple(days),
    )
