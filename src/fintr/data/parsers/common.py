"""Strict JSON-like parsing helpers shared by market-data parsers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn


class MarketDataParseError(ValueError):
    """Base class for invalid upstream market-data payloads."""


class MissingFieldError(MarketDataParseError):
    """A required field is absent or null."""


class FieldTypeError(MarketDataParseError):
    """A field has a type incompatible with the normalized schema."""


def _type_error(path: str, expected: str, value: Any) -> NoReturn:
    raise FieldTypeError(f"{path}: expected {expected}, got {type(value).__name__}")


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
        raise MissingFieldError(f"{path}.{key}: required field is missing")
    return source[key]


def optional_field(source: Mapping[str, Any], key: str) -> Any | None:
    return source.get(key)


def require_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _type_error(path, "non-empty string", value)
    value = value.strip()
    if not value:
        raise MissingFieldError(f"{path}: required string is empty")
    return value


def require_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _type_error(path, "integer", value)
    return value


def require_decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _type_error(path, "JSON number", value)
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise FieldTypeError(f"{path}: invalid decimal number") from exc
    if not result.is_finite():
        raise FieldTypeError(f"{path}: number must be finite")
    return result


def optional_decimal(value: Any | None, path: str) -> Decimal | None:
    return None if value is None else require_decimal(value, path)


def require_date(value: Any, path: str) -> date:
    text = require_string(value, path)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FieldTypeError(f"{path}: expected ISO date YYYY-MM-DD") from exc


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
