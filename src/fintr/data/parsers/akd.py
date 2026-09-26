"""Parser for broker distribution (AKD) snapshots."""

from __future__ import annotations

from typing import Any

from fintr.data.models import (
    AKDBroker, AKDSnapshot, LiveAKDNet, LiveAKDResponse, LiveAKDRow, LiveAKDTotal,
)
from fintr.data.parsers.common import (
    header_field,
    optional_decimal,
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


def _parse_broker(value: Any, path: str) -> AKDBroker:
    row = require_mapping(value, path)
    return AKDBroker(
        code=require_string(require_field(row, "code", path), f"{path}.code"),
        buy_lots=require_int(require_field(row, "buy_lots", path), f"{path}.buy_lots"),
        sell_lots=require_int(require_field(row, "sell_lots", path), f"{path}.sell_lots"),
        net_lots=require_int(require_field(row, "net_lots", path), f"{path}.net_lots"),
        average_cost=optional_decimal(row.get("average_cost"), f"{path}.average_cost"),
        buy_value=optional_decimal(row.get("buy_value"), f"{path}.buy_value"),
        sell_value=optional_decimal(row.get("sell_value"), f"{path}.sell_value"),
    )


def parse_akd_snapshot(event: Any) -> AKDSnapshot:
    """Parse the legacy offline snapshot contract, not the live akd_data envelope."""
    root = require_event(event, {"get_akd", "akd_snapshot"})
    payload = require_payload(root)
    results = require_list(require_field(payload, "results", "event.data"), "event.data.results")
    market_price_value = root.get("market_price", payload.get("market_price"))
    return AKDSnapshot(
        symbol=require_string(header_field(root, payload, "symbol"), "event.symbol"),
        date=require_date(header_field(root, payload, "date"), "event.date"),
        market_price=(
            None
            if market_price_value is None
            else require_decimal(market_price_value, "event.market_price")
        ),
        brokers=tuple(
            _parse_broker(row, f"event.data.results[{index}]")
            for index, row in enumerate(results)
        ),
    )


def parse_live_akd_data(event: Any) -> LiveAKDResponse:
    """Parse observed query fields without inventing symbol, date, or trade sides."""
    root = require_event(event, {"akd_data"})
    envelope = require_payload(root)
    request_id = require_string(
        require_field(envelope, "request_id", "event.data"), "event.data.request_id"
    )
    processing_time_ms = require_decimal(
        require_field(envelope, "processing_time_ms", "event.data"),
        "event.data.processing_time_ms",
    )
    timestamp = require_decimal(
        require_field(envelope, "timestamp", "event.data"), "event.data.timestamp"
    )
    payload = require_mapping(
        require_field(envelope, "data", "event.data"), "event.data.data"
    )
    as_of_value = payload.get("as_of")
    as_of = (
        None if as_of_value is None
        else require_string(as_of_value, "event.data.data.as_of")
    )
    source = require_string(
        require_field(payload, "source", "event.data.data"), "event.data.data.source"
    )
    rows = require_list(
        require_field(payload, "results", "event.data.data"), "event.data.data.results"
    )
    parsed_rows: list[LiveAKDRow] = []
    for index, value in enumerate(rows):
        path = f"event.data.data.results[{index}]"
        row = require_mapping(value, path)
        brokerage = require_string(require_field(row, "brokerage", path), f"{path}.brokerage")
        net = require_mapping(require_field(row, "net", path), f"{path}.net")
        total = require_mapping(require_field(row, "total", path), f"{path}.total")
        parsed_rows.append(LiveAKDRow(
            brokerage=brokerage,
            net=LiveAKDNet(
                size=require_int(require_field(net, "size", f"{path}.net"), f"{path}.net.size"),
                percentage=require_decimal(require_field(net, "percentage", f"{path}.net"), f"{path}.net.percentage"),
                cost=require_decimal(require_field(net, "cost", f"{path}.net"), f"{path}.net.cost"),
            ),
            total=LiveAKDTotal(**{
                key: require_decimal(require_field(total, key, f"{path}.total"), f"{path}.total.{key}")
                for key in ("size", "percentage", "cost", "volume")
            }),
        ))
    return LiveAKDResponse(
        request_id=request_id,
        timestamp=timestamp,
        processing_time_ms=processing_time_ms,
        as_of=as_of,
        source=source,
        results=tuple(parsed_rows),
    )


def validate_akd_data_envelope(event: Any) -> None:
    """Compatibility validator for the observed typed AKD contract."""
    parse_live_akd_data(event)
