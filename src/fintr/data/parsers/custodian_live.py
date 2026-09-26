"""Parser for the observed nested daily custody query response."""

from __future__ import annotations

from typing import Any

from fintr.data.models import LiveCustodianResponse, LiveCustodianRow
from fintr.data.parsers.common import (
    require_date,
    require_decimal,
    require_event,
    require_field,
    require_list,
    require_mapping,
    require_payload,
    require_string,
)


def parse_live_custodian_data(event: Any) -> LiveCustodianResponse:
    root = require_event(event, {"custodian_data"})
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
    response_date = require_date(
        require_field(payload, "date", "event.data.data"), "event.data.data.date"
    )
    source = require_string(
        require_field(payload, "source", "event.data.data"), "event.data.data.source"
    )
    rows = require_list(
        require_field(payload, "results", "event.data.data"), "event.data.data.results"
    )
    parsed_rows: list[LiveCustodianRow] = []
    for index, value in enumerate(rows):
        path = f"event.data.data.results[{index}]"
        row = require_mapping(value, path)
        parsed_rows.append(LiveCustodianRow(
            custodian=require_string(
                require_field(row, "custodian", path), f"{path}.custodian"
            ),
            value=require_decimal(require_field(row, "value", path), f"{path}.value"),
            percentage=require_decimal(
                require_field(row, "percentage", path), f"{path}.percentage"
            ),
        ))
    return LiveCustodianResponse(
        request_id=request_id,
        timestamp=timestamp,
        processing_time_ms=processing_time_ms,
        date=response_date,
        source=source,
        results=tuple(parsed_rows),
    )
