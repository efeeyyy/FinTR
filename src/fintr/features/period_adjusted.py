"""Restate a period capture's lot quantities in post-corporate-action units.

Every value produced here is DERIVED: it applies one or more user-entered,
provenance-carrying ``fintr.data.corporate_actions.CorporateAction`` facts to
a raw ``fintr.data.period_capture.PeriodCapture``. Nothing here fetches or
verifies a corporate action itself -- see ``fintr.data.corporate_actions``
and ``docs/corporate_actions.md`` for how those facts enter the registry.

A corporate action (bonus issue, rights issue, split, reverse split, other)
changes the number of lots outstanding without changing the underlying
holding. Two clocks matter, and this project keeps them separate per the
project's AKD/custody data-family rule:

* ``ex_date`` -- the first trading date already reported in post-action
  units. It applies uniformly to every code's daily AKD ``net_size`` (and,
  for the daily-market-proxy hypothesis, ``total_size``) on and after that
  date; observations strictly before it are in pre-action units and need
  ``lot_multiplier`` applied to compare on equal footing with later dates.
* ``custody_effective_date`` -- the first custody date already reported in
  post-action units. Custody typically lags the exchange by one or more
  settlement days, so this is a SEPARATE, explicitly user-entered date, never
  derived from ``ex_date``.

Multiple actions on the same symbol compose: for a given date, the
multiplier applied is the product of ``lot_multiplier`` over every action
whose boundary date (``ex_date`` for the AKD/price family, or
``custody_effective_date`` for the custody family) is strictly after that
date. The registry loader rejects overlapping action windows (see
``fintr.data.corporate_actions.parse_corporate_action_registry``), so this
composition is unambiguous.

Everything here is a mechanical restatement or a data-vs-assertion
consistency check; none of it is investment advice, and a passing/failing
consistency check is not proof that the registered action (or the absence
of one) is correct -- only that this project's assumption about it agrees or
disagrees with the captured data.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from fintr.data.corporate_actions import CorporateAction
from fintr.data.period_capture import (
    CodeValue,
    CustodianDiffRow,
    DailyMarketProxyRow,
    DatedCodeValues,
    DatedMarketProxyValues,
    PeriodCapture,
)


FORMULA_VERSION = "period_adjusted_v1"

# Matches DEFAULT_DETAIL_LAG in fintr.features.period / scripts/analyze_period.py,
# so the consistency check below uses the same trade-to-custody settlement
# assumption as the rest of the lag-matching report by default. A parameter,
# not a hidden constant: callers may override it.
DEFAULT_CONSISTENCY_LAG = 2

FORMULAS: dict[str, str] = {
    "cumulative_multiplier": (
        "multiplier_akd(d) = product of action.lot_multiplier over every "
        "action with d < action.ex_date; multiplier_custody(d) = product of "
        "action.lot_multiplier over every action with d < "
        "action.custody_effective_date. Both are 1 (identity) when no such "
        "action exists. The registry rejects overlapping action windows, so "
        "this product is unambiguous for any date."
    ),
    "adjusted_daily_series": (
        "adjusted_daily_akd_net[code, d] = raw_daily_akd_net[code, d] * "
        "multiplier_akd(d); adjusted_daily_akd_total_size[code, d] = "
        "raw_total_size[code, d] * multiplier_akd(d) (total_cost, a price, "
        "is left unadjusted); adjusted_daily_custody_lots[code, d] = "
        "raw_daily_custody_lots[code, d] * multiplier_custody(d). A code not "
        "listed for a date stays not listed (never a fabricated 0)."
    ),
    "adjusted_period_akd_net": (
        "adjusted_period_akd_net[code] = sum_d adjusted_daily_akd_net[code, d] "
        "over listed days -- a recomputation, not the provider's own "
        "period-level get_akd_period.net.size, which is mixed-unit whenever "
        "the requested period spans an action's ex_date (see mixed_unit_period)."
    ),
    "adjusted_custodian_diff": (
        "When capture.start < action.custody_effective_date for at least one "
        "action (diff_start_multiplier != 1): adjusted_first_value = "
        "raw_first_value * multiplier_custody(capture.custodian_diff_start); "
        "adjusted_difference = raw_last_value - adjusted_first_value "
        "(last_value is left as reported, since the last server date is "
        "assumed already in current post-action units). When no action's "
        "custody_effective_date is after the period start, first_value and "
        "difference are unchanged from the raw section."
    ),
    "action_consistency_check": (
        "For an action whose custody_effective_date = t is a server date at "
        "index i (i >= lag, i >= 1), with p = server_dates[i-1] and "
        "a = server_dates[i-lag] (lag is a parameter, default "
        f"{DEFAULT_CONSISTENCY_LAG}): predicted[code] = action.lot_multiplier "
        "* raw_custody_lots[code, p] + raw_daily_akd_net[code, a], compared "
        "to actual = raw_custody_lots[code, t], for every code listed in "
        "custody at both p and t. 'pass' when actual == predicted exactly, "
        "'float_artifact_within_tolerance' when the difference is non-zero "
        "but within custody_float_tolerance, otherwise 'fail'. A code listed "
        "in custody but not in AKD at date a is counted separately as "
        "not_evaluated, never treated as a 0 net. This tests the "
        "user-entered action's multiplier and dates against the captured "
        "data; it is not independent proof the action fact is correct."
    ),
}


def cumulative_akd_multiplier(actions: tuple[CorporateAction, ...], on_date: date) -> Decimal:
    """See FORMULAS['cumulative_multiplier']; AKD/price-family boundary (ex_date)."""
    result = Decimal(1)
    for action in actions:
        if on_date < action.ex_date:
            result *= action.lot_multiplier
    return result


def cumulative_custody_multiplier(actions: tuple[CorporateAction, ...], on_date: date) -> Decimal:
    """See FORMULAS['cumulative_multiplier']; custody-family boundary (custody_effective_date)."""
    result = Decimal(1)
    for action in actions:
        if on_date < action.custody_effective_date:
            result *= action.lot_multiplier
    return result


def _applied_action_ids_akd(actions: tuple[CorporateAction, ...], on_date: date) -> tuple[str, ...]:
    return tuple(action.action_id for action in actions if on_date < action.ex_date)


def _applied_action_ids_custody(actions: tuple[CorporateAction, ...], on_date: date) -> tuple[str, ...]:
    return tuple(action.action_id for action in actions if on_date < action.custody_effective_date)


@dataclass(frozen=True, slots=True)
class AdjustmentFactor:
    date: date
    multiplier: Decimal
    action_ids: tuple[str, ...]  # actions contributing to this date's multiplier, by ex_date order


def _factor_series(
    dates: tuple[date, ...], actions: tuple[CorporateAction, ...], *, family: str,
) -> tuple[AdjustmentFactor, ...]:
    if family == "akd":
        multiplier_fn, ids_fn = cumulative_akd_multiplier, _applied_action_ids_akd
    else:
        multiplier_fn, ids_fn = cumulative_custody_multiplier, _applied_action_ids_custody
    return tuple(
        AdjustmentFactor(day, multiplier_fn(actions, day), ids_fn(actions, day)) for day in dates
    )


def _scale_dated_series(
    days: tuple[DatedCodeValues, ...], factors: dict[date, Decimal],
) -> tuple[DatedCodeValues, ...]:
    return tuple(
        DatedCodeValues(
            day.date,
            tuple(
                CodeValue(row.code, row.value * factors.get(day.date, Decimal(1)))
                for row in day.rows
            ),
        )
        for day in days
    )


def _scale_market_proxy(
    days: tuple[DatedMarketProxyValues, ...], factors: dict[date, Decimal],
) -> tuple[DatedMarketProxyValues, ...]:
    return tuple(
        DatedMarketProxyValues(
            day.date,
            tuple(
                DailyMarketProxyRow(row.code, row.total_size * factors.get(day.date, Decimal(1)),
                                    row.total_cost)
                for row in day.rows
            ),
        )
        for day in days
    )


def _period_net_from_daily(daily: tuple[DatedCodeValues, ...]) -> tuple[CodeValue, ...]:
    sums: dict[str, Decimal] = {}
    for day in daily:
        for row in day.rows:
            sums[row.code] = sums.get(row.code, Decimal(0)) + row.value
    return tuple(CodeValue(code, sums[code]) for code in sorted(sums))


def _adjust_custodian_diff(
    diff_rows: tuple[CustodianDiffRow, ...], diff_start_multiplier: Decimal,
) -> tuple[CustodianDiffRow, ...]:
    if diff_start_multiplier == 1:
        return diff_rows
    restated: list[CustodianDiffRow] = []
    for row in diff_rows:
        adjusted_first = row.first_value * diff_start_multiplier
        restated.append(CustodianDiffRow(
            code=row.code, first_value=adjusted_first, last_value=row.last_value,
            difference=row.last_value - adjusted_first,
        ))
    return tuple(restated)


@dataclass(frozen=True, slots=True)
class AdjustedCaptureBundle:
    """The restated capture plus the factor series/flags used to build it."""

    capture: PeriodCapture
    akd_factors: tuple[AdjustmentFactor, ...]
    custody_factors: tuple[AdjustmentFactor, ...]
    diff_restated: bool
    diff_start_multiplier: Decimal
    mixed_unit_period: bool  # True when the raw provider period figures span an ex_date


def build_adjusted_capture(
    capture: PeriodCapture, actions: tuple[CorporateAction, ...],
) -> AdjustedCaptureBundle:
    """Restate ``capture``'s lot-quantity series in post-action units.

    Only ``period_akd_net``, ``daily_akd_net``, ``daily_akd_market``,
    ``daily_custody_lots`` and ``custodian_diff`` change; everything else
    (metadata, brokerage directory, server dates, provenance) is carried
    over unchanged, since a corporate action does not touch those series.
    """
    if not isinstance(capture, PeriodCapture):
        raise TypeError("capture must be a PeriodCapture")
    if not actions:
        raise ValueError("actions must be a non-empty tuple")

    dates = capture.server_dates
    akd_factors = _factor_series(dates, actions, family="akd")
    custody_factors = _factor_series(dates, actions, family="custody")
    akd_factor_by_date = {factor.date: factor.multiplier for factor in akd_factors}
    custody_factor_by_date = {factor.date: factor.multiplier for factor in custody_factors}

    adjusted_daily_akd_net = _scale_dated_series(capture.daily_akd_net, akd_factor_by_date)
    adjusted_daily_akd_market = _scale_market_proxy(capture.daily_akd_market, akd_factor_by_date)
    adjusted_daily_custody_lots = _scale_dated_series(capture.daily_custody_lots, custody_factor_by_date)
    adjusted_period_akd_net = _period_net_from_daily(adjusted_daily_akd_net)

    diff_start_multiplier = cumulative_custody_multiplier(actions, capture.custodian_diff_start)
    diff_restated = diff_start_multiplier != 1
    adjusted_diff = _adjust_custodian_diff(capture.custodian_diff, diff_start_multiplier)

    mixed_unit_period = len({factor.multiplier for factor in akd_factors}) > 1

    adjusted_capture = replace(
        capture,
        period_akd_net=adjusted_period_akd_net,
        daily_akd_net=adjusted_daily_akd_net,
        daily_akd_market=adjusted_daily_akd_market,
        daily_custody_lots=adjusted_daily_custody_lots,
        custodian_diff=adjusted_diff,
    )
    return AdjustedCaptureBundle(
        capture=adjusted_capture,
        akd_factors=akd_factors,
        custody_factors=custody_factors,
        diff_restated=diff_restated,
        diff_start_multiplier=diff_start_multiplier,
        mixed_unit_period=mixed_unit_period,
    )


@dataclass(frozen=True, slots=True)
class ActionConsistencyRow:
    code: str
    predicted: Decimal
    actual: Decimal
    status: str  # pass | float_artifact_within_tolerance | fail


@dataclass(frozen=True, slots=True)
class ActionConsistencyCheck:
    """See FORMULAS['action_consistency_check']."""

    action_id: str
    custody_effective_date: date
    evaluated: bool  # False when custody_effective_date/lag land outside the capture's dates
    previous_custody_date: date | None
    trade_date: date | None
    lag: int
    custody_float_tolerance: Decimal
    rows: tuple[ActionConsistencyRow, ...]
    pass_count: int
    fail_count: int
    not_evaluated_code_count: int


def check_corporate_action_consistency(
    capture: PeriodCapture, actions: tuple[CorporateAction, ...], *,
    lag: int = DEFAULT_CONSISTENCY_LAG, custody_float_tolerance: Decimal,
) -> tuple[ActionConsistencyCheck, ...]:
    """Test each action's multiplier/dates against the RAW captured data.

    Uses raw (not adjusted) daily custody/AKD series deliberately: the check
    is whether the action, applied once at its own boundary, explains the
    observed jump in custody on ``custody_effective_date`` -- adjusting the
    series first would hide exactly the discontinuity being tested.
    """
    if not isinstance(capture, PeriodCapture):
        raise TypeError("capture must be a PeriodCapture")
    if type(lag) is not int or lag < 1:
        raise ValueError("lag must be a positive int")
    if not isinstance(custody_float_tolerance, Decimal) or custody_float_tolerance < 0:
        raise ValueError("custody_float_tolerance must be a non-negative Decimal")

    dates = capture.server_dates
    date_index = {day: index for index, day in enumerate(dates)}
    custody_by_date = {day.date: day.by_code() for day in capture.daily_custody_lots}
    akd_by_date = {day.date: day.by_code() for day in capture.daily_akd_net}

    results: list[ActionConsistencyCheck] = []
    for action in actions:
        t = action.custody_effective_date
        index = date_index.get(t)
        if index is None or index - 1 < 0 or index - lag < 0:
            results.append(ActionConsistencyCheck(
                action_id=action.action_id, custody_effective_date=t, evaluated=False,
                previous_custody_date=None, trade_date=None, lag=lag,
                custody_float_tolerance=custody_float_tolerance, rows=(),
                pass_count=0, fail_count=0, not_evaluated_code_count=0,
            ))
            continue
        previous_date = dates[index - 1]
        trade_date = dates[index - lag]
        previous_custody = custody_by_date.get(previous_date, {})
        current_custody = custody_by_date.get(t, {})
        akd_net = akd_by_date.get(trade_date, {})

        rows: list[ActionConsistencyRow] = []
        not_evaluated = 0
        for code in sorted(set(previous_custody) & set(current_custody)):
            if code not in akd_net:
                not_evaluated += 1
                continue
            predicted = action.lot_multiplier * previous_custody[code] + akd_net[code]
            actual = current_custody[code]
            residual = actual - predicted
            status = (
                "pass" if residual == 0
                else "float_artifact_within_tolerance" if abs(residual) <= custody_float_tolerance
                else "fail"
            )
            rows.append(ActionConsistencyRow(code, predicted, actual, status))

        pass_count = sum(1 for row in rows if row.status in ("pass", "float_artifact_within_tolerance"))
        fail_count = sum(1 for row in rows if row.status == "fail")
        results.append(ActionConsistencyCheck(
            action_id=action.action_id, custody_effective_date=t, evaluated=True,
            previous_custody_date=previous_date, trade_date=trade_date, lag=lag,
            custody_float_tolerance=custody_float_tolerance, rows=tuple(rows),
            pass_count=pass_count, fail_count=fail_count, not_evaluated_code_count=not_evaluated,
        ))
    return tuple(results)
