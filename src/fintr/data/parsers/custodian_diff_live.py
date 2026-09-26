"""Parser for the observed nested custody period-difference response."""

from __future__ import annotations

from typing import Any

from fintr.data.models import LiveCustodianDiffResponse, LiveCustodianDiffRow
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


def parse_live_custodian_diff(event: Any) -> LiveCustodianDiffResponse:
    root = require_event(event, {"custodian_diff_data"})
    envelope = require_payload(root)
    request_id = require_string(
        require_field(envelope, "request_id", "event.data"), "event.data.request_id"
    )
    timestamp = require_decimal(
        require_field(envelope, "timestamp", "event.data"), "event.data.timestamp"
    )
    processing_time_ms = require_decimal(
        require_field(envelope, "processing_time_ms", "event.data"),
        "event.data.processing_time_ms",
    )
    payload = require_mapping(
        require_field(envelope, "data", "event.data"), "event.data.data"
    )
    start_date = require_date(
        require_field(payload, "start_date", "event.data.data"),
        "event.data.data.start_date",
    )
    end_date = require_date(
        require_field(payload, "end_date", "event.data.data"),
        "event.data.data.end_date",
    )
    source = require_string(
        require_field(payload, "source", "event.data.data"), "event.data.data.source"
    )
    rows = require_list(
        require_field(payload, "results", "event.data.data"), "event.data.data.results"
    )
    parsed_rows: list[LiveCustodianDiffRow] = []
    numeric_fields = (
        "difference", "first_value", "last_value", "first_percentage",
        "last_percentage", "percentage_change",
    )
    for index, value in enumerate(rows):
        path = f"event.data.data.results[{index}]"
        row = require_mapping(value, path)
        parsed_rows.append(LiveCustodianDiffRow(
            custodian=require_string(
                require_field(row, "custodian", path), f"{path}.custodian"
            ),
            **{
                key: require_decimal(require_field(row, key, path), f"{path}.{key}")
                for key in numeric_fields
            },
        ))
    return LiveCustodianDiffResponse(
        request_id=request_id,
        timestamp=timestamp,
        processing_time_ms=processing_time_ms,
        start_date=start_date,
        end_date=end_date,
        source=source,
        results=tuple(parsed_rows),
    )
