"""Authentication-separated WebSocket market-data collection."""

from fintr.collector.assembler import MarketSnapshotAssembler
from fintr.collector.client import (
    CollectorConnectionError,
    ReplayTransport,
    RequestRateLimiter,
    WebSocketCollector,
    replay_frames,
    run_with_bounded_retry,
)
from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.events import (
    SUPPORTED_EVENT_TYPES,
    DecodedEvent,
    EventDecodeError,
    decode_event_frame,
)
from fintr.collector.models import (
    CollectorConfig,
    CollectorRequest,
    DispatchResult,
    RetryPolicy,
)
from fintr.collector.secrets import (
    InteractiveSecretProvider,
    SecretProvider,
    build_authenticated_url,
    redact_text,
    redact_url,
)

__all__ = [
    "SUPPORTED_EVENT_TYPES",
    "CollectorConfig",
    "CollectorConnectionError",
    "CollectorRequest",
    "DecodedEvent",
    "DispatchResult",
    "EventDecodeError",
    "EventDispatcher",
    "InteractiveSecretProvider",
    "MarketSnapshotAssembler",
    "ReplayTransport",
    "RequestRateLimiter",
    "RetryPolicy",
    "SecretProvider",
    "WebSocketCollector",
    "build_authenticated_url",
    "decode_event_frame",
    "redact_text",
    "redact_url",
    "replay_frames",
    "run_with_bounded_retry",
]
