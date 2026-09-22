"""Parsers for custody/takas snapshots, differences, history, and statistics."""

from __future__ import annotations

from typing import Any

from fintr.data.models import (
    CustodianDiffRow,
    CustodianDiffSnapshot,
    CustodianRow,
    CustodianSnapshot,
    CustodyDistributionRow,
    CustodyDistributionSnapshot,
    CustodyHistory,
    CustodyHistoryPoint,
    CustodyStats,
    DatedCount,
    DatedMetric,
)
from fintr.data.parsers.common import (
    FieldTypeError,
    header_field,
    require_date,
    require_decimal,
    require_event,
    require_field,
    require_int,
    require_list,
    require_mapping,
    require_payload,
    require_string,
)


def parse_custodian_snapshot(event: Any) -> CustodianSnapshot:
    root = require_event(event, {"get_custodian", "custodian_data"})
    payload = require_payload(root)
    results = require_list(require_field(payload, "results", "event.data"), "event.data.results")
    rows: list[CustodianRow] = []
    for index, value in enumerate(results):
        path = f"event.data.results[{index}]"
        row = require_mapping(value, path)
        rows.append(
            CustodianRow(
                code=require_string(require_field(row, "custodian", path), f"{path}.custodian"),
                value=require_int(require_field(row, "value", path), f"{path}.value"),
                percentage=require_decimal(
                    require_field(row, "percentage", path), f"{path}.percentage"
                ),
            )
        )
    return CustodianSnapshot(
        symbol=require_string(header_field(root, payload, "symbol"), "event.symbol"),
        date=require_date(header_field(root, payload, "date"), "event.date"),
        rows=tuple(rows),
    )


def parse_custodian_diff(event: Any) -> CustodianDiffSnapshot:
    root = require_event(event, {"get_custodian_diff", "custodian_diff_data"})
    payload = require_payload(root)
    results = require_list(require_field(payload, "results", "event.data"), "event.data.results")
    rows: list[CustodianDiffRow] = []
    for index, value in enumerate(results):
        path = f"event.data.results[{index}]"
        row = require_mapping(value, path)
        rows.append(
            CustodianDiffRow(
                code=require_string(require_field(row, "custodian", path), f"{path}.custodian"),
                first_value=require_int(
                    require_field(row, "first_value", path), f"{path}.first_value"
                ),
                last_value=require_int(
                    require_field(row, "last_value", path), f"{path}.last_value"
                ),
                difference=require_int(
                    require_field(row, "difference", path), f"{path}.difference"
                ),
                first_percentage=require_decimal(
                    require_field(row, "first_percentage", path), f"{path}.first_percentage"
                ),
                last_percentage=require_decimal(
                    require_field(row, "last_percentage", path), f"{path}.last_percentage"
                ),
            )
        )
    return CustodianDiffSnapshot(
        symbol=require_string(header_field(root, payload, "symbol"), "event.symbol"),
        first_date=require_date(header_field(root, payload, "first_date"), "event.first_date"),
        last_date=require_date(header_field(root, payload, "last_date"), "event.last_date"),
        rows=tuple(rows),
    )


def parse_custody_distribution(event: Any) -> CustodyDistributionSnapshot:
    root = require_event(event, {"get_custody_distribution", "custody_distribution_data"})
    payload = require_payload(root)
    raw_periods = require_list(
        require_field(payload, "periods", "event.data"), "event.data.periods"
    )
    periods = tuple(
        require_date(value, f"event.data.periods[{index}]")
        for index, value in enumerate(raw_periods)
    )
    results = require_list(require_field(payload, "data", "event.data"), "event.data.data")
    rows: list[CustodyDistributionRow] = []
    for index, value in enumerate(results):
        path = f"event.data.data[{index}]"
        row = require_mapping(value, path)
        raw_values = require_list(require_field(row, "values", path), f"{path}.values")
        if len(raw_values) != 5:
            raise FieldTypeError(f"{path}.values: expected exactly 5 horizon values")
        normalized = tuple(
            require_int(item, f"{path}.values[{value_index}]")
            for value_index, item in enumerate(raw_values)
        )
        rows.append(
            CustodyDistributionRow(
                code=require_string(require_field(row, "code", path), f"{path}.code"),
                values=normalized,  # type: ignore[arg-type]
                percentage=require_decimal(
                    require_field(row, "percentage", path), f"{path}.percentage"
                ),
            )
        )
    return CustodyDistributionSnapshot(
        symbol=require_string(header_field(root, payload, "symbol"), "event.symbol"),
        periods=periods,
        rows=tuple(rows),
    )


