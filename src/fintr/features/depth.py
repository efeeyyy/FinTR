"""Deterministic order-book features."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fintr.data.models import DepthSnapshot


@dataclass(frozen=True, slots=True)
class DepthFeatures:
    total_bid_lots: int
    total_ask_lots: int
    bid_ask_imbalance: Decimal | None
    top5_bid_lots: int
    top5_ask_lots: int
    spread: Decimal | None
    spread_pct: Decimal | None


def calculate_depth_features(snapshot: DepthSnapshot) -> DepthFeatures:
    bids = sorted(snapshot.bids, key=lambda level: level.price, reverse=True)
    asks = sorted(snapshot.asks, key=lambda level: level.price)
    total_bid_lots = sum(level.lots for level in bids)
    total_ask_lots = sum(level.lots for level in asks)
    denominator = total_bid_lots + total_ask_lots
    imbalance = (
        Decimal(total_bid_lots - total_ask_lots) / Decimal(denominator)
        if denominator
        else None
    )

    spread: Decimal | None = None
    spread_pct: Decimal | None = None
    if bids and asks:
        spread = asks[0].price - bids[0].price
        midpoint = (asks[0].price + bids[0].price) / Decimal(2)
        if midpoint != 0:
            spread_pct = spread / midpoint * Decimal(100)

    return DepthFeatures(
        total_bid_lots=total_bid_lots,
        total_ask_lots=total_ask_lots,
        bid_ask_imbalance=imbalance,
        top5_bid_lots=sum(level.lots for level in bids[:5]),
        top5_ask_lots=sum(level.lots for level in asks[:5]),
        spread=spread,
        spread_pct=spread_pct,
    )
