"""Deterministic broker-distribution (AKD) features."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fintr.data.models import AKDBroker, AKDSnapshot


@dataclass(frozen=True, slots=True)
class BrokerCostGap:
    code: str
    gap_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class AKDFeatures:
    top_buyers: tuple[AKDBroker, ...]
    top_sellers: tuple[AKDBroker, ...]
    top5_buy_concentration: Decimal | None
    top5_sell_concentration: Decimal | None
    broker_cost_vs_market_price: tuple[BrokerCostGap, ...]


def _concentration(values: list[int]) -> Decimal | None:
    total = sum(values)
    if total == 0:
        return None
    return Decimal(sum(sorted(values, reverse=True)[:5])) / Decimal(total)


def calculate_akd_features(snapshot: AKDSnapshot) -> AKDFeatures:
    buyers = tuple(
        sorted(
            (row for row in snapshot.brokers if row.net_lots > 0),
            key=lambda row: row.net_lots,
            reverse=True,
        )[:5]
    )
    sellers = tuple(
        sorted(
            (row for row in snapshot.brokers if row.net_lots < 0),
            key=lambda row: row.net_lots,
        )[:5]
    )
    cost_gaps: list[BrokerCostGap] = []
    for row in snapshot.brokers:
        gap: Decimal | None = None
        if snapshot.market_price not in (None, Decimal(0)) and row.average_cost is not None:
            gap = (row.average_cost - snapshot.market_price) / snapshot.market_price * Decimal(100)
        cost_gaps.append(BrokerCostGap(code=row.code, gap_pct=gap))

    return AKDFeatures(
        top_buyers=buyers,
        top_sellers=sellers,
        top5_buy_concentration=_concentration([row.buy_lots for row in snapshot.brokers]),
        top5_sell_concentration=_concentration([row.sell_lots for row in snapshot.brokers]),
        broker_cost_vs_market_price=tuple(cost_gaps),
    )
