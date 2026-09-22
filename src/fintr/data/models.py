"""Immutable normalized models for BIST market microstructure data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class DepthLevel:
    price: Decimal
    lots: int
    order_count: int | None = None


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    symbol: str
    timestamp: datetime | None
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]


@dataclass(frozen=True, slots=True)
class AKDBroker:
    code: str
    buy_lots: int
    sell_lots: int
    net_lots: int
    average_cost: Decimal | None = None
    buy_value: Decimal | None = None
    sell_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AKDSnapshot:
    symbol: str
    date: date
    market_price: Decimal | None
    brokers: tuple[AKDBroker, ...]


@dataclass(frozen=True, slots=True)
class CustodianRow:
    code: str
    value: int
    percentage: Decimal


@dataclass(frozen=True, slots=True)
class CustodianSnapshot:
    symbol: str
    date: date
    rows: tuple[CustodianRow, ...]


@dataclass(frozen=True, slots=True)
class CustodianDiffRow:
    code: str
    first_value: int
    last_value: int
    difference: int
    first_percentage: Decimal
    last_percentage: Decimal


@dataclass(frozen=True, slots=True)
class CustodianDiffSnapshot:
    symbol: str
    first_date: date
    last_date: date
    rows: tuple[CustodianDiffRow, ...]


@dataclass(frozen=True, slots=True)
class CustodyDistributionRow:
    code: str
    values: tuple[int, int, int, int, int]
    percentage: Decimal


@dataclass(frozen=True, slots=True)
class CustodyDistributionSnapshot:
    symbol: str
    periods: tuple[date, ...]
    rows: tuple[CustodyDistributionRow, ...]


@dataclass(frozen=True, slots=True)
class CustodyHistoryPoint:
    symbol: str
    date: date
    broker_code: str
    percentage: Decimal
    lot_count: int
    daily_net: int
    net_size: int
    cost: Decimal


@dataclass(frozen=True, slots=True)
class CustodyHistory:
    symbol: str
    dates: tuple[date, ...]
    points: tuple[CustodyHistoryPoint, ...]


@dataclass(frozen=True, slots=True)
class DatedMetric:
    date: date
    value: Decimal


@dataclass(frozen=True, slots=True)
class DatedCount:
    date: date
    value: int


@dataclass(frozen=True, slots=True)
class CustodyStats:
    symbol: str
    retail_percentage: tuple[DatedMetric, ...]
    institution_percentage: tuple[DatedMetric, ...]
    investor_count: tuple[DatedCount, ...]


@dataclass(frozen=True, slots=True)
class BrokerInfo:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class BrokerageDirectory:
    as_of: date
    brokers: tuple[BrokerInfo, ...]


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    as_of: datetime
    depth: DepthSnapshot | None = None
    akd: AKDSnapshot | None = None
    custody: CustodianSnapshot | None = None
    custody_distribution: CustodyDistributionSnapshot | None = None
    custody_history: CustodyHistory | None = None
    custody_stats: CustodyStats | None = None
