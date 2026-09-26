"""Strict typed models and parser for Yahoo Finance daily OHLCV chart history.

Source: the public, undocumented chart endpoint at
``https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>``. This is an
**unofficial secondary source**, not Borsa İstanbul official data; see
``docs/price_history.md`` for scope, fields, and the recorded permission
basis. Raw close and adjusted close are kept as separate fields and are
never mixed; a value that the source reports as ``null`` stays ``None`` and
is never coerced to ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fintr.data.parsers.common import (
    MarketDataParseError,
    require_field,
    require_int,
    require_list,
    require_mapping,
    require_string,
)


class PriceHistoryParseError(ValueError):
    """The Yahoo Finance chart-endpoint payload did not match the expected shape."""


# Recorded on ``PriceHistoryMeta.date_source`` so every capture states exactly
# which method converted each UTC timestamp to an exchange-local trade date.
DATE_SOURCE_TIMEZONE_NAME = "exchange_timezone_name"
DATE_SOURCE_GMTOFFSET_FALLBACK = "gmtoffset_fallback"


@dataclass(frozen=True, slots=True)
class PriceHistoryMeta:
    """Non-numeric identification/provenance fields from ``chart.result[0].meta``."""

    symbol: str
    currency: str
    exchange_name: str
    exchange_timezone_name: str
    gmtoffset: int
    timezone_abbreviation: str
    date_source: str


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One daily OHLCV point. Any field the source reports as null stays ``None``."""

    trade_date: date
    timestamp_utc: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    adjusted_close: Decimal | None
    volume: int | None


@dataclass(frozen=True, slots=True)
class PriceHistory:
    """The full, unfiltered parsed chart response."""

    meta: PriceHistoryMeta
    bars: tuple[DailyBar, ...]


@dataclass(frozen=True, slots=True)
class FilteredPriceHistory:
    """``PriceHistory`` restricted to an inclusive requested date range."""

    meta: PriceHistoryMeta
    bars: tuple[DailyBar, ...]
    requested_start: date
    requested_end: date
    dropped_before_start: int
    dropped_after_end: int


