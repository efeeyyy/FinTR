"""Freshness-aware assembly of normalized symbol snapshots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from fintr.data.models import (
    AKDSnapshot,
    CustodianSnapshot,
    CustodyDistributionSnapshot,
    CustodyHistory,
    CustodyStats,
    DepthSnapshot,
    MarketSnapshot,
)


SupportedComponent = (
    DepthSnapshot
    | AKDSnapshot
    | CustodianSnapshot
    | CustodyDistributionSnapshot
    | CustodyHistory
    | CustodyStats
)


@dataclass(slots=True)
class _SnapshotState:
    snapshot: MarketSnapshot
    freshness: dict[str, str]
    received_at: dict[str, datetime]


def _component_name(value: SupportedComponent) -> str:
    if isinstance(value, DepthSnapshot):
        return "depth"
    if isinstance(value, AKDSnapshot):
        return "akd"
    if isinstance(value, CustodianSnapshot):
        return "custody"
    if isinstance(value, CustodyDistributionSnapshot):
        return "custody_distribution"
    if isinstance(value, CustodyHistory):
        return "custody_history"
    if isinstance(value, CustodyStats):
        return "custody_stats"
    raise TypeError(f"unsupported normalized component: {type(value).__name__}")


def _latest_date(values: tuple[date, ...]) -> date | None:
    return max(values) if values else None


def _freshness(value: SupportedComponent, received_at: datetime) -> str:
    if isinstance(value, DepthSnapshot):
        return (value.timestamp or received_at).isoformat()
    if isinstance(value, AKDSnapshot | CustodianSnapshot):
        return value.date.isoformat()
    if isinstance(value, CustodyDistributionSnapshot):
        latest = _latest_date(value.periods)
        return (latest.isoformat() if latest is not None else received_at.isoformat())
    if isinstance(value, CustodyHistory):
        latest = _latest_date(value.dates)
        return (latest.isoformat() if latest is not None else received_at.isoformat())
    if isinstance(value, CustodyStats):
        dates = tuple(
            point.date
            for series in (
                value.retail_percentage,
                value.institution_percentage,
                value.investor_count,
            )
            for point in series
        )
        latest = _latest_date(dates)
        return (latest.isoformat() if latest is not None else received_at.isoformat())
    raise TypeError(f"unsupported normalized component: {type(value).__name__}")


class MarketSnapshotAssembler:
    """Combine normalized components without replacing newer data with stale data."""

    def __init__(self) -> None:
        self._states: dict[str, _SnapshotState] = {}

    def ingest(self, value: SupportedComponent, *, received_at: datetime) -> bool:
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        component = _component_name(value)
        symbol = value.symbol
        incoming_freshness = _freshness(value, received_at)
        state = self._states.get(symbol)

        if state is None:
            snapshot = MarketSnapshot(symbol=symbol, as_of=received_at)
            state = _SnapshotState(snapshot, {}, {})
            self._states[symbol] = state

        current_freshness = state.freshness.get(component)
        if current_freshness is not None and incoming_freshness < current_freshness:
            return False

        state.snapshot = replace(
            state.snapshot,
            as_of=max(state.snapshot.as_of, received_at),
            **{component: value},
        )
        state.freshness[component] = incoming_freshness
        state.received_at[component] = received_at
        return True

    def get(self, symbol: str) -> MarketSnapshot | None:
        state = self._states.get(symbol)
        return None if state is None else state.snapshot

    def component_received_at(self, symbol: str) -> dict[str, datetime]:
        state = self._states.get(symbol)
        return {} if state is None else dict(state.received_at)

    def freshness(self, symbol: str) -> dict[str, str]:
        state = self._states.get(symbol)
        return {} if state is None else dict(state.freshness)
