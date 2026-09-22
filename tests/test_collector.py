import asyncio
import json
import logging
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from fintr.collector.assembler import MarketSnapshotAssembler
from fintr.collector.client import (
    CollectorConnectionError,
    RequestRateLimiter,
    WebSocketCollector,
    replay_frames,
    run_with_bounded_retry,
)
from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.events import EventDecodeError, decode_event_frame
from fintr.collector.models import CollectorConfig, RetryPolicy
from fintr.collector.secrets import (
    build_authenticated_url,
    redact_text,
    redact_url,
)
from fintr.collector.subscriptions import (
    build_akd_request,
    build_brokerages_request,
    build_custodian_diff_request,
    build_custodian_request,
    build_custody_analysis_request,
    build_custody_distribution_request,
    build_custody_stats_request,
)
from fintr.data.models import (
    AKDSnapshot,
    BrokerageDirectory,
    CustodianDiffSnapshot,
    CustodianSnapshot,
    CustodyDistributionSnapshot,
    CustodyHistory,
    CustodyStats,
    DepthSnapshot,
)
from fintr.data.parsers import parse_depth_update


FIXTURES = Path(__file__).parent / "fixtures" / "market"
ISTANBUL = timezone(timedelta(hours=3))


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RequestBuilderTests(unittest.TestCase):
    def test_confirmed_requests_serialize_deterministically(self) -> None:
        start = date(2026, 9, 1)
        end = date(2026, 9, 22)
        requests = (
            build_akd_request("asels", start, end),
            build_custodian_request("ASELS", end),
            build_custodian_diff_request("ASELS", start, end),
            build_custody_distribution_request("ASELS"),
            build_custody_analysis_request("ASELS", start, end),
            build_custody_stats_request("ASELS"),
            build_brokerages_request(),
        )
        self.assertEqual(requests[0].request_type, "get_akd")
        self.assertEqual(requests[0].to_dict()["data"]["company"], "ASELS")
        self.assertEqual(
            requests[0].to_json(),
            '{"type":"get_akd","data":{"company":"ASELS","start":"2026-09-01","end":"2026-09-22"}}',
        )
        self.assertEqual(requests[-1].to_json(), '{"type":"get_brokerages"}')
        self.assertTrue(requests[5].to_dict()["data"]["history"])
        self.assertEqual(
            tuple(request.to_json() for request in requests),
            tuple(request.to_json() for request in requests),
        )

    def test_invalid_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "start date"):
            build_akd_request("ASELS", date(2026, 9, 22), date(2026, 9, 1))


class DispatcherTests(unittest.TestCase):
    def test_all_known_fixture_events_dispatch(self) -> None:
        cases = (
            ("depth_update.json", DepthSnapshot),
            ("akd_snapshot.json", AKDSnapshot),
            ("custodian_data.json", CustodianSnapshot),
            ("custodian_diff_data.json", CustodianDiffSnapshot),
            ("custody_distribution_data.json", CustodyDistributionSnapshot),
            ("custody_analysis_data.json", CustodyHistory),
            ("custody_stats_data.json", CustodyStats),
            ("brokerages_data.json", BrokerageDirectory),
        )
        dispatcher = EventDispatcher()
        for filename, expected_type in cases:
            with self.subTest(filename=filename):
                result = dispatcher.dispatch_frame(json.dumps(load(filename)))
                self.assertTrue(result.known)
                self.assertIsInstance(result.normalized, expected_type)

    def test_nested_market_update_is_unwrapped(self) -> None:
        fixture = load("depth_update.json")
        nested = {
            "type": "market_update",
            "data": {
                "type": "depth_update",
                "symbol": fixture["symbol"],
                "timestamp": fixture["timestamp"],
                **fixture["data"],
            },
        }
        result = EventDispatcher().dispatch_frame(json.dumps(nested))
        self.assertTrue(result.known)
        self.assertEqual(result.wrapper_type, "market_update")
        self.assertIsInstance(result.normalized, DepthSnapshot)

    def test_unknown_event_does_not_log_payload(self) -> None:
        logger = logging.getLogger("tests.collector.unknown")
        dispatcher = EventDispatcher(logger)
        secret = "query_id=SECRET-PAYLOAD"
        with self.assertLogs(logger, level="DEBUG") as captured:
            result = dispatcher.dispatch_frame(
                json.dumps(
                    {
                        "type": "future_event",
                        "data": {"init_data": secret},
                    }
                )
            )
        self.assertFalse(result.known)
        self.assertIn("future_event", captured.output[0])
        self.assertNotIn(secret, captured.output[0])

    def test_malformed_json_is_explicit(self) -> None:
        with self.assertRaisesRegex(EventDecodeError, "valid JSON"):
            decode_event_frame("{not-json")


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_transport_uses_dispatcher(self) -> None:
        frames = (
            json.dumps(load("depth_update.json")),
            json.dumps(load("akd_snapshot.json")),
        )
        results = await replay_frames(frames)
        self.assertEqual(
            [result.event_type for result in results],
            ["depth_update", "akd_snapshot"],
        )
        self.assertTrue(all(result.known for result in results))


class SecretSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_connection_url_and_text_are_redacted(self) -> None:
        secret = "query_id=123&user=%7Bsecret%7D"
        url = build_authenticated_url(
            "wss://ws.7k2v9x1r0z8t4m3n5p7w.com/", secret
        )
        safe_url = redact_url(url)
        self.assertEqual(
            safe_url,
            "wss://ws.7k2v9x1r0z8t4m3n5p7w.com/?init_data=[REDACTED]",
        )
        self.assertNotIn(secret, safe_url)
        self.assertNotIn(secret, redact_text(f"failed: {url}", secret))

    async def test_exception_and_repr_do_not_leak_secret(self) -> None:
        secret = "query_id=TOP-SECRET&user=PRIVATE"
        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        class Provider:
            def get_init_data(self) -> str:
                return secret

            def __repr__(self) -> str:
                return f"Provider(secret={secret!r})"

        class FailingConnection:
            def __init__(self, url: str) -> None:
                self.url = url

            async def __aenter__(self):
                raise RuntimeError(f"handshake failed for {self.url}")

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        def connector(url: str, **kwargs):
            return FailingConnection(url)

        collector = WebSocketCollector(
            Provider(),
            retry_policy=RetryPolicy(max_retries=0),
            connector=connector,
        )
        root_logger = logging.getLogger()
        handler = CaptureHandler()
        root_logger.addHandler(handler)
        try:
            self.assertNotIn(secret, repr(collector))
            with self.assertRaises(CollectorConnectionError) as captured:
                await collector.collect((build_brokerages_request(),))
        finally:
            root_logger.removeHandler(handler)
        message = str(captured.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn("TOP-SECRET", message)
        self.assertIn("init_data=[REDACTED]", message)
        self.assertNotIn(secret, "\n".join(record.getMessage() for record in records))

    async def test_browser_origin_is_forwarded_to_connector(self) -> None:
        connector_calls: list[dict] = []

        class Provider:
            def get_init_data(self) -> str:
                return "query_id=SAFE-FIXTURE"

        class EmptyConnection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def send(self, message: str) -> None:
                pass

            async def recv(self) -> str:
                raise TimeoutError

        def connector(url: str, **kwargs):
            connector_calls.append(kwargs)
            return EmptyConnection()

        config = CollectorConfig(receive_timeout_seconds=0.01)
        collector = WebSocketCollector(
            Provider(),
            config=config,
            retry_policy=RetryPolicy(max_retries=0),
            connector=connector,
        )

        await collector.collect((build_brokerages_request(),))

        self.assertEqual(len(connector_calls), 1)
        self.assertEqual(connector_calls[0]["origin"], config.origin)

    def test_origin_must_be_a_bare_https_origin(self) -> None:
        invalid_origins = (
            "http://7k2v9x1r0z8t4m3n5p7w.com",
            "https://7k2v9x1r0z8t4m3n5p7w.com/path",
            "https://7k2v9x1r0z8t4m3n5p7w.com?query=value",
        )
        for origin in invalid_origins:
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(ValueError, "absolute https:// origin"):
                    CollectorConfig(origin=origin)


class WelcomeOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def collect_frames(self, frames, *, max_events=8):
        actions = []
        remaining = iter(frames)

        class Connection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, message):
                actions.append(("send", message))

            async def recv(self):
                try:
                    frame = next(remaining)
                except StopIteration:
                    actions.append(("wait", None))
                    # Exercise the collector's real bounded receive timeout.
                    await asyncio.Future()
                actions.append(("recv", frame["type"]))
                return json.dumps(frame)

        provider = Mock()
        connector = Mock(return_value=Connection())
        config = CollectorConfig(max_events=max_events, receive_timeout_seconds=0.01)
        collector = WebSocketCollector(
            provider, config=config, connector=connector,
            rate_limiter=RequestRateLimiter(
                minimum_interval_seconds=0, deduplication_window_seconds=0,
            ),
        )
        # Use only the transport path with a fake connector; no secret is requested.
        results = await collector._collect_once(config.endpoint, (build_brokerages_request(),))
        provider.get_init_data.assert_not_called()
        connector.assert_called_once()
        return actions, results

    async def test_welcome_precedes_single_send_despite_repeated_controls(self):
        with self.assertLogs("fintr.collector.dispatcher", level="DEBUG") as logs:
            actions, results = await self.collect_frames((
                {"type": "welcome", "data": {"opaque": "DO_NOT_PRINT"}},
                {"type": "hb"}, {"type": "welcome"}, {"type": "hb"},
                load("brokerages_data.json"),
            ))
        self.assertEqual(actions[:2], [
            ("recv", "welcome"), ("send", '{"type":"get_brokerages"}'),
        ])
        self.assertEqual(sum(kind == "send" for kind, _ in actions), 1)
        self.assertFalse(results[0].known)
        self.assertIsNone(results[0].normalized)
        self.assertIsInstance(results[-1].normalized, BrokerageDirectory)
        self.assertNotIn("DO_NOT_PRINT", "\n".join(logs.output))

    async def test_hb_before_welcome_does_not_trigger_send(self):
        actions, _ = await self.collect_frames(({"type": "hb"}, {"type": "welcome"}))
        self.assertEqual(actions[:3], [
            ("recv", "hb"), ("recv", "welcome"),
            ("send", '{"type":"get_brokerages"}'),
        ])

    async def test_no_welcome_timeout_sends_nothing(self):
        actions, results = await self.collect_frames(())
        self.assertEqual(actions, [("wait", None)])
        self.assertEqual(results, ())

    async def test_other_events_do_not_open_sending(self):
        for event_type in ("hb", "error", "banned"):
            with self.subTest(event_type=event_type):
                actions, results = await self.collect_frames(({"type": event_type},))
                self.assertFalse(any(kind == "send" for kind, _ in actions))
                self.assertEqual(len(results), 1)

    async def test_frame_limit_applies_while_waiting_for_welcome(self):
        actions, results = await self.collect_frames((
            {"type": "hb"}, {"type": "hb"}, {"type": "welcome"},
        ), max_events=2)
        self.assertEqual(actions, [("recv", "hb"), ("recv", "hb")])
        self.assertEqual(len(results), 2)

    async def test_nested_welcome_does_not_open_sending(self):
        actions, _ = await self.collect_frames((
            {"type": "market_update", "data": {"type": "welcome"}},
        ))
        self.assertFalse(any(kind == "send" for kind, _ in actions))


class RateAndRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_request_is_suppressed_within_window(self) -> None:
        current = [100.0]
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)
            current[0] += delay

        limiter = RequestRateLimiter(
            minimum_interval_seconds=0.25,
            deduplication_window_seconds=5.0,
            clock=lambda: current[0],
            sleep=sleep,
        )
        request = build_brokerages_request()
        self.assertTrue(await limiter.acquire(request))
        self.assertFalse(await limiter.acquire(request))
        current[0] += 5.0
        self.assertTrue(await limiter.acquire(request))

    async def test_retry_is_bounded_and_exponential(self) -> None:
        attempts = 0
        delays: list[float] = []

        async def operation():
            nonlocal attempts
            attempts += 1
            raise OSError("offline")

        async def sleep(delay: float) -> None:
            delays.append(delay)

        policy = RetryPolicy(
            max_retries=2,
            initial_delay_seconds=0.5,
            multiplier=2,
            max_delay_seconds=4,
        )
        with self.assertRaises(OSError):
            await run_with_bounded_retry(operation, policy, sleep=sleep)
        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [0.5, 1.0])


class AssemblerTests(unittest.TestCase):
    def test_stale_component_does_not_replace_newer_snapshot(self) -> None:
        base = parse_depth_update(load("depth_update.json"))
        newer = replace(
            base,
            timestamp=datetime(2026, 9, 22, 11, 0, tzinfo=ISTANBUL),
        )
        older = replace(
            base,
            timestamp=datetime(2026, 9, 22, 9, 0, tzinfo=ISTANBUL),
        )
        assembler = MarketSnapshotAssembler()
        received_new = datetime(2026, 9, 22, 11, 0, 1, tzinfo=ISTANBUL)
        received_old = datetime(2026, 9, 22, 11, 1, tzinfo=ISTANBUL)

        self.assertTrue(assembler.ingest(newer, received_at=received_new))
        self.assertFalse(assembler.ingest(older, received_at=received_old))
        snapshot = assembler.get("GLRMK")
        self.assertEqual(snapshot.depth.timestamp, newer.timestamp)
        self.assertEqual(snapshot.as_of, received_new)
        self.assertEqual(
            assembler.component_received_at("GLRMK")["depth"], received_new
        )


if __name__ == "__main__":
    unittest.main()
