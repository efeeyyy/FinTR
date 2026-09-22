"""Parser for sanitized ``depth_update`` messages."""

from __future__ import annotations

from typing import Any

from fintr.data.models import DepthLevel, DepthSnapshot
from fintr.data.parsers.common import (
    optional_datetime,
    require_event,
    require_field,
    require_int,
    require_list,
    require_mapping,
    require_decimal,
    require_payload,
    require_string,
    header_field,
)


def _parse_level(value: Any, path: str) -> DepthLevel:
    row = require_mapping(value, path)
    order_count_value = row.get("order_count")
    return DepthLevel(
        price=require_decimal(require_field(row, "price", path), f"{path}.price"),
        lots=require_int(require_field(row, "lots", path), f"{path}.lots"),
        order_count=(
            None
            if order_count_value is None
            else require_int(order_count_value, f"{path}.order_count")
        ),
    )


def parse_depth_update(event: Any) -> DepthSnapshot:
    root = require_event(event, {"depth_update"})
    payload = require_payload(root)
    symbol = require_string(header_field(root, payload, "symbol"), "event.symbol")
    timestamp = optional_datetime(
        root.get("timestamp", payload.get("timestamp")), "event.timestamp"
    )
    bids = require_list(require_field(payload, "bids", "event.data"), "event.data.bids")
    asks = require_list(require_field(payload, "asks", "event.data"), "event.data.asks")
    return DepthSnapshot(
        symbol=symbol,
        timestamp=timestamp,
        bids=tuple(
            _parse_level(row, f"event.data.bids[{index}]") for index, row in enumerate(bids)
        ),
        asks=tuple(
            _parse_level(row, f"event.data.asks[{index}]") for index, row in enumerate(asks)
        ),
    )
