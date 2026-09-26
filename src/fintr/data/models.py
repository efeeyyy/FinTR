"""Immutable normalized models for BIST market microstructure data."""

from __future__ import annotations

from dataclasses import dataclass, field
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
class SentAKDRequestProvenance:
    """Local send evidence, not a symbol/period echoed by the server."""

    symbol: str
    start: date
    end: date
    origin: str = "local_sent_request"


@dataclass(frozen=True, slots=True)
class LiveAKDNet:
    size: int
    percentage: Decimal
    cost: Decimal


@dataclass(frozen=True, slots=True)
class LiveAKDTotal:
    size: Decimal
    percentage: Decimal
    cost: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class LiveAKDRow:
    brokerage: str
    net: LiveAKDNet
    total: LiveAKDTotal


@dataclass(frozen=True, slots=True)
class LiveAKDResponse:
    """Observed query response; never interchangeable with AKDSnapshot."""

    request_id: str = field(repr=False)
    timestamp: Decimal
    processing_time_ms: Decimal
    as_of: str | None
    source: str
    results: tuple[LiveAKDRow, ...]
    request_provenance: SentAKDRequestProvenance | None = None


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
class SentCustodianRequestProvenance:
    """Locally sent query fields; neither field is echoed by the live response."""

    symbol: str
    requested_date: date
    origin: str = "local_sent_request"


@dataclass(frozen=True, slots=True)
class LiveCustodianRow:
    custodian: str
    value: Decimal
    percentage: Decimal


@dataclass(frozen=True, slots=True)
class LiveCustodianResponse:
    """Observed nested response, distinct from the legacy CustodianSnapshot."""

    request_id: str = field(repr=False)
    timestamp: Decimal
    processing_time_ms: Decimal
    date: date
    source: str
    results: tuple[LiveCustodianRow, ...]
    request_provenance: SentCustodianRequestProvenance | None = None


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
class SentCustodianDiffRequestProvenance:
    """Locally sent symbol and range, not server-echoed fields."""

    symbol: str
    requested_start: date
    requested_end: date
    origin: str = "local_sent_request"


@dataclass(frozen=True, slots=True)
class LiveCustodianDiffRow:
    custodian: str
    difference: Decimal
    first_value: Decimal
    last_value: Decimal
    first_percentage: Decimal
    last_percentage: Decimal
    percentage_change: Decimal


@dataclass(frozen=True, slots=True)
class LiveCustodianDiffResponse:
    """Observed nested period-difference response, separate from legacy data."""

    request_id: str = field(repr=False)
    timestamp: Decimal
    processing_time_ms: Decimal
    start_date: date
    end_date: date
    source: str
    results: tuple[LiveCustodianDiffRow, ...]
    request_provenance: SentCustodianDiffRequestProvenance | None = None


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
class SentCustodyDistributionRequestProvenance:
    """Locally sent query symbol; the live response does not echo it."""

    symbol: str
    origin: str = "local_sent_request"


@dataclass(frozen=True, slots=True)
class LiveCustodyDistributionRow:
    code: str
    values: tuple[Decimal | None, ...]
    percentage: Decimal | None


@dataclass(frozen=True, slots=True)
class LiveCustodyDistributionResponse:
    """Observed nested distribution response, distinct from the legacy snapshot."""

    request_id: str = field(repr=False)
    timestamp: Decimal
    processing_time_ms: Decimal
    periods: tuple[date, ...]
    rows: tuple[LiveCustodyDistributionRow, ...]
    source: str
    request_provenance: SentCustodyDistributionRequestProvenance | None = None


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
class SentCustodyAnalysisRequestProvenance:
    symbol: str
    requested_from: date
    requested_to: date
    origin: str = "local_sent_request"


@dataclass(frozen=True, slots=True)
class LiveAnalysisAKDRow:
    broker_code: str
    net_size: Decimal
    net_cost: Decimal
    net_percentage: Decimal
    total_size: Decimal
    total_cost: Decimal


@dataclass(frozen=True, slots=True)
class LiveAnalysisCustodyRow:
    broker_code: str
    lot_count: Decimal
    percentage: Decimal


@dataclass(frozen=True, slots=True)
class LiveAnalysisAKDDay:
    date: date
    rows: tuple[LiveAnalysisAKDRow, ...]


@dataclass(frozen=True, slots=True)
class LiveAnalysisCustodyDay:
    date: date
    rows: tuple[LiveAnalysisCustodyRow, ...]


@dataclass(frozen=True, slots=True)
class LiveAnalysisRange:
    requested_from: date
    requested_to: date
    actual_from: date


@dataclass(frozen=True, slots=True)
class LiveAnalysisMetadataDay:
    date: date
    null_fields: tuple[str, ...]
    uninterpreted_keys: tuple[str, ...]
    retail_pct: Decimal | None = None
    institutional_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LiveCustodyAnalysisResponse:
    """Observed live analysis subset; AKD and custody are independent series."""

    request_id: str = field(repr=False)
    timestamp: Decimal
    processing_time_ms: Decimal
    symbol: str
    dates: tuple[date, ...]
    akd: tuple[LiveAnalysisAKDDay, ...]
    custody: tuple[LiveAnalysisCustodyDay, ...]
    data_range: LiveAnalysisRange
    metadata: tuple[LiveAnalysisMetadataDay, ...]
    request_provenance: SentCustodyAnalysisRequestProvenance | None = None


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
class SentCustodyStatsRequestProvenance:
    """Locally sent stats query fields; the server response does not echo them."""

    symbol: str
    history: bool
    origin: str = "local_sent_request"


@dataclass(frozen=True, slots=True)
class LiveCustodyStatsFormat:
    """Scalar display metadata preserved verbatim; it is not a value transform."""

    fields: tuple[tuple[str, str | int | bool | Decimal | None], ...]


@dataclass(frozen=True, slots=True)
class LiveCustodyStatsPoint:
    date: date
    value: Decimal | None
    label: None


@dataclass(frozen=True, slots=True)
class LiveCustodyStatsSeries:
    format: LiveCustodyStatsFormat
    points: tuple[LiveCustodyStatsPoint, ...]


@dataclass(frozen=True, slots=True)
class LiveCustodyStatsResponse:
    """Observed nested stats envelope, separate from legacy CustodyStats."""

    request_id: str | None = field(repr=False)
    cached: bool
    retail_percentage: LiveCustodyStatsSeries
    institution_percentage: LiveCustodyStatsSeries
    investor_count: LiveCustodyStatsSeries
    request_provenance: SentCustodyStatsRequestProvenance | None = None


@dataclass(frozen=True, slots=True)
class BrokerInfo:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class BrokerageDirectory:
    as_of: date
    brokers: tuple[BrokerInfo, ...]


@dataclass(frozen=True, slots=True)
class LiveBrokerageRow:
    """Candidate row accepted in one reported live response; text stays redacted."""

    code: str = field(repr=False)
    title: str = field(repr=False)
    short_title: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LiveBrokeragesResponse:
    """Typed live rows; one accepted response is not a provider-wide guarantee."""

    rows: tuple[LiveBrokerageRow, ...]


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
