"""Strict JSON-like parsing helpers shared by market-data parsers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn


class MarketDataParseError(ValueError):
    """Base class for invalid upstream market-data payloads."""

    def __init__(
        self,
        message: str,
        *,
        field_path: str | None = None,
        expected_kind: str | None = None,
        actual_kind: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.field_path = field_path
        self.expected_kind = expected_kind
        self.actual_kind = actual_kind
        self.reason = reason


class MissingFieldError(MarketDataParseError):
    """A required field is absent or null."""


class FieldTypeError(MarketDataParseError):
    """A field has a type incompatible with the normalized schema."""


def json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "unknown"


def _type_error(path: str, expected: str, value: Any) -> NoReturn:
    expected_kind = {
        "object": "object",
        "array": "array",
        "non-empty string": "string",
        "integer": "number",
        "JSON number": "number",
    }.get(expected)
    raise FieldTypeError(
        f"{path}: expected {expected}, got {type(value).__name__}",
        field_path=path,
        expected_kind=expected_kind,
        actual_kind=json_kind(value),
    )


def require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _type_error(path, "object", value)
    return value


def require_list(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _type_error(path, "array", value)
    return value


def require_field(source: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in source or source[key] is None:
        raise MissingFieldError(
            f"{path}.{key}: required field is missing",
            field_path=f"{path}.{key}",
            expected_kind="present",
            actual_kind="missing_or_null",
        )
    return source[key]


def optional_field(source: Mapping[str, Any], key: str) -> Any | None:
    return source.get(key)


def require_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _type_error(path, "non-empty string", value)
    value = value.strip()
    if not value:
        raise MissingFieldError(
            f"{path}: required string is empty",
            field_path=path,
            expected_kind="string",
            actual_kind="empty_string",
        )
    return value


def require_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _type_error(path, "integer", value)
    return value


def require_decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        _type_error(path, "JSON number", value)
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise FieldTypeError(
            f"{path}: invalid decimal number", field_path=path,
            expected_kind="number", actual_kind="number", reason="invalid_number",
        ) from exc
    if not result.is_finite():
        raise FieldTypeError(
            f"{path}: number must be finite", field_path=path,
            expected_kind="number", actual_kind="number", reason="non_finite_number",
        )
    return result


def optional_decimal(value: Any | None, path: str) -> Decimal | None:
    return None if value is None else require_decimal(value, path)


def require_date(value: Any, path: str) -> date:
    text = require_string(value, path)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FieldTypeError(
            f"{path}: expected ISO date YYYY-MM-DD", field_path=path,
            expected_kind="date", actual_kind="string", reason="invalid_date",
        ) from exc


def optional_datetime(value: Any | None, path: str) -> datetime | None:
    if value is None:
        return None
    text = require_string(value, path)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FieldTypeError(f"{path}: expected ISO-8601 datetime") from exc


def require_event(event: Any, allowed_types: set[str]) -> Mapping[str, Any]:
    root = require_mapping(event, "event")
    event_type = require_string(require_field(root, "type", "event"), "event.type")
    if event_type not in allowed_types:
        raise MarketDataParseError(
            f"event.type: expected one of {sorted(allowed_types)}, got {event_type!r}"
        )
    return root


def require_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return require_mapping(require_field(event, "data", "event"), "event.data")


def header_field(
    event: Mapping[str, Any], payload: Mapping[str, Any], key: str, path: str = "event"
) -> Any:
    if key in event and event[key] is not None:
        return event[key]
    return require_field(payload, key, f"{path}.data")
