"""Typed parser for the observed nested custody-stats response contract."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from fintr.data.models import (
    LiveCustodyStatsFormat,
    LiveCustodyStatsPoint,
    LiveCustodyStatsResponse,
    LiveCustodyStatsSeries,
)
from fintr.data.parsers.common import (
    FieldTypeError,
    MissingFieldError,
    optional_decimal,
    require_date,
    require_event,
    require_field,
    require_list,
    require_mapping,
)


def _request_id(envelope: Mapping[str, Any]) -> str | None:
    path = "event.data.request_id"
    if "request_id" not in envelope:
        raise MissingFieldError(
            f"{path}: required field is missing", field_path=path,
            expected_kind="present", actual_kind="missing_or_null",
        )
    value = envelope["request_id"]
    if value is not None and not isinstance(value, str):
        raise FieldTypeError(
            f"{path}: expected string or null", field_path=path,
            expected_kind="string", actual_kind="number" if isinstance(
                value, (int, float, Decimal)
            ) and not isinstance(value, bool) else "boolean" if isinstance(value, bool)
            else "array" if isinstance(value, list) else "object" if isinstance(value, Mapping)
            else "unknown",
        )
    return value


def _format(value: Any, path: str) -> LiveCustodyStatsFormat:
    raw = require_mapping(value, path)
    fields: list[tuple[str, str | int | bool | Decimal | None]] = []
    for key, item in raw.items():
        if isinstance(item, (str, int, bool, Decimal)) or item is None:
            fields.append((str(key), item))
        else:
            raise FieldTypeError(
                f"{path}.{key}: expected scalar display metadata",
                field_path=f"{path}.{key}", expected_kind="present",
                actual_kind=("array" if isinstance(item, list) else
                             "object" if isinstance(item, Mapping) else "unknown"),
            )
    return LiveCustodyStatsFormat(tuple(fields))


def _series(value: Any, key: str) -> LiveCustodyStatsSeries:
    path = f"event.data.data.{key}"
    root = require_mapping(value, path)
    display_format = _format(
        require_field(root, "format", path), f"{path}.format"
    )
    raw_points = require_list(
        require_field(root, "data", path), f"{path}.data"
    )
    points: list[LiveCustodyStatsPoint] = []
    for index, raw in enumerate(raw_points):
        point_path = f"{path}.data[{index}]"
        row = require_mapping(raw, point_path)
        if "value" not in row:
            raise MissingFieldError(
                f"{point_path}.value: required field is missing",
                field_path=f"{point_path}.value", expected_kind="present",
                actual_kind="missing_or_null",
            )
        raw_value = row["value"]
        if "label" not in row:
            raise MissingFieldError(
                f"{point_path}.label: required field is missing",
                field_path=f"{point_path}.label", expected_kind="present",
                actual_kind="missing_or_null",
            )
        if row["label"] is not None:
            raise FieldTypeError(
                f"{point_path}.label: expected null", field_path=f"{point_path}.label",
                expected_kind="null", actual_kind="string" if isinstance(
                    row["label"], str
                ) else "number" if isinstance(row["label"], (int, float, Decimal))
                and not isinstance(row["label"], bool) else "boolean" if isinstance(
                    row["label"], bool
                ) else "array" if isinstance(row["label"], list) else "object" if isinstance(
                    row["label"], Mapping
                ) else "unknown",
            )
        points.append(LiveCustodyStatsPoint(
            date=require_date(
                require_field(row, "date", point_path), f"{point_path}.date"
            ),
            value=optional_decimal(raw_value, f"{point_path}.value"),
            label=None,
        ))
    return LiveCustodyStatsSeries(display_format, tuple(points))


def parse_live_custody_stats(event: Any) -> LiveCustodyStatsResponse:
    root = require_event(event, {"get_custody_stats", "custody_stats_data"})
    envelope = require_mapping(
        require_field(root, "data", "event"), "event.data"
    )
    request_id = _request_id(envelope)
    cached = require_field(envelope, "cached", "event.data")
    if not isinstance(cached, bool):
        raise FieldTypeError(
            "event.data.cached: expected boolean", field_path="event.data.cached",
            expected_kind="boolean", actual_kind="null" if cached is None else
            "number" if isinstance(cached, (int, float, Decimal)) else
            "string" if isinstance(cached, str) else "array" if isinstance(cached, list)
            else "object" if isinstance(cached, Mapping) else "unknown",
        )
    payload = require_mapping(
        require_field(envelope, "data", "event.data"), "event.data.data"
    )
    return LiveCustodyStatsResponse(
        request_id=request_id,
        cached=cached,
        retail_percentage=_series(
            require_field(payload, "retail_percentage", "event.data.data"),
            "retail_percentage",
        ),
        institution_percentage=_series(
            require_field(payload, "institution_percentage", "event.data.data"),
            "institution_percentage",
        ),
        investor_count=_series(
            require_field(payload, "investor_count", "event.data.data"),
            "investor_count",
        ),
    )
