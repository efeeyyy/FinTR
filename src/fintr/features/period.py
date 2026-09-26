"""Deterministic checks over one saved AKD / daily custody / custody-diff period.

Observation index ``t`` means the t-th provider server date in ascending order;
it is a trading-observation step, not a calendar day. Values keep the units the
provider reported. None of these results identifies an ultimate investor or a
cause; they are consistency and timing observations only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fintr.data.period_capture import PeriodCapture


FORMULA_VERSION = "period_features_v1"

FORMULAS: dict[str, str] = {
    "akd_reconciliation": (
        "daily_sum[code] = sum_t daily_akd.net_size[code, t] over listed days; "
        "match when daily_sum[code] == get_akd_period.net.size[code]"
    ),
    "custodian_diff_internal": (
        "consistent when last_value - first_value == difference"
    ),
    "custodian_diff_vs_daily": (
        "first_value vs daily lot_count on the first server date and last_value vs "
        "daily lot_count on the last server date; a code not listed that day is "
        "reported as absent, never as zero"
    ),
    "flow_runs": (
        "consecutive listed server dates whose daily AKD net_size has the same sign; "
        "a date on which the code is not listed ends the run"
    ),
    "lagged_match": (
        "custody_change[code, t+lag] = lot_count[code, t+lag] - lot_count[code, t+lag-1]; "
        "comparable when both balances and akd net_size[code, t] are listed; "
        "exact when custody_change == net_size; sign when both signs are equal"
    ),
}


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


@dataclass(frozen=True, slots=True)
class AKDReconciliationRow:
    code: str
    period_net: Decimal | None
    daily_sum: Decimal | None
    days_listed: int
    status: str  # match | mismatch | missing_in_daily | missing_in_period


def reconcile_daily_akd(capture: PeriodCapture) -> tuple[AKDReconciliationRow, ...]:
    period = {row.code: row.value for row in capture.period_akd_net}
    sums: dict[str, Decimal] = {}
    days: dict[str, int] = {}
    for day in capture.daily_akd_net:
        for row in day.rows:
            sums[row.code] = sums.get(row.code, Decimal(0)) + row.value
            days[row.code] = days.get(row.code, 0) + 1
    rows: list[AKDReconciliationRow] = []
    for code in sorted(period.keys() | sums.keys()):
        period_net, daily_sum = period.get(code), sums.get(code)
        status = (
            "missing_in_daily" if daily_sum is None
            else "missing_in_period" if period_net is None
            else "match" if period_net == daily_sum
            else "mismatch"
        )
        rows.append(AKDReconciliationRow(code, period_net, daily_sum, days.get(code, 0), status))
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class CustodianDiffCheckRow:
    code: str
    reported_difference: Decimal
    internally_consistent: bool
    first_value: Decimal
    daily_first_value: Decimal | None
    first_status: str  # match | mismatch | absent_in_daily_panel
    last_value: Decimal
    daily_last_value: Decimal | None
    last_status: str


@dataclass(frozen=True, slots=True)
class CustodianDiffCheck:
    daily_first_date: date | None
    daily_last_date: date | None
    range_aligned: bool
    rows: tuple[CustodianDiffCheckRow, ...]


def _balance_status(reported: Decimal, daily: Decimal | None) -> str:
    if daily is None:
        return "absent_in_daily_panel"
    return "match" if reported == daily else "mismatch"


def check_custodian_diff(capture: PeriodCapture) -> CustodianDiffCheck:
    custody = capture.daily_custody_lots
    first = custody[0].by_code() if custody else {}
    last = custody[-1].by_code() if custody else {}
    first_date = custody[0].date if custody else None
    last_date = custody[-1].date if custody else None
    rows = tuple(
        CustodianDiffCheckRow(
            code=row.code,
            reported_difference=row.difference,
            internally_consistent=row.last_value - row.first_value == row.difference,
            first_value=row.first_value,
            daily_first_value=first.get(row.code),
            first_status=_balance_status(row.first_value, first.get(row.code)),
            last_value=row.last_value,
            daily_last_value=last.get(row.code),
            last_status=_balance_status(row.last_value, last.get(row.code)),
        )
        for row in capture.custodian_diff
    )
    return CustodianDiffCheck(
        first_date, last_date,
        first_date == capture.custodian_diff_start and last_date == capture.custodian_diff_end,
        rows,
    )


@dataclass(frozen=True, slots=True)
class FlowRun:
    start: date
    end: date
    days: int
    direction: str  # positive | negative | zero
    net_sum: Decimal


@dataclass(frozen=True, slots=True)
class BrokerFlowSeries:
    code: str
    points: tuple[tuple[date, Decimal | None], ...]  # None: not listed that date
    listed_sum: Decimal
    runs: tuple[FlowRun, ...]


_DIRECTIONS = {1: "positive", -1: "negative", 0: "zero"}


def broker_flow_series(capture: PeriodCapture) -> tuple[BrokerFlowSeries, ...]:
    daily = {day.date: day.by_code() for day in capture.daily_akd_net}
    dates = capture.server_dates
    codes = sorted({code for values in daily.values() for code in values})
    result: list[BrokerFlowSeries] = []
    for code in codes:
        points = tuple((day, daily.get(day, {}).get(code)) for day in dates)
        runs: list[FlowRun] = []
        current: FlowRun | None = None
        for day, value in points:
            if value is None:
                if current is not None:
                    runs.append(current)
                current = None
                continue
            direction = _DIRECTIONS[_sign(value)]
            if current is not None and current.direction == direction:
                current = FlowRun(current.start, day, current.days + 1, direction,
                                  current.net_sum + value)
            else:
                if current is not None:
                    runs.append(current)
                current = FlowRun(day, day, 1, direction, value)
        if current is not None:
            runs.append(current)
        result.append(BrokerFlowSeries(
            code, points,
            sum((value for _, value in points if value is not None), Decimal(0)),
            tuple(runs),
        ))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class LagSummary:
    lag: int
    comparable: int
    exact_matches: int
    sign_matches: int


@dataclass(frozen=True, slots=True)
class LagObservation:
    code: str
    akd_date: date
    custody_date: date
    akd_net: Decimal
    custody_change: Decimal


@dataclass(frozen=True, slots=True)
class LagAnalysis:
    summaries: tuple[LagSummary, ...]
    detail_lag: int
    unmatched: tuple[LagObservation, ...]
    # AKD dates that have no custody observation at t+detail_lag in this capture.
    pending_akd_dates: tuple[date, ...]


def _lag_observations(capture: PeriodCapture, lag: int) -> list[LagObservation]:
    dates = capture.server_dates
    akd = {day.date: day.by_code() for day in capture.daily_akd_net}
    custody = {day.date: day.by_code() for day in capture.daily_custody_lots}
    observations: list[LagObservation] = []
    for index, akd_date in enumerate(dates):
        target = index + lag
        if target < 1 or target >= len(dates):
            continue
        current = custody.get(dates[target], {})
        previous = custody.get(dates[target - 1], {})
        for code, net in sorted(akd.get(akd_date, {}).items()):
            if code in current and code in previous:
                observations.append(LagObservation(
                    code, akd_date, dates[target], net, current[code] - previous[code],
                ))
    return observations


def lagged_akd_custody_match(
    capture: PeriodCapture, *, lags: tuple[int, ...], detail_lag: int,
) -> LagAnalysis:
    """Count exact/sign agreement between AKD net and a later custody change."""
    if not lags or any(type(lag) is not int or lag < 0 for lag in (*lags, detail_lag)):
        raise ValueError("lags must be non-negative integers")
    summaries: list[LagSummary] = []
    for lag in lags:
        observations = _lag_observations(capture, lag)
        summaries.append(LagSummary(
            lag, len(observations),
            sum(item.custody_change == item.akd_net for item in observations),
            sum(_sign(item.custody_change) == _sign(item.akd_net) for item in observations),
        ))
    detail = _lag_observations(capture, detail_lag)
    dates = capture.server_dates
    return LagAnalysis(
        summaries=tuple(summaries),
        detail_lag=detail_lag,
        unmatched=tuple(item for item in detail if item.custody_change != item.akd_net),
        pending_akd_dates=tuple(
            day for index, day in enumerate(dates) if index + detail_lag >= len(dates)
        ),
    )


@dataclass(frozen=True, slots=True)
class CodeCoverage:
    code: str
    akd_missing_dates: tuple[date, ...]
    custody_missing_dates: tuple[date, ...]


def daily_panel_coverage(capture: PeriodCapture) -> tuple[CodeCoverage, ...]:
    """List server dates on which a code seen elsewhere in the period is not listed."""
    akd = {day.date: day.by_code() for day in capture.daily_akd_net}
    custody = {day.date: day.by_code() for day in capture.daily_custody_lots}
    codes = sorted(
        {code for values in akd.values() for code in values}
        | {code for values in custody.values() for code in values}
    )
    return tuple(
        CodeCoverage(
            code,
            tuple(day for day in capture.server_dates if code not in akd.get(day, {})),
            tuple(day for day in capture.server_dates if code not in custody.get(day, {})),
        )
        for code in codes
    )
