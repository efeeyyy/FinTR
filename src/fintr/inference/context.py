"""Deterministic, provenance-aware context construction for market analysis."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any

from fintr.data.models import AKDBroker, CustodianRow, DepthLevel, MarketSnapshot
from fintr.features.akd import AKDFeatures
from fintr.features.custody import BrokerPersistence, HorizonChanges
from fintr.features.depth import DepthFeatures
from fintr.features.stats import StatsFeatures


SCHEMA_VERSION = "market_context_v0"

ANALYSIS_CONSTRAINTS: tuple[str, ...] = (
    "AKD is not custody.",
    "Broker code does not identify the ultimate investor.",
    "A single-day broker flow does not prove persistent accumulation.",
    "Order-book liquidity can change rapidly.",
    "Missing data must not be inferred.",
    "Conflicting horizons should be reported, not silently averaged.",
    "Do not claim certain future price direction from these observations.",
)


class ContextBuildError(ValueError):
    """Raised when normalized inputs cannot form a coherent context."""


@dataclass(frozen=True, slots=True)
class ContextLimits:
    depth_levels: int = 10
    akd_brokers: int = 5
    custody_holders: int = 10

    def __post_init__(self) -> None:
        for name, value in (
            ("depth_levels", self.depth_levels),
            ("akd_brokers", self.akd_brokers),
            ("custody_holders", self.custody_holders),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class MarketSummary:
    """Optional current price/volume summary from a distinct market source."""

    source: str
    as_of: datetime
    last_price: Decimal | None
    volume: int | None


@dataclass(frozen=True, slots=True)
class RecentTradesSummary:
    """Optional bounded summary; individual trades are intentionally excluded."""

    source: str
    as_of: datetime
    trade_count: int
    total_lots: int | None


@dataclass(frozen=True, slots=True)
class MarketFeatureSet:
    """Feature-engine outputs associated with a normalized market snapshot."""

    depth: DepthFeatures | None = None
    akd: AKDFeatures | None = None
    custody_changes: tuple[HorizonChanges, ...] = ()
    custody_top5_concentration: Decimal | None = None
    custody_persistence: tuple[BrokerPersistence, ...] = ()
    stats: StatsFeatures | None = None


@dataclass(frozen=True, slots=True)
class MarketAnalysisContext:
    """Structured context ready for deterministic JSON or text rendering."""

    schema_version: str
    symbol: str
    analysis_time: str
    data_freshness: dict[str, Any]
    market: dict[str, Any]
    depth: dict[str, Any]
    akd: dict[str, Any]
    custody: dict[str, Any]
    custody_stats: dict[str, Any]
    recent_trades: dict[str, Any]
    data_availability: dict[str, bool]
    missing_for_stronger_analysis: tuple[str, ...]
    analysis_constraints: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible representation."""

        return deepcopy(
            {
                "schema_version": self.schema_version,
                "symbol": self.symbol,
                "analysis_time": self.analysis_time,
                "data_freshness": self.data_freshness,
                "market": self.market,
                "depth": self.depth,
                "akd": self.akd,
                "custody": self.custody,
                "custody_stats": self.custody_stats,
                "recent_trades": self.recent_trades,
                "data_availability": self.data_availability,
                "missing_for_stronger_analysis": list(
                    self.missing_for_stronger_analysis
                ),
                "analysis_constraints": list(self.analysis_constraints),
            }
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize with stable ordering and UTF-8-safe characters."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _encoded(value: Decimal | int | str | date | datetime | None) -> Any:
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _value(
    value: Decimal | int | str | date | datetime | None,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "value": _encoded(value),
        "available": value is not None,
    }
    if value is None and reason is not None:
        result["reason"] = reason
    return result


def _validate_symbol(snapshot: MarketSnapshot) -> None:
    nested = (
        snapshot.depth,
        snapshot.akd,
        snapshot.custody,
        snapshot.custody_distribution,
        snapshot.custody_history,
        snapshot.custody_stats,
    )
    for item in nested:
        if item is not None and item.symbol != snapshot.symbol:
            raise ContextBuildError(
                f"symbol mismatch: market={snapshot.symbol!r}, nested={item.symbol!r}"
            )


def _depth_level(level: DepthLevel) -> dict[str, Any]:
    return {
        "price": _decimal(level.price),
        "lots": level.lots,
        "order_count": _value(level.order_count),
    }


def _depth_section(
    snapshot: MarketSnapshot,
    features: MarketFeatureSet,
    limits: ContextLimits,
) -> dict[str, Any]:
    depth = snapshot.depth
    if depth is None:
        return {
            "available": False,
            "source": None,
            "as_of": _value(None),
            "raw_summary": {"bids": [], "asks": []},
            "features": {},
        }

    bids = sorted(depth.bids, key=lambda level: level.price, reverse=True)
    asks = sorted(depth.asks, key=lambda level: level.price)
    feature = features.depth
    return {
        "available": True,
        "source": "depth_update",
        "as_of": _value(depth.timestamp),
        "raw_summary": {
            "level_limit_per_side": limits.depth_levels,
            "bids": [_depth_level(level) for level in bids[: limits.depth_levels]],
            "asks": [_depth_level(level) for level in asks[: limits.depth_levels]],
        },
        "features": {
            "total_bid_lots": _value(
                None if feature is None else feature.total_bid_lots
            ),
            "total_ask_lots": _value(
                None if feature is None else feature.total_ask_lots
            ),
            "bid_ask_imbalance": _value(
                None if feature is None else feature.bid_ask_imbalance,
                reason=(
                    "zero_denominator"
                    if feature and feature.bid_ask_imbalance is None
                    else None
                ),
            ),
            "top5_bid_lots": _value(
                None if feature is None else feature.top5_bid_lots
            ),
            "top5_ask_lots": _value(
                None if feature is None else feature.top5_ask_lots
            ),
            "spread": _value(None if feature is None else feature.spread),
            "spread_pct": _value(
                None if feature is None else feature.spread_pct,
                reason=(
                    "missing_book_side"
                    if feature and feature.spread_pct is None and (not bids or not asks)
                    else "zero_midpoint"
                    if feature and feature.spread_pct is None
                    else None
                ),
            ),
        },
    }


def _akd_broker(row: AKDBroker) -> dict[str, Any]:
    return {
        "code": row.code,
        "buy_lots": row.buy_lots,
        "sell_lots": row.sell_lots,
        "net_lots": row.net_lots,
        "average_cost": _value(row.average_cost),
    }


def _akd_section(
    snapshot: MarketSnapshot,
    features: MarketFeatureSet,
    limits: ContextLimits,
) -> dict[str, Any]:
    akd = snapshot.akd
    if akd is None:
        return {
            "available": False,
            "source": None,
            "as_of": _value(None),
            "market_price": _value(None),
            "top_buyers": [],
            "top_sellers": [],
            "features": {},
        }

    feature = features.akd
    top_buyers = tuple(
        sorted(
            (row for row in akd.brokers if row.net_lots > 0),
            key=lambda row: (-row.net_lots, row.code),
        )
    )
    top_sellers = tuple(
        sorted(
            (row for row in akd.brokers if row.net_lots < 0),
            key=lambda row: (row.net_lots, row.code),
        )
    )
    cost_gaps = () if feature is None else feature.broker_cost_vs_market_price
    return {
        "available": True,
        "source": "akd_snapshot",
        "as_of": _value(akd.date),
        "market_price": _value(akd.market_price),
        "broker_limit_per_side": limits.akd_brokers,
        "top_buyers": [
            _akd_broker(row) for row in top_buyers[: limits.akd_brokers]
        ],
        "top_sellers": [
            _akd_broker(row) for row in top_sellers[: limits.akd_brokers]
        ],
        "features": {
            "top5_buy_concentration": _value(
                None if feature is None else feature.top5_buy_concentration,
                reason=(
                    "zero_total_buy_lots"
                    if feature and feature.top5_buy_concentration is None
                    else None
                ),
            ),
            "top5_sell_concentration": _value(
                None if feature is None else feature.top5_sell_concentration,
                reason=(
                    "zero_total_sell_lots"
                    if feature and feature.top5_sell_concentration is None
                    else None
                ),
            ),
            "broker_cost_vs_market_price": [
                {"code": item.code, "gap_pct": _value(item.gap_pct)}
                for item in cost_gaps
            ],
        },
    }


def _holder(row: CustodianRow) -> dict[str, Any]:
    return {
        "code": row.code,
        "value": row.value,
        "percentage": _decimal(row.percentage),
    }


def _persistence(item: BrokerPersistence) -> dict[str, Any]:
    return {
        "code": item.code,
        "direction": _value(item.direction),
        "persistence": _value(item.persistence),
        "nonzero_observations": item.nonzero_observations,
    }


def _custody_section(
    snapshot: MarketSnapshot,
    features: MarketFeatureSet,
    limits: ContextLimits,
) -> dict[str, Any]:
    custody = snapshot.custody
    distribution = snapshot.custody_distribution
    history = snapshot.custody_history
    holders = () if custody is None else tuple(
        sorted(custody.rows, key=lambda row: (-row.value, row.code))
    )
    return {
        "available": any(item is not None for item in (custody, distribution, history)),
        "snapshot": {
            "available": custody is not None,
            "source": "custodian_data" if custody is not None else None,
            "date": _value(None if custody is None else custody.date),
            "holder_limit": limits.custody_holders,
            "top_holders": [
                _holder(row) for row in holders[: limits.custody_holders]
            ],
        },
        "distribution": {
            "available": distribution is not None,
            "source": "custody_distribution_data" if distribution is not None else None,
            "periods": []
            if distribution is None
            else [period.isoformat() for period in distribution.periods],
            "changes": [
                {
                    "code": item.code,
                    "change_1d": item.change_1d,
                    "change_7d": item.change_7d,
                    "change_30d": item.change_30d,
                    "change_90d": item.change_90d,
                }
                for item in features.custody_changes
            ],
        },
        "history": {
            "available": history is not None,
            "source": "custody_analysis_data" if history is not None else None,
            "from": _value(None if not history or not history.dates else min(history.dates)),
            "to": _value(None if not history or not history.dates else max(history.dates)),
            "raw_daily_points_included": False,
            "persistence": [
                _persistence(item) for item in features.custody_persistence
            ],
        },
        "features": {
            "top5_custody_concentration": _value(
                features.custody_top5_concentration,
                reason=(
                    "zero_total_custody"
                    if custody is not None
                    and features.custody_top5_concentration is None
                    else None
                ),
            )
        },
    }


def _latest(series: tuple[Any, ...]) -> Any | None:
    if not series:
        return None
    return max(series, key=lambda point: point.date).value


def _stats_section(
    snapshot: MarketSnapshot,
    features: MarketFeatureSet,
) -> dict[str, Any]:
    stats = snapshot.custody_stats
    if stats is None:
        return {
            "available": False,
            "source": None,
            "as_of": _value(None),
            "retail_percentage": _value(None),
            "institution_percentage": _value(None),
            "investor_count": _value(None),
            "features": {},
        }

    dates = [
        point.date
        for series in (
            stats.retail_percentage,
            stats.institution_percentage,
            stats.investor_count,
        )
        for point in series
    ]
    feature = features.stats
    return {
        "available": True,
        "source": "custody_stats_data",
        "as_of": _value(max(dates) if dates else None),
        "retail_percentage": _value(_latest(stats.retail_percentage)),
        "institution_percentage": _value(_latest(stats.institution_percentage)),
        "investor_count": _value(_latest(stats.investor_count)),
        "features": {
            "retail_share_change": _value(
                None if feature is None else feature.retail_share_change
            ),
            "institutional_share_change": _value(
                None if feature is None else feature.institutional_share_change
            ),
            "investor_count_change": _value(
                None if feature is None else feature.investor_count_change
            ),
        },
    }


def _market_section(market: MarketSummary | None) -> dict[str, Any]:
    if market is None:
        return {
            "available": False,
            "source": None,
            "as_of": _value(None),
            "last_price": _value(None),
            "volume": _value(None),
        }
    return {
        "available": True,
        "source": market.source,
        "as_of": _value(market.as_of),
        "last_price": _value(market.last_price),
        "volume": _value(market.volume),
    }


def _trades_section(trades: RecentTradesSummary | None) -> dict[str, Any]:
    if trades is None:
        return {
            "available": False,
            "source": None,
            "as_of": _value(None),
            "trade_count": _value(None),
            "total_lots": _value(None),
            "raw_trades_included": False,
        }
    return {
        "available": True,
        "source": trades.source,
        "as_of": _value(trades.as_of),
        "trade_count": _value(trades.trade_count),
        "total_lots": _value(trades.total_lots),
        "raw_trades_included": False,
    }


def build_market_context(
    snapshot: MarketSnapshot,
    features: MarketFeatureSet,
    *,
    market: MarketSummary | None = None,
    recent_trades: RecentTradesSummary | None = None,
    limits: ContextLimits | None = None,
) -> MarketAnalysisContext:
    """Build a bounded context without adding analysis or inferred values."""

    _validate_symbol(snapshot)
    selected_limits = ContextLimits() if limits is None else limits

    availability = {
        "market": market is not None,
        "depth": snapshot.depth is not None,
        "akd": snapshot.akd is not None,
        "custody_snapshot": snapshot.custody is not None,
        "custody_distribution": snapshot.custody_distribution is not None,
        "custody_history": snapshot.custody_history is not None,
        "custody_stats": snapshot.custody_stats is not None,
        "recent_trades": recent_trades is not None,
    }
    missing_names = {
        "market": "current_market_data",
        "depth": "current_depth",
        "akd": "current_akd",
        "custody_snapshot": "current_custody_snapshot",
        "custody_distribution": "multi_horizon_custody_distribution",
        "custody_history": "custody_history",
        "custody_stats": "custody_stats",
        "recent_trades": "recent_trades",
    }
    missing = tuple(
        missing_names[name] for name, available in availability.items() if not available
    )

    history = snapshot.custody_history
    return MarketAnalysisContext(
        schema_version=SCHEMA_VERSION,
        symbol=snapshot.symbol,
        analysis_time=snapshot.as_of.isoformat(),
        data_freshness={
            "market_as_of": _value(None if market is None else market.as_of),
            "depth_as_of": _value(
                None if snapshot.depth is None else snapshot.depth.timestamp
            ),
            "akd_as_of": _value(None if snapshot.akd is None else snapshot.akd.date),
            "custody_date": _value(
                None if snapshot.custody is None else snapshot.custody.date
            ),
            "custody_history_to": _value(
                None if not history or not history.dates else max(history.dates)
            ),
            "custody_stats_to": _stats_section(snapshot, features)["as_of"],
            "recent_trades_as_of": _value(
                None if recent_trades is None else recent_trades.as_of
            ),
        },
        market=_market_section(market),
        depth=_depth_section(snapshot, features, selected_limits),
        akd=_akd_section(snapshot, features, selected_limits),
        custody=_custody_section(snapshot, features, selected_limits),
        custody_stats=_stats_section(snapshot, features),
        recent_trades=_trades_section(recent_trades),
        data_availability=availability,
        missing_for_stronger_analysis=missing,
        analysis_constraints=ANALYSIS_CONSTRAINTS,
    )


def render_market_context(context: MarketAnalysisContext) -> str:
    """Render structured context for a text-only LLM without new analysis."""

    data = context.to_dict()

    def compact(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    sections = [
        "[ŞEMA]",
        context.schema_version,
        "",
        "[SEMBOL]",
        context.symbol,
        "",
        "[ANALİZ ZAMANI]",
        context.analysis_time,
        "",
        "[VERİ ZAMANLARI]",
        compact(data["data_freshness"]),
        "",
        "[PİYASA]",
        compact(data["market"]),
        "",
        "[DERİNLİK]",
        compact(data["depth"]),
        "",
        "[AKD]",
        compact(data["akd"]),
        "",
        "[TAKAS]",
        compact(data["custody"]),
        "",
        "[TAKAS İSTATİSTİKLERİ]",
        compact(data["custody_stats"]),
        "",
        "[SON İŞLEMLER]",
        compact(data["recent_trades"]),
        "",
        "[VERİ MEVCUDİYETİ]",
        compact(data["data_availability"]),
        "",
        "[VERİ EKSİKLERİ]",
        compact(data["missing_for_stronger_analysis"]),
        "",
        "[KISITLAR]",
        *(f"- {constraint}" for constraint in context.analysis_constraints),
    ]
    return "\n".join(sections) + "\n"