def parse_custody_history(event: Any) -> CustodyHistory:
    root = require_event(event, {"custody_analysis", "custody_analysis_data"})
    payload = require_payload(root)
    symbol = require_string(header_field(root, payload, "symbol"), "event.symbol")
    raw_dates = require_list(
        require_field(payload, "dates", "event.data"), "event.data.dates"
    )
    dates = tuple(
        require_date(value, f"event.data.dates[{index}]")
        for index, value in enumerate(raw_dates)
    )
    custody = require_mapping(
        require_field(payload, "custody", "event.data"), "event.data.custody"
    )
    points: list[CustodyHistoryPoint] = []
    for current_date in dates:
        date_key = current_date.isoformat()
        rows = require_list(
            require_field(custody, date_key, "event.data.custody"),
            f"event.data.custody.{date_key}",
        )
        for index, value in enumerate(rows):
            path = f"event.data.custody.{date_key}[{index}]"
            row = require_mapping(value, path)
            points.append(
                CustodyHistoryPoint(
                    symbol=symbol,
                    date=current_date,
                    broker_code=require_string(
                        require_field(row, "broker_code", path), f"{path}.broker_code"
                    ),
                    percentage=require_decimal(
                        require_field(row, "percentage", path), f"{path}.percentage"
                    ),
                    lot_count=require_int(
                        require_field(row, "lot_count", path), f"{path}.lot_count"
                    ),
                    daily_net=require_int(
                        require_field(row, "daily_net", path), f"{path}.daily_net"
                    ),
                    net_size=require_int(
                        require_field(row, "net_size", path), f"{path}.net_size"
                    ),
                    cost=require_decimal(require_field(row, "cost", path), f"{path}.cost"),
                )
            )
    return CustodyHistory(symbol=symbol, dates=dates, points=tuple(points))


def _parse_metric_series(payload: dict[str, Any] | Any, key: str) -> tuple[DatedMetric, ...]:
    root = require_mapping(require_field(payload, key, "event.data"), f"event.data.{key}")
    values = require_list(
        require_field(root, "data", f"event.data.{key}"), f"event.data.{key}.data"
    )
    result: list[DatedMetric] = []
    for index, value in enumerate(values):
        path = f"event.data.{key}.data[{index}]"
        row = require_mapping(value, path)
        result.append(
            DatedMetric(
                date=require_date(require_field(row, "date", path), f"{path}.date"),
                value=require_decimal(require_field(row, "value", path), f"{path}.value"),
            )
        )
    return tuple(result)


def _parse_count_series(payload: Any, key: str) -> tuple[DatedCount, ...]:
    root = require_mapping(require_field(payload, key, "event.data"), f"event.data.{key}")
    values = require_list(
        require_field(root, "data", f"event.data.{key}"), f"event.data.{key}.data"
    )
    result: list[DatedCount] = []
    for index, value in enumerate(values):
        path = f"event.data.{key}.data[{index}]"
        row = require_mapping(value, path)
        result.append(
            DatedCount(
                date=require_date(require_field(row, "date", path), f"{path}.date"),
                value=require_int(require_field(row, "value", path), f"{path}.value"),
            )
        )
    return tuple(result)


def parse_custody_stats(event: Any) -> CustodyStats:
    root = require_event(event, {"get_custody_stats", "custody_stats_data"})
    payload = require_payload(root)
    return CustodyStats(
        symbol=require_string(header_field(root, payload, "symbol"), "event.symbol"),
        retail_percentage=_parse_metric_series(payload, "retail_percentage"),
        institution_percentage=_parse_metric_series(payload, "institution_percentage"),
        investor_count=_parse_count_series(payload, "investor_count"),
    )
