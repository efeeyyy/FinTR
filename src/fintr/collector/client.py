"""Bounded live WebSocket collection and offline replay transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
import logging
from time import monotonic
from typing import Any, Protocol, TypeVar

from websockets.asyncio.client import connect

from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.models import (
    CollectorConfig,
    CollectorRequest,
    DispatchResult,
    RetryPolicy,
)
from fintr.collector.secrets import (
    SecretProvider,
    build_authenticated_url,
    redact_text,
    redact_url,
)


T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


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
) -> T:
    """Retry an async operation a bounded number of times with backoff."""

    delays = policy.delays()
    for attempt in range(policy.max_retries + 1):
        try:
            return await operation()
        except Exception:
            if attempt >= policy.max_retries:
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
    ) -> None:
        self._secret_provider = secret_provider
        self._config = config or CollectorConfig()
        self._retry_policy = retry_policy or RetryPolicy()
        self._dispatcher = dispatcher or EventDispatcher()
        self._rate_limiter = rate_limiter or RequestRateLimiter()
        self._connector = connector
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"WebSocketCollector(endpoint={self._config.endpoint!r})"

    async def _collect_once(
        self,
        connection_url: str,
        requests: Sequence[CollectorRequest],
    ) -> tuple[DispatchResult, ...]:
        results: list[DispatchResult] = []
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
                result = self._dispatcher.dispatch_frame(frame)
                results.append(result)
                # Provider readiness hypothesis: only a top-level welcome opens sending.
                if (
                    not requests_attempted
                    and result.event_type == "welcome"
                    and result.wrapper_type is None
                ):
                    requests_attempted = True
                    for request in requests:
                        if await self._rate_limiter.acquire(request):
                            await websocket.send(request.to_json())
        return tuple(results)

    async def collect(
        self,
        requests: Sequence[CollectorRequest],
    ) -> tuple[DispatchResult, ...]:
        init_data = self._secret_provider.get_init_data()
        connection_url = build_authenticated_url(self._config.endpoint, init_data)

        async def operation() -> tuple[DispatchResult, ...]:
            return await self._collect_once(connection_url, requests)

        try:
            return await run_with_bounded_retry(
                operation,
                self._retry_policy,
                sleep=self._sleep,
            )
        except Exception as exc:
            safe_url = redact_url(connection_url)
            safe_error = redact_text(str(exc), init_data, connection_url)
            raise CollectorConnectionError(
                f"WebSocket collection failed for {safe_url}: {safe_error}"
            ) from None
        finally:
            init_data = ""
            connection_url = ""
