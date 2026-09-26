"""Parser for the observed nested custody distribution response."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fintr.data.models import (
    LiveCustodyDistributionResponse,
    LiveCustodyDistributionRow,
)
from fintr.data.parsers.common import (
    FieldTypeError,
    optional_decimal,
    require_date,
    require_decimal,
    require_event,
    require_field,
    require_list,
    require_mapping,
    require_payload,
    require_string,
)


def _nullable_decimal_field(row: Mapping[str, Any], key: str, path: str):
    if key not in row:
        require_field(row, key, path)
    return optional_decimal(row[key], f"{path}.{key}")


def parse_live_custody_distribution(event: Any) -> LiveCustodyDistributionResponse:
    root = require_event(event, {"custody_distribution_data"})
    envelope = require_payload(root)
    request_id = require_string(
        require_field(envelope, "request_id", "event.data"),
        "event.data.request_id",
    )
    timestamp = require_decimal(
        require_field(envelope, "timestamp", "event.data"),
        "event.data.timestamp",
    )
    processing_time_ms = require_decimal(
        require_field(envelope, "processing_time_ms", "event.data"),
        "event.data.processing_time_ms",
    )
    wrapper = require_mapping(
        require_field(envelope, "data", "event.data"), "event.data.data"
    )
    nested = wrapper.get("data")
    direct_candidate = (
        "periods" in wrapper or isinstance(wrapper.get("data"), list)
    )
    nested_candidate = (
        isinstance(nested, Mapping)
        and ("periods" in nested or "data" in nested)
    )
    if direct_candidate and nested_candidate:
        raise FieldTypeError(
            "event.data.data: conflicting distribution payload paths",
            field_path="event.data.data.periods",
            expected_kind="array",
            actual_kind="array",
            reason="ambiguous_distribution_payload",
        )
    if nested_candidate:
        payload = require_mapping(nested, "event.data.data.data")
        payload_path = "event.data.data.data"
        source_candidates = tuple(
            (path, container)
            for path, container in (
                ("event.data.data", wrapper),
                ("event.data.data.data", payload),
            )
            if "source" in container
        )
        if len(source_candidates) > 1:
            raise FieldTypeError(
                "event.data.data: conflicting source fields",
                field_path="event.data.data.source",
                expected_kind="string",
                actual_kind="string",
                reason="ambiguous_distribution_source",
            )
        if source_candidates:
            source_path, source_container = source_candidates[0]
            source = require_string(
                source_container["source"], f"{source_path}.source"
            )
        else:
            source = require_string(
                require_field(wrapper, "source", "event.data.data"),
                "event.data.data.source",
            )
    else:
        payload = wrapper
        payload_path = "event.data.data"
        source = require_string(
            require_field(payload, "source", payload_path),
            f"{payload_path}.source",
        )
    raw_periods = require_list(
        require_field(payload, "periods", payload_path),
        f"{payload_path}.periods",
    )
    periods = tuple(
        require_date(value, f"{payload_path}.periods[{index}]")
        for index, value in enumerate(raw_periods)
    )
    rows_path = f"{payload_path}.data"
    raw_rows = require_list(
        require_field(payload, "data", payload_path), rows_path,
    )
    rows: list[LiveCustodyDistributionRow] = []
    for index, raw_row in enumerate(raw_rows):
        path = f"{rows_path}[{index}]"
        row = require_mapping(raw_row, path)
        raw_values = require_list(
            require_field(row, "values", path), f"{path}.values"
        )
        if len(raw_values) != len(periods):
            raise FieldTypeError(
                f"{path}.values: length must match periods",
                field_path=f"{path}.values",
                expected_kind="array",
                actual_kind="array",
                reason="period_values_length_mismatch",
            )
        values = tuple(
            optional_decimal(value, f"{path}.values[{value_index}]")
            for value_index, value in enumerate(raw_values)
        )
        rows.append(LiveCustodyDistributionRow(
            code=require_string(
                require_field(row, "code", path), f"{path}.code"
            ),
            values=values,
            percentage=_nullable_decimal_field(row, "percentage", path),
        ))
    return LiveCustodyDistributionResponse(
        request_id=request_id,
        timestamp=timestamp,
        processing_time_ms=processing_time_ms,
        periods=periods,
        rows=tuple(rows),
        source=source,
    )
