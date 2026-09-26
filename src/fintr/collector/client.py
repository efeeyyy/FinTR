"""Bounded live WebSocket collection and offline replay transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import date
import logging
import re
from time import monotonic
from typing import Any, Protocol, TypeVar

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.events import EventDecodeError, SUPPORTED_EVENT_TYPES, decode_event_frame
from fintr.collector.models import (
    AttemptDiagnostic,
    CollectorConfig,
    CollectorRequest,
    DispatchResult,
    RetryPolicy,
    SendObservation,
)
from fintr.data.parsers.common import (
    FieldTypeError,
    MarketDataParseError,
    MissingFieldError,
    json_kind,
)
from fintr.data.parsers.brokerages_live import (
    brokerage_shape_summary as _brokerages_shape_summary,
)
from fintr.collector.secrets import (
    SecretProvider,
    build_authenticated_url,
    redact_url,
)
from fintr.data.models import (
    LiveAKDResponse,
    LiveCustodianResponse,
    LiveCustodianDiffResponse,
    LiveCustodyAnalysisResponse,
    LiveCustodyDistributionResponse,
    LiveCustodyStatsResponse,
    SentAKDRequestProvenance,
    SentCustodianRequestProvenance,
    SentCustodianDiffRequestProvenance,
    SentCustodyDistributionRequestProvenance,
    SentCustodyAnalysisRequestProvenance,
    SentCustodyStatsRequestProvenance,
)


T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
SendObserver = Callable[[SendObservation], None]
DiagnosticObserver = Callable[[AttemptDiagnostic], None]
_SAFE_REQUEST_TYPES = frozenset({
    "get_akd", "get_brokerages", "get_custodian", "get_custodian_diff",
    "get_custody_distribution", "custody_analysis", "get_custody_stats",
})
_SAFE_FIELD_PATH = re.compile(
    r"^event(?:\.data(?:\.data)?)?(?:\.(?:request_id|timestamp|processing_time_ms|as_of|source|date|start_date|end_date|symbol|dates|periods|akd|custody|data_range|metadata|broker_code|net_size|net_cost|net_percentage|total_size|total_cost|lot_count|results|data|code|values|brokerage|custodian|net|total|size|value|difference|first_value|last_value|first_percentage|last_percentage|percentage_change|percentage|cost|volume)(?:\[[0-9]{1,3}\])?)*$"
)
_ANALYSIS_DATED_PATH = re.compile(
    r"^event\.data\.data\.(?P<family>akd|custody|metadata)\."
    r"(?P<day>\[date\]|[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"(?:\[(?P<index>[0-9]{1,8})\])?"
    r"(?:\.(?P<field>[A-Za-z_]+))?$"
)
_ANALYSIS_ROW_FIELDS = {
    "akd": frozenset({
        "broker_code", "net_size", "net_cost", "net_percentage",
        "total_size", "total_cost",
    }),
    "custody": frozenset({"broker_code", "lot_count", "percentage"}),
    "metadata": frozenset({"retail_pct", "institutional_pct", "investor_count"}),
}
_BROKERAGES_FIELD_PATH = re.compile(
    r"^event\.data\.data_json(?:\[[0-9]{1,8}\])?"
    r"(?:\.(?:code|title|short_title))?$"
)
_SAFE_EXPECTED_KINDS = frozenset({"present", "string", "number", "object", "array", "date", "null"})
_SAFE_ACTUAL_KINDS = frozenset({
    "missing_or_null", "empty_string", "null", "string", "number",
    "boolean", "array", "object", "unknown",
})
_DISTRIBUTION_SHAPE_KEYS = (
    "request_id", "timestamp", "processing_time_ms", "symbol", "data",
    "periods", "results", "source",
)
_AKD_SHAPE_KEYS = (
    "request_id", "timestamp", "processing_time_ms", "data",
    "as_of", "source", "results",
)
_CUSTODY_ANALYSIS_SHAPE_KEYS = (
    "request_id", "timestamp", "processing_time_ms", "data",
    "symbol", "dates", "data_range", "akd", "custody", "metadata",
)
_CUSTODIAN_DIFF_SHAPE_KEYS = (
    "request_id", "timestamp", "processing_time_ms", "data",
    "start_date", "end_date", "source", "results",
)
_SHAPE_KINDS = frozenset({
    "null", "string", "number", "boolean", "array", "object", "unknown",
})
_MISSING_SHAPE_VALUE = object()
_RETRYABLE_TRANSPORT = (OSError, TimeoutError)
# Response event -> (request type whose local send context it may carry, typed response).
_PROVENANCE_ROUTES: dict[str, tuple[str, type]] = {
    "akd_data": ("get_akd", LiveAKDResponse),
    "custodian_data": ("get_custodian", LiveCustodianResponse),
    "custodian_diff_data": ("get_custodian_diff", LiveCustodianDiffResponse),
    "custody_analysis_data": ("custody_analysis", LiveCustodyAnalysisResponse),
    "custody_distribution_data": (
        "get_custody_distribution", LiveCustodyDistributionResponse,
    ),
    "custody_stats_data": ("get_custody_stats", LiveCustodyStatsResponse),
}


def _shape_kind(value: Any) -> str:
    kind = json_kind(value)
    return kind if kind in _SHAPE_KINDS else "unknown"


def _allowlisted_shape_level(
    path: str, value: Mapping[str, Any], keys: Sequence[str],
) -> str:
    fields = ",".join(
        f"{key}:{'missing' if key not in value else _shape_kind(value[key])}"
        for key in keys
    )
    return f"{path}=object{{{fields}}}"


def _custody_distribution_shape_summary(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Summarize only allowlisted wrapper keys and fixed JSON type categories."""

    summary: list[str] = []
    envelope = event.get("data", _MISSING_SHAPE_VALUE)
    if envelope is _MISSING_SHAPE_VALUE:
        summary.append("event.data=missing")
        return tuple(summary)
    if not isinstance(envelope, Mapping):
        summary.append(f"event.data={_shape_kind(envelope)}")
        return tuple(summary)

    summary.append(_allowlisted_shape_level(
        "event.data", envelope, _DISTRIBUTION_SHAPE_KEYS
    ))
    payload = envelope.get("data", _MISSING_SHAPE_VALUE)
    if payload is _MISSING_SHAPE_VALUE:
        summary.append("event.data.data=missing")
    elif isinstance(payload, Mapping):
        summary.append(_allowlisted_shape_level(
            "event.data.data", payload, _DISTRIBUTION_SHAPE_KEYS
        ))
        nested_payload = payload.get("data", _MISSING_SHAPE_VALUE)
        if nested_payload is _MISSING_SHAPE_VALUE:
            summary.append("event.data.data.data=missing")
        elif isinstance(nested_payload, Mapping):
            summary.append(_allowlisted_shape_level(
                "event.data.data.data", nested_payload, _DISTRIBUTION_SHAPE_KEYS
            ))
        else:
            summary.append(
                f"event.data.data.data={_shape_kind(nested_payload)}"
            )
    else:
        summary.append(f"event.data.data={_shape_kind(payload)}")
    return tuple(summary)


