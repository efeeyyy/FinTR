"""Deterministic custody-statistic changes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fintr.data.models import CustodyStats, DatedCount, DatedMetric


@dataclass(frozen=True, slots=True)
class StatsFeatures:
    retail_share_change: Decimal | None
    institutional_share_change: Decimal | None
    investor_count_change: int | None


def _change(series: tuple[DatedMetric, ...]) -> Decimal | None:
    if len(series) < 2:
        return None
    ordered = sorted(series, key=lambda point: point.date)
    return ordered[-1].value - ordered[0].value


def _count_change(series: tuple[DatedCount, ...]) -> int | None:
    if len(series) < 2:
        return None
    ordered = sorted(series, key=lambda point: point.date)
    return ordered[-1].value - ordered[0].value


def calculate_stats_features(stats: CustodyStats) -> StatsFeatures:
    return StatsFeatures(
        retail_share_change=_change(stats.retail_percentage),
        institutional_share_change=_change(stats.institution_percentage),
        investor_count_change=_count_change(stats.investor_count),
    )
