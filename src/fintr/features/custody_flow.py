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
* ``compute_estimated_retail_lots`` -- an estimated retail (bireysel) and
  institutional (kurumsal) lot series computed from the provider's own
  ``retail_pct``/``institutional_pct`` metadata field applied to that same
  server date's total custody lots (see ``RETAIL_LOT_LABEL`` and
  ``RETAIL_LOT_VALIDATION_NOTE`` for the caveats and provenance).

Custody-lot inputs are never defaulted: a residual whose custody lot or
previous custody lot is missing for that code/date is ``None``, not ``0``.

AKD-net inputs follow an explicit, caller-visible ``missing_akd_policy`` on
``compute_custody_residual`` (see ``MISSING_AKD_POLICIES``):

* ``"zero"`` (default) -- if the AKD panel for the anchor server date exists
  at all but a given code is not listed in it, that code's AKD net is
  treated as 0 for that date. This is a recorded decision by the project
  owner (Efe), dated 2026-09-26: see ``MISSING_AKD_ZERO_EVIDENCE`` for the
  evidence and ``docs/custody_flow.md`` for the full writeup. It does NOT
  apply when the whole day's AKD panel is absent from the capture -- that
  case is always ``None``, under either policy, because there is then no
  panel to have omitted the code from.
* ``"unknown"`` -- the previous behaviour: any AKD-net input that is not
  listed for that code/date, whether or not the day's panel exists, is
  ``None``.

Every ``CustodyResidualResult`` carries the ``missing_akd_policy`` that
produced it and the ``formula_version`` (``FORMULA_VERSION``); every
``CodeResidual`` row records whether its AKD input was defaulted to 0 under
the "zero" policy (``akd_defaulted``).

``compute_estimated_retail_lots`` derives a lot-scale estimate of the
provider's percentage split; it never invents the split itself. The label
(``RETAIL_LOT_LABEL``, Turkish, carried verbatim on every result) makes clear
this is a computed value, not a reported one, and that the underlying
bireysel/kurumsal classification is the provider's, with an unverified
method. It carries a validation status (``RETAIL_LOT_VALIDATION_STATUS``)
pointing at an offline single-symbol study
(``RETAIL_LOT_VALIDATION_STUDY_FILE``, name only -- see
``docs/custody_flow.md``); a custody lot-scale change (e.g. a corporate
action) changes lot *levels* but, per that study, not the retail/institutional
*ratio*, since the estimate is a percentage of the same date's total, not an
absolute count. The series also carries the ``provisional_last_custody_days``
policy (same meaning as ``fintr.features.period``): the most recent that many
server dates are flagged ``provisional`` because custody balances there have
been observed to be revised later.

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
from fintr.features.period import DEFAULT_PROVISIONAL_LAST_CUSTODY_DAYS


FORMULA_VERSION = "custody_flow_v3"

# Exact-equality by default: the scratch script this reproduces used exact
# cancellation (v2 == -v1). Exposed as a parameter, never a hidden default,
# for callers whose lot values carry float-ish noise.
DEFAULT_PAIR_TOLERANCE = Decimal(0)

MISSING_AKD_POLICIES = ("zero", "unknown")
DEFAULT_MISSING_AKD_POLICY = "zero"

MISSING_AKD_ZERO_EVIDENCE = (
    "Decision by Efe (project owner), recorded 2026-09-26: a broker/code "
    "absent from a server date's AKD list is treated as net 0 for that "
    "date, when the AKD panel for that date is itself present in the "
    "capture. Evidence: in both saved AKFIS captures, the listed brokers' "
    "daily net_size sums to exactly 0 on every server date (the "
    "period_features 'net_sum_zero' check passed on all days) -- a result "
    "inconsistent with the list being truncated/partial, and consistent "
    "with the list being exhaustive so that an unlisted code genuinely "
    "traded zero net that day. This does not extend to a server date whose "
    "AKD panel is missing entirely from the capture; that stays None under "
    "either policy."
)

RETAIL_LOT_LABEL = (
    "Tahmini bireysel lot (hesaplanan): sağlayıcı retail_pct × aynı saklama "
    "tarihindeki toplam saklama lotu / 100. Bireysel/kurumsal sınıflaması "
    "sağlayıcıya aittir; yöntem belgesi doğrulanmadı."
)