def _two_level_shape_summary(
    event: Mapping[str, Any], keys: Sequence[str],
) -> tuple[str, ...]:
    """Summarize event.data/event.data.data using one shared allowlist per family.

    Same allowlisted-keys-only, fixed-JSON-kind-category approach as
    ``_custody_distribution_shape_summary``, without that function's third
    (``event.data.data.data``) nesting level, which these families do not have.
    """

    summary: list[str] = []
    envelope = event.get("data", _MISSING_SHAPE_VALUE)
    if envelope is _MISSING_SHAPE_VALUE:
        summary.append("event.data=missing")
        return tuple(summary)
    if not isinstance(envelope, Mapping):
        summary.append(f"event.data={_shape_kind(envelope)}")
        return tuple(summary)

    summary.append(_allowlisted_shape_level("event.data", envelope, keys))
    payload = envelope.get("data", _MISSING_SHAPE_VALUE)
    if payload is _MISSING_SHAPE_VALUE:
        summary.append("event.data.data=missing")
    elif isinstance(payload, Mapping):
        summary.append(_allowlisted_shape_level("event.data.data", payload, keys))
    else:
        summary.append(f"event.data.data={_shape_kind(payload)}")
    return tuple(summary)


def _akd_shape_summary(event: Mapping[str, Any]) -> tuple[str, ...]:
    return _two_level_shape_summary(event, _AKD_SHAPE_KEYS)


