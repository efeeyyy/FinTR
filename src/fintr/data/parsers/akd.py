"""Parser for broker distribution (AKD) snapshots."""

from __future__ import annotations

from typing import Any

from fintr.data.models import AKDBroker, AKDSnapshot
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
