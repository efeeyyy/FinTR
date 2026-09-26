"""Observed subset of the nested custody analysis response."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from fintr.data.models import (
    LiveAnalysisAKDDay,
    LiveAnalysisAKDRow,
    LiveAnalysisCustodyDay,
    LiveAnalysisCustodyRow,
    LiveAnalysisMetadataDay,
    LiveAnalysisRange,
    LiveCustodyAnalysisResponse,
)
from fintr.data.parsers.common import (
    FieldTypeError,
    json_kind,
    require_date,
    require_decimal,
    require_event,
    require_field,
    require_list,
    require_mapping,
    require_payload,
    require_string,
)


_KNOWN_METADATA_FIELDS = ("retail_pct", "institutional_pct", "investor_count")


def _parse_akd_days(value: Any) -> tuple[LiveAnalysisAKDDay, ...]:
    days = require_mapping(value, "event.data.data.akd")
    parsed: list[LiveAnalysisAKDDay] = []
    for date_text, raw_rows in days.items():
        day = require_date(date_text, "event.data.data.akd.[date]")
        path = f"event.data.data.akd.{date_text}"
        rows = require_list(raw_rows, path)
        parsed_rows: list[LiveAnalysisAKDRow] = []
        for index, value in enumerate(rows):
            row_path = f"{path}[{index}]"
            row = require_mapping(value, row_path)
            parsed_rows.append(LiveAnalysisAKDRow(
                broker_code=require_string(
                    require_field(row, "broker_code", row_path), f"{row_path}.broker_code"
                ),
                **{
                    key: require_decimal(
                        require_field(row, key, row_path), f"{row_path}.{key}"
                    )
                    for key in (
                        "net_size", "net_cost", "net_percentage", "total_size",
                        "total_cost",
                    )
                },
            ))
        parsed.append(LiveAnalysisAKDDay(day, tuple(parsed_rows)))
    return tuple(parsed)


def _parse_custody_days(value: Any) -> tuple[LiveAnalysisCustodyDay, ...]:
    days = require_mapping(value, "event.data.data.custody")
    parsed: list[LiveAnalysisCustodyDay] = []
    for date_text, raw_rows in days.items():
        day = require_date(date_text, "event.data.data.custody.[date]")
        path = f"event.data.data.custody.{date_text}"
        rows = require_list(raw_rows, path)
        parsed_rows: list[LiveAnalysisCustodyRow] = []
        for index, value in enumerate(rows):
            row_path = f"{path}[{index}]"
            row = require_mapping(value, row_path)
            parsed_rows.append(LiveAnalysisCustodyRow(
                broker_code=require_string(
                    require_field(row, "broker_code", row_path), f"{row_path}.broker_code"
                ),
                lot_count=require_decimal(
                    require_field(row, "lot_count", row_path), f"{row_path}.lot_count"
                ),
                percentage=require_decimal(
                    require_field(row, "percentage", row_path), f"{row_path}.percentage"
                ),
            ))
        parsed.append(LiveAnalysisCustodyDay(day, tuple(parsed_rows)))
    return tuple(parsed)


def _parse_investor_count(value: Any, path: str) -> int:
    # First seen non-null in a one-year live response (2026-09-27) as a JSON
    # number; only a non-negative whole number is a plausible count.
    number = None
    if not isinstance(value, bool) and isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
    if (number is None or not number.is_finite() or number < 0
            or number != number.to_integral_value()):
        raise FieldTypeError(
            f"{path}: expected a non-negative whole-number investor count",
            field_path=path, expected_kind="number", actual_kind=json_kind(value),
            reason="non_null_metadata_unverified",
        )
    return int(number)


def _parse_metadata(value: Any) -> tuple[LiveAnalysisMetadataDay, ...]:
    days = require_mapping(value, "event.data.data.metadata")
    parsed: list[LiveAnalysisMetadataDay] = []
    for date_text, raw_entry in days.items():
        day = require_date(date_text, "event.data.data.metadata.[date]")
        path = f"event.data.data.metadata.{date_text}"
        entry: Mapping[str, Any] = require_mapping(raw_entry, path)
        null_fields: list[str] = []
        retail_pct: Decimal | None = None
        institutional_pct: Decimal | None = None
        investor_count: int | None = None
        for key in _KNOWN_METADATA_FIELDS:
            if key in entry:
                if entry[key] is None:
                    null_fields.append(key)
                elif key in ("retail_pct", "institutional_pct"):
                    parsed_value = require_decimal(entry[key], f"{path}.{key}")
                    if key == "retail_pct":
                        retail_pct = parsed_value
                    else:
                        institutional_pct = parsed_value
                else:
                    investor_count = _parse_investor_count(entry[key], f"{path}.{key}")
        uninterpreted_keys = tuple(key for key in entry if key not in _KNOWN_METADATA_FIELDS)
        parsed.append(LiveAnalysisMetadataDay(
            day, tuple(null_fields), uninterpreted_keys, retail_pct,
            institutional_pct, investor_count,
        ))
    return tuple(parsed)


def parse_live_custody_analysis(event: Any) -> LiveCustodyAnalysisResponse:
    root = require_event(event, {"custody_analysis_data"})
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
    symbol = require_string(
        require_field(payload, "symbol", "event.data.data"), "event.data.data.symbol"
    )
    raw_dates = require_list(
        require_field(payload, "dates", "event.data.data"), "event.data.data.dates"
    )
    dates = tuple(
        require_date(value, f"event.data.data.dates[{index}]")
        for index, value in enumerate(raw_dates)
    )
    raw_range = require_mapping(
        require_field(payload, "data_range", "event.data.data"),
        "event.data.data.data_range",
    )
    data_range = LiveAnalysisRange(**{
        key: require_date(
            require_field(raw_range, key, "event.data.data.data_range"),
            f"event.data.data.data_range.{key}",
        )
        for key in ("requested_from", "requested_to", "actual_from")
    })
    return LiveCustodyAnalysisResponse(
        request_id=request_id,
        timestamp=timestamp,
        processing_time_ms=processing_time_ms,
        symbol=symbol,
        dates=dates,
        akd=_parse_akd_days(require_field(payload, "akd", "event.data.data")),
        custody=_parse_custody_days(
            require_field(payload, "custody", "event.data.data")
        ),
        data_range=data_range,
        metadata=_parse_metadata(
            require_field(payload, "metadata", "event.data.data")
        ),
    )