def _custody_analysis_shape_summary(event: Mapping[str, Any]) -> tuple[str, ...]:
    return _two_level_shape_summary(event, _CUSTODY_ANALYSIS_SHAPE_KEYS)


def _custodian_diff_shape_summary(event: Mapping[str, Any]) -> tuple[str, ...]:
    return _two_level_shape_summary(event, _CUSTODIAN_DIFF_SHAPE_KEYS)


def _safe_analysis_path(path: str) -> str | None:
    match = _ANALYSIS_DATED_PATH.fullmatch(path)
    if match is not None:
        family, index, field = (
            match.group("family"), match.group("index"), match.group("field")
        )
        if field is not None and field not in _ANALYSIS_ROW_FIELDS[family]:
            return None
        if family == "metadata" and index is not None:
            return None
        if family != "metadata" and field is not None and index is None:
            return None
        suffix = "[index]" if index is not None else ""
        suffix += f".{field}" if field is not None else ""
        return f"event.data.data.{family}.[date]{suffix}"
    if _SAFE_FIELD_PATH.fullmatch(path):
        return re.sub(r"\[[0-9]+\]", "[index]", path)
    return None


def _safe_field_path(
    exc: MarketDataParseError, event_type: str | None = None,
) -> str | None:
    structured_path = getattr(exc, "field_path", None)
    if event_type == "custody_analysis_data":
        return _safe_analysis_path(structured_path) if isinstance(structured_path, str) else None
    if (event_type == "brokerages_data"
            and isinstance(structured_path, str)
            and _BROKERAGES_FIELD_PATH.fullmatch(structured_path)):
        return re.sub(r"\[[0-9]+\]", "[index]", structured_path)
    candidate = structured_path if isinstance(structured_path, str) else str(exc).split(":", 1)[0]
    if not _SAFE_FIELD_PATH.fullmatch(candidate):
        return None
    if event_type == "custody_distribution_data":
        return re.sub(r"\[[0-9]+\]", "[index]", candidate)
    return candidate


