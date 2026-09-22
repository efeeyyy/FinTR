"""Typed collector configuration, request, and dispatch models."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import urlsplit


DEFAULT_ENDPOINT = "wss://ws.7k2v9x1r0z8t4m3n5p7w.com/"
DEFAULT_ORIGIN = "https://7k2v9x1r0z8t4m3n5p7w.com"


@dataclass(frozen=True, slots=True)
class CollectorRequest:
    request_type: str
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.request_type}
        if self.data is not None:
            result["data"] = deepcopy(self.data)
        return result

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def deduplication_key(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class DispatchResult:
    event_type: str
    known: bool
    normalized: Any | None
    wrapper_type: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    endpoint: str = DEFAULT_ENDPOINT
    origin: str = DEFAULT_ORIGIN
    open_timeout_seconds: float = 10.0
    receive_timeout_seconds: float = 5.0
    close_timeout_seconds: float = 5.0
    max_message_bytes: int = 1_048_576
    max_events: int = 20

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "wss" or not parsed.netloc:
            raise ValueError("endpoint must be an absolute wss:// URL")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain query or fragment data")
        parsed_origin = urlsplit(self.origin)
        if (
            parsed_origin.scheme != "https"
            or not parsed_origin.netloc
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise ValueError("origin must be an absolute https:// origin")
        if self.max_events < 1:
            raise ValueError("max_events must be positive")
        if self.max_message_bytes < 1:
            raise ValueError("max_message_bytes must be positive")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 2
    initial_delay_seconds: float = 0.5
    multiplier: float = 2.0
    max_delay_seconds: float = 4.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")

    def delays(self) -> tuple[float, ...]:
        values: list[float] = []
        delay = self.initial_delay_seconds
        for _ in range(self.max_retries):
            values.append(min(delay, self.max_delay_seconds))
            delay *= self.multiplier
        return tuple(values)
