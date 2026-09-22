"""Raw JSON frame validation and safe market_update unwrapping."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


SUPPORTED_EVENT_TYPES: tuple[str, ...] = (
    "depth_update",
    "akd_snapshot",
    "custodian_data",
    "custodian_diff_data",
    "custody_distribution_data",
    "custody_analysis_data",
    "custody_stats_data",
    "brokerages_data",
)

_HEADER_FIELDS = {
    "symbol",
    "timestamp",
    "date",
    "first_date",
    "last_date",
    "market_price",
}


class EventDecodeError(ValueError):
    """A raw WebSocket frame isn't a valid event envelope."""


@dataclass(frozen=True, slots=True)
class DecodedEvent:
    event_type: str
    event: dict[str, Any]
    wrapper_type: str | None = None


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EventDecodeError(f"{path} must be a JSON object")
    return value


def _event_type(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventDecodeError(f"{path} must be a non-empty string")
    return value.strip()


def _unwrap_market_update(root: Mapping[str, Any]) -> DecodedEvent:
    nested = _mapping(root.get("data"), "event.data")
    nested_type = _event_type(nested.get("type"), "event.data.type")
    if isinstance(nested.get("data"), Mapping):
        event = dict(nested)
        event["type"] = nested_type
        return DecodedEvent(nested_type, event, wrapper_type="market_update")

    event: dict[str, Any] = {"type": nested_type}
    payload: dict[str, Any] = {}
    for key, value in nested.items():
        if key == "type":
            continue
        if key in _HEADER_FIELDS:
            event[key] = value
        else:
            payload[key] = value
    event["data"] = payload
    return DecodedEvent(nested_type, event, wrapper_type="market_update")


def decode_event_frame(frame: str | bytes | bytearray) -> DecodedEvent:
    """Decode a JSON frame without logging or retaining the raw payload."""

    if isinstance(frame, bytearray):
        frame = bytes(frame)
    if isinstance(frame, bytes):
        try:
            frame = frame.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EventDecodeError("frame must be valid UTF-8") from exc
    if not isinstance(frame, str):
        raise EventDecodeError("frame must be str or bytes")
    try:
        parsed = json.loads(frame)
    except json.JSONDecodeError as exc:
        raise EventDecodeError("frame is not valid JSON") from exc
    root = _mapping(parsed, "event")
    top_type = _event_type(root.get("type"), "event.type")
    if top_type == "market_update":
        return _unwrap_market_update(root)
    return DecodedEvent(top_type, dict(root))