def _handshake_rejection_category(exc: InvalidStatus) -> str:
    """Server refused the WebSocket upgrade; only the numeric HTTP status is kept."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return f"handshake_rejected_http_{status}"
    return "handshake_rejected"


class CollectorAttemptError(RuntimeError):
    """Classified failure without upstream exception text or raw frame."""

    def __init__(self, diagnostic: AttemptDiagnostic, *, retryable: bool) -> None:
        self.diagnostic = diagnostic
        self.retryable = retryable
        super().__init__(
            f"attempt {diagnostic.attempt}: {diagnostic.stage}/{diagnostic.category}"
        )


class CollectorConnectionError(RuntimeError):
    """A redacted terminal error after bounded connection attempts."""


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class ReplayTransport:
    """In-memory transport that behaves like a finite WebSocket frame source."""

    def __init__(self, frames: Iterable[str | bytes | bytearray]) -> None:
        self._frames = iter(tuple(frames))
        self.sent_messages: list[str] = []

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def recv(self) -> str | bytes | bytearray:
        try:
            return next(self._frames)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    def __repr__(self) -> str:
        return "ReplayTransport()"


async def replay_frames(
    frames: Iterable[str | bytes | bytearray],
    dispatcher: EventDispatcher | None = None,
) -> tuple[DispatchResult, ...]:
    """Run sanitized frames through the same dispatcher used by live traffic."""

    transport = ReplayTransport(frames)
    selected_dispatcher = dispatcher or EventDispatcher()
    results: list[DispatchResult] = []
    while True:
        try:
            frame = await transport.recv()
        except StopAsyncIteration:
            break
        results.append(selected_dispatcher.dispatch_frame(frame))
    return tuple(results)


class RequestRateLimiter:
    """Global pacing plus short-window request-key deduplication."""

    def __init__(
        self,
        *,
        minimum_interval_seconds: float = 0.25,
        deduplication_window_seconds: float = 5.0,
        clock: Clock = monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if minimum_interval_seconds < 0 or deduplication_window_seconds < 0:
            raise ValueError("rate-limit durations must be non-negative")
        self._minimum_interval = minimum_interval_seconds
        self._deduplication_window = deduplication_window_seconds
        self._clock = clock
        self._sleep = sleep
        self._last_sent_at: float | None = None
        self._last_by_key: dict[str, float] = {}

    async def acquire(self, request: CollectorRequest) -> bool:
        now = self._clock()
        key = request.deduplication_key()
        previous = self._last_by_key.get(key)
        if previous is not None and now - previous < self._deduplication_window:
            return False

        delay = 0.0
        if self._last_sent_at is not None:
            delay = max(0.0, self._last_sent_at + self._minimum_interval - now)
        if delay:
            await self._sleep(delay)
        sent_at = max(self._clock(), now + delay)
        self._last_sent_at = sent_at
        self._last_by_key[key] = sent_at
        return True


async def run_with_bounded_retry(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Sleep = asyncio.sleep,
    retry_if: Callable[[Exception], bool] | None = None,
) -> T:
    """Retry an async operation a bounded number of times with backoff."""

    delays = policy.delays()
    for attempt in range(policy.max_retries + 1):
        try:
            return await operation()
        except Exception as exc:
            should_retry = (
                retry_if(exc) if retry_if is not None
                else isinstance(exc, _RETRYABLE_TRANSPORT)
            )
            if attempt >= policy.max_retries or not should_retry:
                raise
            await sleep(delays[attempt])
    raise AssertionError("unreachable retry state")


def _silent_websocket_logger() -> logging.Logger:
    logger = logging.getLogger("fintr.collector.websocket.transport")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


class WebSocketCollector:
    """Short-lived collector that never stores or logs authentication data."""

    def __init__(
        self,
        secret_provider: SecretProvider,
        *,
        config: CollectorConfig | None = None,
        retry_policy: RetryPolicy | None = None,
        dispatcher: EventDispatcher | None = None,
        rate_limiter: RequestRateLimiter | None = None,
        connector: Callable[..., Any] = connect,
        sleep: Sleep = asyncio.sleep,
        send_observer: SendObserver | None = None,
        diagnostic_observer: DiagnosticObserver | None = None,
    ) -> None:
        self._secret_provider = secret_provider
        self._config = config or CollectorConfig()
        self._retry_policy = retry_policy or RetryPolicy()
        self._dispatcher = dispatcher or EventDispatcher()
        self._rate_limiter = rate_limiter or RequestRateLimiter()
        self._connector = connector
        self._sleep = sleep
        self._send_observer = send_observer
        self._diagnostic_observer = diagnostic_observer

    def _observe_send(self, request_type: str, status: str, attempt: int) -> None:
        if self._send_observer is not None:
            safe_type = request_type if request_type in _SAFE_REQUEST_TYPES else "[UNKNOWN_REQUEST_TYPE]"
            self._send_observer(SendObservation(attempt, safe_type, status))

    def __repr__(self) -> str:
        return f"WebSocketCollector(endpoint={self._config.endpoint!r})"

    @staticmethod
    def _akd_provenance(request: CollectorRequest) -> SentAKDRequestProvenance | None:
        if request.request_type != "get_akd" or not isinstance(request.data, dict):
            return None
        data = request.data
        symbol, start_text, end_text = (
            data.get("company"), data.get("start"), data.get("end")
        )
        if not all(isinstance(value, str) for value in (symbol, start_text, end_text)):
            return None
        try:
            start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
        except ValueError:
            return None
        if not symbol or start > end:
            return None
        return SentAKDRequestProvenance(symbol=symbol, start=start, end=end)

    @staticmethod
    def _custodian_provenance(
        request: CollectorRequest,
    ) -> SentCustodianRequestProvenance | None:
        if request.request_type != "get_custodian" or not isinstance(request.data, dict):
            return None
        symbol, requested_date_text = (
            request.data.get("company"), request.data.get("date")
        )
        if not isinstance(symbol, str) or not symbol or not isinstance(requested_date_text, str):
            return None
        try:
            requested_date = date.fromisoformat(requested_date_text)
        except ValueError:
            return None
        return SentCustodianRequestProvenance(symbol, requested_date)

    @staticmethod
    def _custodian_diff_provenance(
        request: CollectorRequest,
    ) -> SentCustodianDiffRequestProvenance | None:
        if request.request_type != "get_custodian_diff" or not isinstance(request.data, dict):
            return None
        symbol, start_text, end_text = (
            request.data.get("company"), request.data.get("start"), request.data.get("end")
        )
        if not isinstance(symbol, str) or not symbol or not all(
            isinstance(value, str) for value in (start_text, end_text)
        ):
            return None
        try:
            requested_start = date.fromisoformat(start_text)
            requested_end = date.fromisoformat(end_text)
        except ValueError:
            return None
        if requested_start > requested_end:
            return None
        return SentCustodianDiffRequestProvenance(
            symbol, requested_start, requested_end
        )

    @staticmethod
    def _custody_analysis_provenance(
        request: CollectorRequest,
    ) -> SentCustodyAnalysisRequestProvenance | None:
        if request.request_type != "custody_analysis" or not isinstance(request.data, dict):
            return None
        symbol, from_text, to_text = (
            request.data.get("symbol"), request.data.get("from_date"),
            request.data.get("to_date"),
        )
        if not isinstance(symbol, str) or not symbol or not all(
            isinstance(value, str) for value in (from_text, to_text)
        ):
            return None
        try:
            requested_from = date.fromisoformat(from_text)
            requested_to = date.fromisoformat(to_text)
        except ValueError:
            return None
        if requested_from > requested_to:
            return None
        return SentCustodyAnalysisRequestProvenance(
            symbol, requested_from, requested_to
        )

    @staticmethod
    def _custody_distribution_provenance(
        request: CollectorRequest,
    ) -> SentCustodyDistributionRequestProvenance | None:
        if (request.request_type != "get_custody_distribution"
                or not isinstance(request.data, dict)):
            return None
        symbol = request.data.get("code")
        if not isinstance(symbol, str) or not symbol.strip():
            return None
        return SentCustodyDistributionRequestProvenance(symbol)

    @staticmethod
    def _custody_stats_provenance(
        request: CollectorRequest,
    ) -> SentCustodyStatsRequestProvenance | None:
        if request.request_type != "get_custody_stats" or not isinstance(request.data, dict):
            return None
        symbol, history = request.data.get("symbol"), request.data.get("history")
        if not isinstance(symbol, str) or not symbol.strip() or not isinstance(history, bool):
            return None
        return SentCustodyStatsRequestProvenance(symbol, history)

    @classmethod
    def _request_provenance(cls, request: CollectorRequest) -> Any | None:
        builder = {
            "get_akd": cls._akd_provenance,
            "get_custodian": cls._custodian_provenance,
            "get_custodian_diff": cls._custodian_diff_provenance,
            "custody_analysis": cls._custody_analysis_provenance,
            "get_custody_distribution": cls._custody_distribution_provenance,
            "get_custody_stats": cls._custody_stats_provenance,
        }.get(request.request_type)
        return None if builder is None else builder(request)

    async def _collect_once(
        self,
        connection_url: str,
        requests: Sequence[CollectorRequest],
        attempt: int = 1,
        *,
        stop_when: Callable[[tuple[DispatchResult, ...]], bool] | None = None,
    ) -> tuple[DispatchResult, ...]:
        results: list[DispatchResult] = []
        sent_provenance: dict[str, list[Any | None]] = {
            request_type: [] for request_type, _ in _PROVENANCE_ROUTES.values()
        }
        async with self._connector(
            connection_url,
            origin=self._config.origin,
            open_timeout=self._config.open_timeout_seconds,
            close_timeout=self._config.close_timeout_seconds,
            max_size=self._config.max_message_bytes,
            logger=_silent_websocket_logger(),
        ) as websocket:
            requests_attempted = False
            while len(results) < self._config.max_events:
                try:
                    async with asyncio.timeout(
                        self._config.receive_timeout_seconds
                    ):
                        frame = await websocket.recv()
                except TimeoutError:
                    break
                except _RETRYABLE_TRANSPORT:
                    raise CollectorAttemptError(
                        AttemptDiagnostic(attempt, "receive", "transport_error"),
                        retryable=True,
                    ) from None
                except ConnectionClosed:
                    raise CollectorAttemptError(
                        AttemptDiagnostic(attempt, "receive", "transport_closed"),
                        retryable=False,
                    ) from None
                except Exception:
                    raise CollectorAttemptError(
                        AttemptDiagnostic(attempt, "receive", "internal_error"),
                        retryable=False,
                    ) from None
                try:
                    decoded = decode_event_frame(frame)
                except EventDecodeError:
                    raise CollectorAttemptError(
                        AttemptDiagnostic(attempt, "decode", "invalid_event_frame"),
                        retryable=False,
                    ) from None
                safe_event = decoded.event_type if decoded.event_type in SUPPORTED_EVENT_TYPES else None
                try:
                    result = self._dispatcher.dispatch_decoded(decoded)
                except MarketDataParseError as exc:
                    reason = getattr(exc, "reason", None)
                    category = (
                        "unsupported_metadata" if (
                            safe_event == "custody_analysis_data"
                            and reason == "non_null_metadata_unverified"
                        )
                        else "missing_field" if isinstance(exc, MissingFieldError)
                        else "field_type" if isinstance(exc, FieldTypeError)
                        else "schema_error"
                    )
                    expected = getattr(exc, "expected_kind", None)
                    actual = getattr(exc, "actual_kind", None)
                    shape_summary = ()
                    if safe_event == "custody_distribution_data":
                        shape_summary = _custody_distribution_shape_summary(decoded.event)
                    elif safe_event == "brokerages_data":
                        attached_summary = getattr(exc, "shape_summary", None)
                        shape_summary = (
                            attached_summary
                            if isinstance(attached_summary, tuple)
                            and all(isinstance(item, str) for item in attached_summary)
                            else _brokerages_shape_summary(decoded.event)
                        )
                    elif safe_event == "akd_data":
                        shape_summary = _akd_shape_summary(decoded.event)
                    elif safe_event == "custody_analysis_data":
                        shape_summary = _custody_analysis_shape_summary(decoded.event)
                    elif safe_event == "custodian_diff_data":
                        shape_summary = _custodian_diff_shape_summary(decoded.event)
                    raise CollectorAttemptError(
                        AttemptDiagnostic(
                            attempt, "parse", category, safe_event,
                            _safe_field_path(exc, safe_event),
                            expected if isinstance(expected, str) and expected in _SAFE_EXPECTED_KINDS else None,
                            actual if isinstance(actual, str) and actual in _SAFE_ACTUAL_KINDS else None,
                            shape_summary,
                        ), retryable=False,
                    ) from None
                except Exception:
                    raise CollectorAttemptError(
                        AttemptDiagnostic(attempt, "parse", "internal_error", safe_event),
                        retryable=False,
                    ) from None
                route = _PROVENANCE_ROUTES.get(result.event_type)
                if route is not None and isinstance(result.normalized, route[1]):
                    sent = sent_provenance[route[0]]
                    # Attach local send context only when exactly one sent request explains it.
                    if len(sent) == 1 and sent[0] is not None:
                        provenance = sent[0]
                        if (
                            result.event_type == "custody_analysis_data"
                            and result.normalized.symbol != provenance.symbol
                        ):
                            raise CollectorAttemptError(
                                AttemptDiagnostic(
                                    attempt, "parse", "symbol_mismatch",
                                    "custody_analysis_data",
                                ),
                                retryable=False,
                            )
                        result = replace(
                            result,
                            normalized=replace(
                                result.normalized, request_provenance=provenance
                            ),
                            normalization_pending=False,
                        )
                results.append(result)
                if stop_when is not None and stop_when(tuple(results)):
                    break
                # Provider readiness hypothesis: only a top-level welcome opens sending.
                if (
                    not requests_attempted
                    and result.event_type == "welcome"
                    and result.wrapper_type is None
                ):
                    requests_attempted = True
                    for request in requests:
                        if await self._rate_limiter.acquire(request):
                            # Freeze what is actually serialized before the await.
                            snapshot = CollectorRequest(request.request_type, deepcopy(request.data))
                            wire_json = snapshot.to_json()
                            provenance = self._request_provenance(snapshot)
                            try:
                                await websocket.send(wire_json)
                            except Exception as exc:
                                self._observe_send(request.request_type, "send_failed", attempt)
                                raise CollectorAttemptError(
                                    AttemptDiagnostic(
                                        attempt, "send",
                                        "transport_error" if isinstance(exc, _RETRYABLE_TRANSPORT)
                                        else "transport_closed" if isinstance(exc, ConnectionClosed)
                                        else "internal_error",
                                    ),
                                    retryable=isinstance(exc, _RETRYABLE_TRANSPORT),
                                ) from None
                            self._observe_send(request.request_type, "sent", attempt)
                            if request.request_type in sent_provenance:
                                sent_provenance[request.request_type].append(provenance)
                        else:
                            self._observe_send(request.request_type, "rate_limited", attempt)
            if not requests_attempted:
                for request in requests:
                    self._observe_send(request.request_type, "not_sent_no_welcome", attempt)
        return tuple(results)

    async def collect(
        self,
        requests: Sequence[CollectorRequest],
        *,
        stop_when: Callable[[tuple[DispatchResult, ...]], bool] | None = None,
    ) -> tuple[DispatchResult, ...]:
        init_data = self._secret_provider.get_init_data()
        connection_url = build_authenticated_url(self._config.endpoint, init_data)

        attempt = 0

        async def operation() -> tuple[DispatchResult, ...]:
            nonlocal attempt
            attempt += 1
            try:
                return await self._collect_once(
                    connection_url, requests, attempt, stop_when=stop_when,
                )
            except Exception as exc:
                if isinstance(exc, CollectorAttemptError):
                    failure = exc
                else:
                    retryable = isinstance(exc, _RETRYABLE_TRANSPORT)
                    failure = CollectorAttemptError(
                        AttemptDiagnostic(
                            attempt, "connect",
                            "transport_error" if retryable else
                            "transport_closed" if isinstance(exc, ConnectionClosed)
                            else _handshake_rejection_category(exc)
                            if isinstance(exc, InvalidStatus)
                            else "internal_error",
                        ),
                        retryable=retryable,
                    )
                if self._diagnostic_observer is not None:
                    self._diagnostic_observer(failure.diagnostic)
                raise failure from None

        try:
            return await run_with_bounded_retry(
                operation,
                self._retry_policy,
                sleep=self._sleep,
                retry_if=lambda exc: isinstance(exc, CollectorAttemptError) and exc.retryable,
            )
        except CollectorAttemptError as exc:
            safe_url = redact_url(connection_url)
            raise CollectorConnectionError(
                f"WebSocket collection failed for {safe_url}: {exc}"
            ) from None
        finally:
            init_data = ""
            connection_url = ""