def _optional_decimal(value: Any, path: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceHistoryParseError(
            f"{path}: expected number or null, got {type(value).__name__}"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise PriceHistoryParseError(f"{path}: number must be finite")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise PriceHistoryParseError(f"{path}: invalid decimal number") from exc
    if not result.is_finite():
        raise PriceHistoryParseError(f"{path}: number must be finite")
    return result


def _optional_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PriceHistoryParseError(
            f"{path}: expected integer or null, got {type(value).__name__}"
        )
    return value


def _resolve_timezone(tz_name: str) -> tuple[ZoneInfo | None, str]:
    """A ``ZoneInfo`` when the IANA database is available locally, else None.

    On a system without the ``tzdata`` package (observed on this project's
    Windows dev environment), ``ZoneInfo`` construction raises
    ``ZoneInfoNotFoundError`` even for a valid IANA name; callers then fall
    back to ``meta.gmtoffset`` and must record that fact.
    """
    try:
        return ZoneInfo(tz_name), DATE_SOURCE_TIMEZONE_NAME
    except (ZoneInfoNotFoundError, ValueError):
        return None, DATE_SOURCE_GMTOFFSET_FALLBACK


def _local_trade_date(
    timestamp_utc: int, tz: ZoneInfo | None, gmtoffset: int, path: str,
) -> date:
    try:
        if tz is not None:
            return datetime.fromtimestamp(timestamp_utc, tz=tz).date()
        return (
            datetime.fromtimestamp(timestamp_utc, tz=timezone.utc)
            + timedelta(seconds=gmtoffset)
        ).date()
    except (OverflowError, OSError, ValueError) as exc:
        raise PriceHistoryParseError(f"{path}: unconvertible unix timestamp") from exc


def _require_int_list(value: Any, path: str) -> list[int]:
    raw = require_list(value, path)
    result: list[int] = []
    for index, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, int):
            raise PriceHistoryParseError(
                f"{path}[{index}]: expected integer, got {type(item).__name__}"
            )
        result.append(item)
    return result


def parse_chart_json(document: Any) -> PriceHistory:
    """Validate and normalize one Yahoo Finance ``/v8/finance/chart`` response body.

    Rejects: more/less than one ``chart.result`` entry, an upstream
    ``chart.error``, mismatched OHLCV/adjclose array lengths, duplicate
    exchange-local trade dates, non-finite numbers, and a non-string
    symbol/currency.
    """
    try:
        root = require_mapping(document, "document")
        chart = require_mapping(require_field(root, "chart", "document"), "document.chart")
        error = chart.get("error")
        if error is not None:
            raise PriceHistoryParseError(f"document.chart.error: upstream reported an error: {error!r}")
        results = require_list(require_field(chart, "result", "document.chart"), "document.chart.result")
        if len(results) != 1:
            raise PriceHistoryParseError(
                f"document.chart.result: expected exactly 1 entry, got {len(results)}"
            )
        result = require_mapping(results[0], "document.chart.result[0]")

        meta_path = "document.chart.result[0].meta"
        meta_raw = require_mapping(require_field(result, "meta", "document.chart.result[0]"), meta_path)
        symbol = require_string(require_field(meta_raw, "symbol", meta_path), f"{meta_path}.symbol")
        currency = require_string(require_field(meta_raw, "currency", meta_path), f"{meta_path}.currency")
        exchange_name = require_string(
            require_field(meta_raw, "exchangeName", meta_path), f"{meta_path}.exchangeName",
        )
        exchange_timezone_name = require_string(
            require_field(meta_raw, "exchangeTimezoneName", meta_path),
            f"{meta_path}.exchangeTimezoneName",
        )
        gmtoffset = require_int(require_field(meta_raw, "gmtoffset", meta_path), f"{meta_path}.gmtoffset")
        timezone_abbreviation = require_string(
            require_field(meta_raw, "timezone", meta_path), f"{meta_path}.timezone",
        )

        result_path = "document.chart.result[0]"
        timestamp_raw = result.get("timestamp")
        timestamps = (
            [] if timestamp_raw is None
            else _require_int_list(timestamp_raw, f"{result_path}.timestamp")
        )
        count = len(timestamps)

        indicators = require_mapping(
            require_field(result, "indicators", result_path), f"{result_path}.indicators",
        )
        quote_list = require_list(
            require_field(indicators, "quote", f"{result_path}.indicators"),
            f"{result_path}.indicators.quote",
        )
        if len(quote_list) != 1:
            raise PriceHistoryParseError(
                f"{result_path}.indicators.quote: expected exactly 1 entry, got {len(quote_list)}"
            )
        quote_path = f"{result_path}.indicators.quote[0]"
        quote = require_mapping(quote_list[0], quote_path)

        def _quote_series(key: str) -> list[Any]:
            raw = require_list(require_field(quote, key, quote_path), f"{quote_path}.{key}")
            if len(raw) != count:
                raise PriceHistoryParseError(
                    f"{quote_path}.{key}: length {len(raw)} does not match "
                    f"{result_path}.timestamp length {count}"
                )
            return list(raw)

        opens = _quote_series("open")
        highs = _quote_series("high")
        lows = _quote_series("low")
        closes = _quote_series("close")
        volumes = _quote_series("volume")

        adjclose_raw = indicators.get("adjclose")
        if adjclose_raw is None:
            adjcloses: list[Any] = [None] * count
        else:
            adjclose_list = require_list(adjclose_raw, f"{result_path}.indicators.adjclose")
            if len(adjclose_list) != 1:
                raise PriceHistoryParseError(
                    f"{result_path}.indicators.adjclose: expected exactly 1 entry, "
                    f"got {len(adjclose_list)}"
                )
            adjclose_path = f"{result_path}.indicators.adjclose[0]"
            adjclose_map = require_mapping(adjclose_list[0], adjclose_path)
            adjcloses = list(require_list(
                require_field(adjclose_map, "adjclose", adjclose_path),
                f"{adjclose_path}.adjclose",
            ))
            if len(adjcloses) != count:
                raise PriceHistoryParseError(
                    f"{adjclose_path}.adjclose: length {len(adjcloses)} does not match "
                    f"{result_path}.timestamp length {count}"
                )

        tz, date_source = _resolve_timezone(exchange_timezone_name)

        bars: list[DailyBar] = []
        seen_dates: set[date] = set()
        for index in range(count):
            timestamp_utc = timestamps[index]
            trade_date = _local_trade_date(
                timestamp_utc, tz, gmtoffset, f"{result_path}.timestamp[{index}]",
            )
            if trade_date in seen_dates:
                raise PriceHistoryParseError(
                    f"duplicate trade date {trade_date.isoformat()} at index {index}"
                )
            seen_dates.add(trade_date)
            bars.append(DailyBar(
                trade_date=trade_date,
                timestamp_utc=timestamp_utc,
                open=_optional_decimal(opens[index], f"{quote_path}.open[{index}]"),
                high=_optional_decimal(highs[index], f"{quote_path}.high[{index}]"),
                low=_optional_decimal(lows[index], f"{quote_path}.low[{index}]"),
                close=_optional_decimal(closes[index], f"{quote_path}.close[{index}]"),
                adjusted_close=_optional_decimal(
                    adjcloses[index], f"{result_path}.indicators.adjclose[0].adjclose[{index}]",
                ),
                volume=_optional_int(volumes[index], f"{quote_path}.volume[{index}]"),
            ))

        meta = PriceHistoryMeta(
            symbol=symbol,
            currency=currency,
            exchange_name=exchange_name,
            exchange_timezone_name=exchange_timezone_name,
            gmtoffset=gmtoffset,
            timezone_abbreviation=timezone_abbreviation,
            date_source=date_source,
        )
        return PriceHistory(meta=meta, bars=tuple(bars))
    except MarketDataParseError as exc:
        raise PriceHistoryParseError(str(exc)) from exc


def filter_to_range(history: PriceHistory, start: date, end: date) -> FilteredPriceHistory:
    """Keep only bars whose exchange-local trade date falls in ``[start, end]``."""
    if not isinstance(start, date) or not isinstance(end, date):
        raise TypeError("start and end must be date instances")
    if start > end:
        raise ValueError("start must not be after end")
    kept: list[DailyBar] = []
    dropped_before = 0
    dropped_after = 0
    for bar in history.bars:
        if bar.trade_date < start:
            dropped_before += 1
        elif bar.trade_date > end:
            dropped_after += 1
        else:
            kept.append(bar)
    return FilteredPriceHistory(
        meta=history.meta,
        bars=tuple(kept),
        requested_start=start,
        requested_end=end,
        dropped_before_start=dropped_before,
        dropped_after_end=dropped_after,
    )


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def normalized_payload(filtered: FilteredPriceHistory) -> dict[str, Any]:
    """A plain JSON-safe dict (Decimal encoded via ``str()``) for capture artifacts."""
    meta = filtered.meta
    return {
        "schema": "fintr_price_history_normalized_v1",
        "symbol": meta.symbol,
        "currency": meta.currency,
        "exchange_name": meta.exchange_name,
        "exchange_timezone_name": meta.exchange_timezone_name,
        "gmtoffset": meta.gmtoffset,
        "timezone_abbreviation": meta.timezone_abbreviation,
        "date_source": meta.date_source,
        "requested_start_date": filtered.requested_start.isoformat(),
        "requested_end_date": filtered.requested_end.isoformat(),
        "dropped_before_start_count": filtered.dropped_before_start,
        "dropped_after_end_count": filtered.dropped_after_end,
        "bar_count": len(filtered.bars),
        "bars": [
            {
                "date": bar.trade_date.isoformat(),
                "timestamp_utc": bar.timestamp_utc,
                "open": _decimal_str(bar.open),
                "high": _decimal_str(bar.high),
                "low": _decimal_str(bar.low),
                "close": _decimal_str(bar.close),
                "adjusted_close": _decimal_str(bar.adjusted_close),
                "volume": bar.volume,
            }
            for bar in filtered.bars
        ],
    }


def canonical_json(value: Any) -> str:
    """Sorted-key compact JSON with exact Decimal/number tokens, for capture hashes."""
    import json

    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise PriceHistoryParseError("non_string_json_key")
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + canonical_json(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PriceHistoryParseError("non_finite_number")
        return str(value)
    if value is None or type(value) in (str, int, bool):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PriceHistoryParseError("non_finite_number")
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise PriceHistoryParseError("unsupported_json_value")
