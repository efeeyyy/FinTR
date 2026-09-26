"""Deterministic features over the live (WebSocket) response models.

These functions are pure and operate only on ``Live*`` models from
``fintr.data.models`` (see ``fintr.collector.evidence.LiveCollectionEvidence``
and ``fintr.inference.live_context.build_live_analysis_context``). They are
distinct from ``fintr.features.akd``/``custody``/``stats``, which compute
features for the legacy snapshot models (``AKDSnapshot``, ``CustodianSnapshot``,
``CustodyDistributionSnapshot``, ``CustodyStats``) and are left unchanged.

Every ratio is ``None`` when its denominator is zero; a zero denominator is
never treated as a zero ratio. Sums that are reported "as received" are
integrity observations only: they are never forced to zero or otherwise
adjusted, even where a zero sum might be expected in a well-formed response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fintr.data.models import (
    LiveAKDResponse,
    LiveCustodianResponse,
    LiveCustodyDistributionResponse,
    LiveCustodyStatsResponse,
    LiveCustodyStatsSeries,
)


FORMULA_VERSION = "live_features_v1"

FORMULAS = {
    "live_akd_positive_net_sum": "sum(net.size for rows with net.size > 0)",
    "live_akd_negative_net_sum": "sum(net.size for rows with net.size < 0)",
    "live_akd_net_sum_as_received": (
        "sum(net.size for all rows); an integrity observation, never forced to 0"
    ),
    "live_akd_top5_positive_net_share": (
        "sum(5 largest positive net.size) / positive_net_sum; None if positive_net_sum == 0"
    ),
    "live_akd_top5_negative_net_share": (
        "sum(abs(5 most negative net.size)) / abs(negative_net_sum); "
        "None if negative_net_sum == 0"
    ),
    "live_custodian_total_value": "sum(value for all rows)",
    "live_custodian_top5_value_share": (
        "sum(5 largest value) / total_value; None if total_value == 0"
    ),
    "live_custodian_percentage_sum_as_received": (
        "sum(percentage for all rows); an integrity observation, never forced to 0"
    ),
    "live_distribution_change": (
        "value at the latest period (by date, not tuple position) minus value at "
        "each other period; None if either side is None"
    ),
    "live_stats_series_change": (
        "last non-null value minus first non-null value, points ordered by date; "
        "None if fewer than 2 non-null points"
    ),
}


@dataclass(frozen=True, slots=True)
class LiveAKDComputedFeatures:
    row_count: int
    positive_net_count: int
    negative_net_count: int
    zero_net_count: int
    positive_net_sum: int
    negative_net_sum: int
    net_sum_as_received: int
    top5_positive_net_share: Decimal | None
    top5_negative_net_share: Decimal | None


@dataclass(frozen=True, slots=True)
class LiveCustodianComputedFeatures:
    row_count: int
    total_value: Decimal
    top5_value_share: Decimal | None
    percentage_sum_as_received: Decimal


@dataclass(frozen=True, slots=True)
class LiveDistributionPeriodChange:
    period: date
    change: Decimal | None


@dataclass(frozen=True, slots=True)
class LiveDistributionRowFeatures:
    code: str
    changes: tuple[LiveDistributionPeriodChange, ...]


@dataclass(frozen=True, slots=True)
class LiveCustodyDistributionComputedFeatures:
    row_count: int
    period_count: int
    latest_period: date
    rows: tuple[LiveDistributionRowFeatures, ...]


@dataclass(frozen=True, slots=True)
class LiveStatsSeriesChange:
    change: Decimal | None
    first_date: date | None
    last_date: date | None
    non_null_point_count: int


@dataclass(frozen=True, slots=True)
class LiveCustodyStatsComputedFeatures:
    retail_percentage: LiveStatsSeriesChange
    institution_percentage: LiveStatsSeriesChange
    investor_count: LiveStatsSeriesChange


def calculate_live_akd_features(response: LiveAKDResponse) -> LiveAKDComputedFeatures:
    if not isinstance(response, LiveAKDResponse):
        raise TypeError("response must be LiveAKDResponse")
    nets = [row.net.size for row in response.results]
    positive = [value for value in nets if value > 0]
    negative = [value for value in nets if value < 0]
    zero = [value for value in nets if value == 0]
    positive_sum = sum(positive)
    negative_sum = sum(negative)
    top5_positive_share: Decimal | None = None
    if positive_sum != 0:
        top5_positive = sum(sorted(positive, reverse=True)[:5])
        top5_positive_share = Decimal(top5_positive) / Decimal(positive_sum)
    top5_negative_share: Decimal | None = None
    if negative_sum != 0:
        abs_negative = sorted((-value for value in negative), reverse=True)
        top5_negative_share = Decimal(sum(abs_negative[:5])) / Decimal(-negative_sum)
    return LiveAKDComputedFeatures(
        row_count=len(nets),
        positive_net_count=len(positive),
        negative_net_count=len(negative),
        zero_net_count=len(zero),
        positive_net_sum=positive_sum,
        negative_net_sum=negative_sum,
        net_sum_as_received=sum(nets),
        top5_positive_net_share=top5_positive_share,
        top5_negative_net_share=top5_negative_share,
    )


def calculate_live_custodian_features(
    response: LiveCustodianResponse,
) -> LiveCustodianComputedFeatures:
    if not isinstance(response, LiveCustodianResponse):
        raise TypeError("response must be LiveCustodianResponse")
    values = [row.value for row in response.results]
    total = sum(values, Decimal(0))
    top5_share: Decimal | None = None
    if total != 0:
        top5 = sum(sorted(values, reverse=True)[:5], Decimal(0))
        top5_share = top5 / total
    percentage_sum = sum((row.percentage for row in response.results), Decimal(0))
    return LiveCustodianComputedFeatures(
        row_count=len(values),
        total_value=total,
        top5_value_share=top5_share,
        percentage_sum_as_received=percentage_sum,
    )


def calculate_live_distribution_features(
    response: LiveCustodyDistributionResponse,
) -> LiveCustodyDistributionComputedFeatures:
    if not isinstance(response, LiveCustodyDistributionResponse):
        raise TypeError("response must be LiveCustodyDistributionResponse")
    periods = response.periods
    if not periods:
        raise ValueError("live_distribution_no_periods")
    ordered_periods = sorted(range(len(periods)), key=lambda index: periods[index])
    latest_index = ordered_periods[-1]
    latest_period = periods[latest_index]
    other_indices = [index for index in ordered_periods if index != latest_index]

    rows: list[LiveDistributionRowFeatures] = []
    for row in response.rows:
        if len(row.values) != len(periods):
            raise ValueError("live_distribution_length_mismatch")
        latest_value = row.values[latest_index]
        changes = []
        for index in other_indices:
            other_value = row.values[index]
            change = (
                None
                if latest_value is None or other_value is None
                else latest_value - other_value
            )
            changes.append(LiveDistributionPeriodChange(period=periods[index], change=change))
        rows.append(LiveDistributionRowFeatures(code=row.code, changes=tuple(changes)))

    return LiveCustodyDistributionComputedFeatures(
        row_count=len(response.rows),
        period_count=len(periods),
        latest_period=latest_period,
        rows=tuple(rows),
    )


def _series_change(series: LiveCustodyStatsSeries) -> LiveStatsSeriesChange:
    non_null = sorted(
        (point for point in series.points if point.value is not None),
        key=lambda point: point.date,
    )
    if len(non_null) < 2:
        return LiveStatsSeriesChange(
            change=None,
            first_date=non_null[0].date if non_null else None,
            last_date=non_null[-1].date if non_null else None,
            non_null_point_count=len(non_null),
        )
    return LiveStatsSeriesChange(
        change=non_null[-1].value - non_null[0].value,
        first_date=non_null[0].date,
        last_date=non_null[-1].date,
        non_null_point_count=len(non_null),
    )


def calculate_live_stats_features(
    response: LiveCustodyStatsResponse,
) -> LiveCustodyStatsComputedFeatures:
    if not isinstance(response, LiveCustodyStatsResponse):
        raise TypeError("response must be LiveCustodyStatsResponse")
    return LiveCustodyStatsComputedFeatures(
        retail_percentage=_series_change(response.retail_percentage),
        institution_percentage=_series_change(response.institution_percentage),
        investor_count=_series_change(response.investor_count),
    )
