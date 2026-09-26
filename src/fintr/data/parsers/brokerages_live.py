"""Parser for the JS-derived brokerage candidate accepted by one live smoke."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import json
from typing import Any

from fintr.data.models import LiveBrokerageRow, LiveBrokeragesResponse
from fintr.data.parsers.common import (
    FieldTypeError,
    MarketDataParseError,
    MissingFieldError,
    json_kind,
    require_event,
    require_field,
    require_mapping,
)


MAX_EMBEDDED_JSON_CHARS = 65_536
_SHAPE_KEYS = ("data", "results", "date")
_NO_EMBEDDED = object()
_INVALID_JSON = object()
_OVERSIZE = object()


def _shape_kind(value: Any) -> str:
    kind = json_kind(value)
    return kind if kind in {
        "array", "object", "string", "number", "boolean", "null",
    } else "unknown"


def _object_shape(path: str, value: Mapping[str, Any]) -> str:
    fields = ",".join(
        f"{key}:{'missing' if key not in value else _shape_kind(value[key])}"
        for key in _SHAPE_KEYS
    )
    return f"{path}=object{{{fields}}}"


def brokerage_shape_summary(
    event: Any,
    *,
    embedded_json: Any = _NO_EMBEDDED,
) -> tuple[str, ...]:
    """Return allowlisted kinds only; ``embedded_json`` must already be decoded."""

    if not isinstance(event, Mapping):
        return (f"event={_shape_kind(event)}",)
    if "data" not in event:
        return ("event.data=missing",)
    envelope = event["data"]
    if not isinstance(envelope, Mapping):
        return (f"event.data={_shape_kind(envelope)}",)

    summary = [_object_shape("event.data", envelope)]
    if "data" not in envelope:
        summary.append("event.data.data=missing")
        return tuple(summary)

    payload = envelope["data"]
    if not isinstance(payload, str):
        if isinstance(payload, Mapping):
            summary.append(_object_shape("event.data.data", payload))
        else:
            summary.append(f"event.data.data={_shape_kind(payload)}")
        return tuple(summary)

    summary.append("event.data.data=string")
    if embedded_json is _NO_EMBEDDED:
        return tuple(summary)
    if embedded_json is _INVALID_JSON:
        summary.append("event.data.data_json=invalid_json")
    elif embedded_json is _OVERSIZE:
        summary.append("event.data.data_json=oversize")
    elif isinstance(embedded_json, Mapping):
        summary.append(_object_shape("event.data.data_json", embedded_json))
    else:
        summary.append(f"event.data.data_json={_shape_kind(embedded_json)}")
    return tuple(summary)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _raise_field_type(
    message: str,
    *,
    path: str,
    expected: str,
    actual: str,
    reason: str | None = None,
) -> None:
    raise FieldTypeError(
        message,
        field_path=path,
        expected_kind=expected,
        actual_kind=actual,
        reason=reason,
    )


def _text_field(row: Mapping[str, Any], key: str, path: str) -> str:
    field_path = f"{path}.{key}"
    if key not in row:
        raise MissingFieldError(
            f"{field_path}: required field is missing",
            field_path=field_path,
            expected_kind="present",
            actual_kind="missing_or_null",
        )
    value = row[key]
    if not isinstance(value, str):
        _raise_field_type(
            f"{field_path}: expected string, got {json_kind(value)}",
            path=field_path,
            expected="string",
            actual=json_kind(value),
        )
    return value


def parse_live_brokerages_data(event: Any) -> LiveBrokeragesResponse:
    """Decode a bounded array and validate candidate fields seen in one smoke."""

    embedded_json: Any = _NO_EMBEDDED
    try:
        root = require_event(event, {"get_brokerages", "brokerages_data"})
        envelope = require_mapping(
            require_field(root, "data", "event"), "event.data"
        )
        payload_text = require_field(envelope, "data", "event.data")
        if not isinstance(payload_text, str):
            _raise_field_type(
                f"event.data.data: expected string, got {json_kind(payload_text)}",
                path="event.data.data",
                expected="string",
                actual=json_kind(payload_text),
            )
        if len(payload_text) > MAX_EMBEDDED_JSON_CHARS:
            embedded_json = _OVERSIZE
            _raise_field_type(
                "event.data.data: embedded JSON exceeds the diagnostic size bound",
                path="event.data.data_json",
                expected="array",
                actual="string",
                reason="oversize",
            )
        try:
            embedded_json = json.loads(
                payload_text,
                parse_float=Decimal,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, RecursionError):
            embedded_json = _INVALID_JSON
            _raise_field_type(
                "event.data.data: embedded JSON is invalid",
                path="event.data.data_json",
                expected="array",
                actual="string",
                reason="invalid_json",
            )
        if not isinstance(embedded_json, list):
            _raise_field_type(
                "event.data.data: expected a JSON array",
                path="event.data.data_json",
                expected="array",
                actual=json_kind(embedded_json),
                reason="wrong_top_level_type",
            )

        rows: list[LiveBrokerageRow] = []
        for index, raw_row in enumerate(embedded_json):
            row_path = f"event.data.data_json[{index}]"
            row = require_mapping(raw_row, row_path)
            rows.append(LiveBrokerageRow(
                code=_text_field(row, "code", row_path),
                title=_text_field(row, "title", row_path),
                short_title=_text_field(row, "short_title", row_path),
            ))
        return LiveBrokeragesResponse(rows=tuple(rows))
    except MarketDataParseError as exc:
        # This summary is made from the already-decoded value; never parse it again.
        exc.shape_summary = brokerage_shape_summary(
            event, embedded_json=embedded_json
        )
        raise
