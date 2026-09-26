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
    _akd_shape_summary,
    _custodian_diff_shape_summary,
    _custody_analysis_shape_summary,
    replay_frames,
    run_with_bounded_retry,
)
from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.events import EventDecodeError, decode_event_frame
from fintr.collector.models import AttemptDiagnostic, CollectorConfig, RetryPolicy
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


class NewFamilyShapeSummaryUnitTests(unittest.TestCase):
    """Direct unit coverage of the akd/custody_analysis/custodian_diff shape
    summary helpers: allowlisted keys and fixed JSON kind categories only,
    never values, never array contents, and never unknown key names."""

    def test_akd_shape_summary_hides_unknown_key_and_secret_value(self) -> None:
        secret = "https://evil.example/?initData=SECRET-TOKEN"
        summary = _akd_shape_summary({
            "type": "akd_data",
            "data": {
                "request_id": "r1",
                "timestamp": 1,
                "processing_time_ms": 2,
                "data": {
                    "as_of": None,
                    "results": [],
                    "unknown_secret_field": secret,
                },
            },
        })
        joined = " | ".join(summary)
        self.assertIn("event.data=object{", joined)
        self.assertIn("source:missing", joined)
        self.assertIn("results:array", joined)
        self.assertIn("as_of:null", joined)
        self.assertNotIn("unknown_secret_field", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("SECRET", joined)

    def test_akd_shape_summary_handles_missing_and_non_mapping_levels(self) -> None:
        self.assertEqual(_akd_shape_summary({"type": "akd_data"}), ("event.data=missing",))
        self.assertEqual(
            _akd_shape_summary({"type": "akd_data", "data": "PRIVATE-STRING"}),
            ("event.data=string",),
        )
        summary = _akd_shape_summary({"type": "akd_data", "data": {"data": 5}})
        self.assertIn("event.data.data=number", summary)

    def test_custody_analysis_shape_summary_hides_unknown_key_and_secret_value(self) -> None:
        secret = "wss://secret.example/ws?auth_date=1700000000"
        summary = _custody_analysis_shape_summary({
            "type": "custody_analysis_data",
            "data": {
                "request_id": "r1",
                "timestamp": 1,
                "processing_time_ms": 2,
                "data": {
                    "dates": [],
                    "unknown_secret_field": secret,
                },
            },
        })
        joined = " | ".join(summary)
        self.assertIn("symbol:missing", joined)
        self.assertIn("dates:array", joined)
        self.assertNotIn("unknown_secret_field", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("secret.example", joined)

    def test_custodian_diff_shape_summary_hides_unknown_key_and_secret_value(self) -> None:
        secret = "query_id=TOP-SECRET&user=PRIVATE"
        summary = _custodian_diff_shape_summary({
            "type": "custodian_diff_data",
            "data": {
                "request_id": "r1",
                "timestamp": 1,
                "processing_time_ms": 2,
                "data": {
                    "end_date": "2026-09-22",
                    "unknown_secret_field": secret,
                },
            },
        })
        joined = " | ".join(summary)
        self.assertIn("start_date:missing", joined)
        self.assertIn("end_date:string", joined)
        self.assertNotIn("unknown_secret_field", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("TOP-SECRET", joined)


class NewFamilyShapeSummaryDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a real parse failure through the collector attaches a
    shape summary built only from the allowlisted helpers above."""

    async def _diagnostic_for(self, frames, request):
        remaining = iter(frames)

        class Connection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, message):
                pass

            async def recv(self):
                try:
                    frame = next(remaining)
                except StopIteration:
                    await asyncio.Future()
                return json.dumps(frame)

        provider = Mock()
        provider.get_init_data.return_value = "query_id=SAFE-FIXTURE"
        connector = Mock(return_value=Connection())
        config = CollectorConfig(max_events=8, receive_timeout_seconds=0.01)
        diagnostics: list = []
        collector = WebSocketCollector(
            provider, config=config, connector=connector,
            retry_policy=RetryPolicy(max_retries=0),
            rate_limiter=RequestRateLimiter(
                minimum_interval_seconds=0, deduplication_window_seconds=0,
            ),
            diagnostic_observer=diagnostics.append,
        )
        with self.assertRaises(CollectorConnectionError):
            await collector.collect((request,))
        self.assertEqual(len(diagnostics), 1)
        return diagnostics[0]

    async def test_akd_missing_source_failure_carries_safe_shape_summary(self) -> None:
        secret = "http://evil.example/SECRET-TOKEN"
        diagnostic = await self._diagnostic_for((
            {"type": "welcome"},
            {
                "type": "akd_data",
                "data": {
                    "request_id": "r1", "timestamp": 1, "processing_time_ms": 2,
                    "data": {"as_of": None, "results": [], "leak": secret},
                },
            },
        ), build_akd_request("ASELS", date(2026, 9, 1), date(2026, 9, 1)))
        self.assertEqual(diagnostic.field_path, "event.data.data.source")
        joined = " | ".join(diagnostic.shape_summary)
        self.assertIn("source:missing", joined)
        self.assertIn("results:array", joined)
        self.assertNotIn("leak", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("SECRET", joined)

    async def test_custody_analysis_missing_symbol_failure_carries_safe_shape_summary(self) -> None:
        secret = "wss://secret.example/ws?auth_date=1700000000"
        diagnostic = await self._diagnostic_for((
            {"type": "welcome"},
            {
                "type": "custody_analysis_data",
                "data": {
                    "request_id": "r1", "timestamp": 1, "processing_time_ms": 2,
                    "data": {"dates": [], "leak": secret},
                },
            },
        ), build_custody_analysis_request("ASELS", date(2026, 9, 1), date(2026, 9, 1)))
        self.assertEqual(diagnostic.field_path, "event.data.data.symbol")
        joined = " | ".join(diagnostic.shape_summary)
        self.assertIn("symbol:missing", joined)
        self.assertIn("dates:array", joined)
        self.assertNotIn("leak", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("secret.example", joined)

    async def test_custodian_diff_missing_start_date_failure_carries_safe_shape_summary(self) -> None:
        secret = "query_id=TOP-SECRET&user=PRIVATE"
        diagnostic = await self._diagnostic_for((
            {"type": "welcome"},
            {
                "type": "custodian_diff_data",
                "data": {
                    "request_id": "r1", "timestamp": 1, "processing_time_ms": 2,
                    "data": {"end_date": "2026-09-22", "leak": secret},
                },
            },
        ), build_custodian_diff_request("ASELS", date(2026, 9, 1), date(2026, 9, 1)))
        self.assertEqual(diagnostic.field_path, "event.data.data.start_date")
        joined = " | ".join(diagnostic.shape_summary)
        self.assertIn("start_date:missing", joined)
        self.assertIn("end_date:string", joined)
        self.assertNotIn("leak", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("TOP-SECRET", joined)


class EarlyStopTests(unittest.IsolatedAsyncioTestCase):
    """WebSocketCollector.collect/_collect_once optional stop_when predicate."""

    async def _collect(self, frames, *, stop_when=None, max_events=8):
        actions: list = []
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
        results = await collector._collect_once(
            config.endpoint, (build_brokerages_request(),), stop_when=stop_when,
        )
        return actions, results

    async def test_stop_when_ends_attempt_without_waiting_for_further_frames(self) -> None:
        def stop_when(results):
            return any(result.event_type == "brokerages_data" for result in results)

        actions, results = await self._collect((
            {"type": "welcome"},
            load("brokerages_data.json"),
            {"type": "hb"}, {"type": "hb"}, {"type": "hb"},
        ), stop_when=stop_when)
        # Only the welcome + brokerages_data frames were received; the trailing
        # hb frames were never read because the attempt stopped and closed.
        self.assertEqual(
            [value for kind, value in actions if kind == "recv"],
            ["welcome", "brokerages_data"],
        )
        self.assertEqual(
            [result.event_type for result in results], ["welcome", "brokerages_data"],
        )

    async def test_no_stop_when_keeps_default_behavior(self) -> None:
        actions, results = await self._collect((
            {"type": "welcome"}, {"type": "hb"}, {"type": "hb"},
        ), stop_when=None)
        self.assertEqual(
            [value for kind, value in actions if kind == "recv"],
            ["welcome", "hb", "hb"],
        )
        self.assertEqual(len(results), 3)

    async def test_stop_when_false_for_missing_response_keeps_collecting(self) -> None:
        def stop_when(results):
            return any(result.event_type == "brokerages_data" for result in results)

        actions, results = await self._collect((
            {"type": "welcome"}, {"type": "hb"}, {"type": "hb"},
        ), stop_when=stop_when, max_events=3)
        # brokerages_data never arrives, so stop_when never fires; every
        # supplied frame is still consumed up to the configured max_events.
        self.assertEqual(
            [value for kind, value in actions if kind == "recv"],
            ["welcome", "hb", "hb"],
        )
        self.assertEqual(len(results), 3)

    async def test_stop_when_is_evaluated_after_each_append_with_growing_tuple(self) -> None:
        seen_lengths: list[int] = []

        def stop_when(results):
            seen_lengths.append(len(results))
            return False

        await self._collect(({"type": "welcome"}, {"type": "hb"}), stop_when=stop_when)
        self.assertEqual(seen_lengths, [1, 2])


if __name__ == "__main__":
    unittest.main()


class HandshakeRejectionDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_rejection_is_reported_with_status_only(self) -> None:
        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response

        from fintr.collector.client import CollectorConnectionError, WebSocketCollector
        from fintr.collector.models import CollectorRequest, RetryPolicy

        class Provider:
            def get_init_data(self) -> str:
                return "query_id=SECRET-HANDSHAKE&hash=abc"

        class Rejecting:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                raise InvalidStatus(Response(401, "Unauthorized", Headers()))

            async def __aexit__(self, *exc) -> bool:
                return False

        diagnostics = []
        collector = WebSocketCollector(
            Provider(), retry_policy=RetryPolicy(max_retries=0), connector=Rejecting,
            diagnostic_observer=diagnostics.append,
        )
        with self.assertRaises(CollectorConnectionError) as captured:
            await collector.collect((CollectorRequest("get_brokerages"),))
        self.assertEqual([(d.stage, d.category) for d in diagnostics],
                         [("connect", "handshake_rejected_http_401")])
        self.assertNotIn("SECRET-HANDSHAKE", str(captured.exception) + repr(diagnostics))