RETAIL_LOT_VALIDATION_STATUS = "provider_method_consistent_single_symbol_study"

# Name only, per project policy: the study's numbers are not reproduced here.
RETAIL_LOT_VALIDATION_STUDY_FILE = "data/interim/research/retail_pct_validation.md"

RETAIL_LOT_VALIDATION_NOTE = (
    "Validation status 'provider_method_consistent_single_symbol_study': an "
    "offline research study (see RETAIL_LOT_VALIDATION_STUDY_FILE for the "
    "file name, not reproduced here) found this lag-0, same-server-date "
    "percentage-of-total-custody construction consistent with the "
    "provider's own retail_pct series, but tested on ONE symbol and one "
    "capture period only -- not a general guarantee for other symbols or "
    "periods. A custody lot-scale change (e.g. a corporate action / split) "
    "changes the absolute lot levels used here but, per that study, does "
    "not by itself change the retail/institutional ratio, since both the "
    "numerator and denominator scale together."
)

FORMULAS: dict[str, str] = {
    "custody_residual": (
        "For server-date index i >= lag, with t = server_dates[i], "
        "p = server_dates[i-1], a = server_dates[i-lag]: "
        "residual(code, t) = (lot[code, t] - lot[code, p]) - akd_net(code, a). "
        "residual(code, t) is None if lot[code, t] or lot[code, p] is not "
        "listed for that code on that date (custody inputs are never "
        "defaulted). akd_net(code, a) depends on missing_akd_policy: if the "
        "AKD panel for date a is entirely absent from the capture, "
        "akd_net(code, a) is undefined and residual is None regardless of "
        "policy; if the panel for a exists but code is not listed in it, "
        "akd_net(code, a) is 0 under policy='zero' (default; see "
        "MISSING_AKD_ZERO_EVIDENCE) or undefined (residual is None) under "
        "policy='unknown'. Computed for the union of codes listed in "
        "custody at t or at p."
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
    "estimated_retail_lots": (
        "For each server date t, with total(t) = sum of custody_lots(c, t) "
        "over ALL codes listed in the custody panel for t (no lag, no "
        "forward-fill): estimated_retail_lots(t) = retail_pct(t) / 100 * "
        "total(t), and estimated_institutional_lots(t) = "
        "institutional_pct(t) / 100 * total(t). Both are None if the "
        "corresponding percentage is null for t OR the day's custody panel "
        "is absent from the capture (total(t) is then undefined). See "
        "RETAIL_LOT_LABEL and RETAIL_LOT_VALIDATION_NOTE."
    ),
}


@dataclass(frozen=True, slots=True)
class CodeResidual:
    code: str
    residual: Decimal | None
    # True only when this row's AKD-net input was missing from an otherwise
    # present AKD panel and defaulted to 0 under missing_akd_policy='zero'.
    akd_defaulted: bool = False


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
    missing_akd_policy: str
    days: tuple[CustodyResidualDay, ...]


def compute_custody_residual(
    capture: PeriodCapture,
    *,
    lag: int = 2,
    missing_akd_policy: str = DEFAULT_MISSING_AKD_POLICY,
) -> CustodyResidualResult:
    """See ``FORMULAS['custody_residual']``. ``lag`` counts server-date steps.

    ``missing_akd_policy`` must be one of ``MISSING_AKD_POLICIES``
    (``"zero"`` default, or ``"unknown"``); see the module docstring and
    ``MISSING_AKD_ZERO_EVIDENCE``.
    """
    if not isinstance(capture, PeriodCapture):
        raise TypeError("capture must be a PeriodCapture")
    if not isinstance(lag, int) or isinstance(lag, bool) or lag < 1:
        raise ValueError("lag must be a positive int")
    if missing_akd_policy not in MISSING_AKD_POLICIES:
        raise ValueError(
            f"missing_akd_policy must be one of {MISSING_AKD_POLICIES}, got {missing_akd_policy!r}"
        )

    dates = capture.server_dates
    custody_by_date = {day.date: day.by_code() for day in capture.daily_custody_lots}
    akd_by_date = {day.date: day.by_code() for day in capture.daily_akd_net}

    days: list[CustodyResidualDay] = []
    for i in range(lag, len(dates)):
        t, p, a = dates[i], dates[i - 1], dates[i - lag]
        lot_t = custody_by_date.get(t, {})
        lot_p = custody_by_date.get(p, {})
        akd_panel_present = a in akd_by_date
        net_a = akd_by_date.get(a, {})

        rows_list: list[CodeResidual] = []
        for code in sorted(set(lot_t) | set(lot_p)):
            if code not in lot_t or code not in lot_p:
                rows_list.append(CodeResidual(code, None))
                continue
            if code in net_a:
                akd_value, akd_defaulted = net_a[code], False
            elif akd_panel_present and missing_akd_policy == "zero":
                akd_value, akd_defaulted = Decimal(0), True
            else:
                rows_list.append(CodeResidual(code, None))
                continue
            rows_list.append(
                CodeResidual(code, (lot_t[code] - lot_p[code]) - akd_value, akd_defaulted)
            )
        days.append(CustodyResidualDay(date=t, previous_date=p, akd_date=a, rows=tuple(rows_list)))
    return CustodyResidualResult(
        formula_version=FORMULA_VERSION, lag=lag,
        missing_akd_policy=missing_akd_policy, days=tuple(days),
    )


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


@dataclass(frozen=True, slots=True)
class RetailLotDay:
    """See ``FORMULAS['estimated_retail_lots']``."""

    date: date
    total_custody_lots: Decimal | None
    retail_pct: Decimal | None
    institutional_pct: Decimal | None
    estimated_retail_lots: Decimal | None
    estimated_institutional_lots: Decimal | None
    provisional: bool


@dataclass(frozen=True, slots=True)
class RetailLotSeriesResult:
    formula_version: str
    label: str
    validation_status: str
    validation_study_file: str
    validation_note: str
    provisional_last_custody_days: int
    days: tuple[RetailLotDay, ...]


def compute_estimated_retail_lots(
    capture: PeriodCapture,
    *,
    provisional_last_custody_days: int = DEFAULT_PROVISIONAL_LAST_CUSTODY_DAYS,
) -> RetailLotSeriesResult:
    """See ``FORMULAS['estimated_retail_lots']``.

    ``provisional_last_custody_days`` marks the most recent that many server
    dates as ``provisional`` (same policy as
    ``fintr.features.period.check_custodian_diff``): 0 disables marking.
    """
    if not isinstance(capture, PeriodCapture):
        raise TypeError("capture must be a PeriodCapture")
    if type(provisional_last_custody_days) is not int or provisional_last_custody_days < 0:
        raise ValueError("provisional_last_custody_days must be a non-negative integer")

    custody_by_date = {day.date: day.by_code() for day in capture.daily_custody_lots}
    metadata_by_date = {day.date: day for day in capture.metadata}
    provisional_dates = set(
        capture.server_dates[-provisional_last_custody_days:]
        if provisional_last_custody_days else ()
    )

    days: list[RetailLotDay] = []
    for t in capture.server_dates:
        lots = custody_by_date.get(t)
        total = sum(lots.values(), Decimal(0)) if lots is not None else None
        meta = metadata_by_date.get(t)
        retail_pct = meta.retail_pct if meta is not None else None
        institutional_pct = meta.institutional_pct if meta is not None else None
        estimated_retail = (
            None if retail_pct is None or total is None else (retail_pct / Decimal(100)) * total
        )
        estimated_institutional = (
            None if institutional_pct is None or total is None
            else (institutional_pct / Decimal(100)) * total
        )
        days.append(RetailLotDay(
            date=t, total_custody_lots=total, retail_pct=retail_pct,
            institutional_pct=institutional_pct,
            estimated_retail_lots=estimated_retail,
            estimated_institutional_lots=estimated_institutional,
            provisional=t in provisional_dates,
        ))
    return RetailLotSeriesResult(
        formula_version=FORMULA_VERSION, label=RETAIL_LOT_LABEL,
        validation_status=RETAIL_LOT_VALIDATION_STATUS,
        validation_study_file=RETAIL_LOT_VALIDATION_STUDY_FILE,
        validation_note=RETAIL_LOT_VALIDATION_NOTE,
        provisional_last_custody_days=provisional_last_custody_days,
        days=tuple(days),
    )
