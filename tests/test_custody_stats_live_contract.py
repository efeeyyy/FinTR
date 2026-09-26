"""Offline tests for the separate live custody-stats contract and provenance."""

import copy
from decimal import Decimal
import json
from pathlib import Path
import unittest

from fintr.collector.client import (
    CollectorConnectionError,
    RequestRateLimiter,
    WebSocketCollector,
    replay_frames,
)
from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.models import CollectorConfig, RetryPolicy
from fintr.collector.subscriptions import build_custody_stats_request
from fintr.data.models import CustodyStats, LiveCustodyStatsResponse
from fintr.data.parsers.common import FieldTypeError, MissingFieldError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market"
LIVE_FRAME = (FIXTURE_DIR / "custody_stats_data_live.json").read_text(encoding="utf-8")
LEGACY_FRAME = (FIXTURE_DIR / "custody_stats_data.json").read_text(encoding="utf-8")
WELCOME = '{"type":"welcome"}'


class FakeConnection:
    def __init__(self, frames, *, fail_after_frames=False, on_send=None):
        self.frames = iter(frames)
        self.fail_after_frames = fail_after_frames
        self.on_send = on_send
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        self.sent.append(message)
        if self.on_send:
            self.on_send()

    async def recv(self):
        try:
            return next(self.frames)
        except StopIteration:
            if self.fail_after_frames:
                raise OSError("synthetic transport interruption")
            raise TimeoutError


class LiveCustodyStatsContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = json.loads(LIVE_FRAME)
        self.request = build_custody_stats_request("AKFIS")

    def collector(
        self, connections, *, limiter=None, retries=0, max_events=2,
    ):
        queue = iter(connections)

        class Provider:
            def get_init_data(self):
                return "synthetic-secret"

        return WebSocketCollector(
            Provider(), connector=lambda *args, **kwargs: next(queue),
            config=CollectorConfig(max_events=max_events, receive_timeout_seconds=0.01),
            retry_policy=RetryPolicy(max_retries=retries, initial_delay_seconds=0),
            rate_limiter=limiter or RequestRateLimiter(
                minimum_interval_seconds=0, deduplication_window_seconds=0
            ),
        )

    async def test_live_shape_preserves_decimal_null_format_and_nullable_metadata(self):
        result = EventDispatcher().dispatch_frame(LIVE_FRAME)
        self.assertTrue(result.known)
        self.assertTrue(result.normalization_pending)
        response = result.normalized
        self.assertIsInstance(response, LiveCustodyStatsResponse)
        self.assertIsNone(response.request_id)
        self.assertTrue(response.cached)
        self.assertEqual(
            response.retail_percentage.points[0].value,
            Decimal("12.34567890123456789"),
        )
        self.assertIsNone(response.retail_percentage.points[1].value)
        self.assertIsNone(response.retail_percentage.points[0].label)
        self.assertEqual(response.retail_percentage.format.fields,
                         (("decimals", 2), ("suffix", "%")))
        self.assertEqual(response.investor_count.format.fields, (("thousand", True),))
        self.assertEqual(response.investor_count.points[0].value, Decimal("4321"))
        self.assertFalse(hasattr(response, "symbol"))
        self.assertIsNone(response.request_provenance)
        self.assertNotIn("private-request-id", repr(response))

    async def test_nullable_request_id_can_also_be_a_redacted_string(self):
        event = copy.deepcopy(self.event)
        event["data"]["request_id"] = "private-request-id"
        response = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertEqual(response.request_id, "private-request-id")
        self.assertNotIn("private-request-id", repr(response))

    async def test_live_required_fields_and_types_are_validated(self):
        missing = copy.deepcopy(self.event)
        del missing["data"]["request_id"]
        with self.assertRaises(MissingFieldError):
            EventDispatcher().dispatch_frame(json.dumps(missing))

        invalid = copy.deepcopy(self.event)
        invalid["data"]["cached"] = 1
        with self.assertRaises(FieldTypeError):
            EventDispatcher().dispatch_frame(json.dumps(invalid))

        invalid = copy.deepcopy(self.event)
        invalid["data"]["data"]["retail_percentage"]["data"][0]["label"] = "not-null"
        with self.assertRaises(FieldTypeError):
            EventDispatcher().dispatch_frame(json.dumps(invalid))

        invalid = copy.deepcopy(self.event)
        del invalid["data"]["data"]["investor_count"]["data"][0]["value"]
        with self.assertRaises(MissingFieldError):
            EventDispatcher().dispatch_frame(json.dumps(invalid))

        invalid = copy.deepcopy(self.event)
        invalid["data"]["data"]["institution_percentage"]["data"][0]["value"] = True
        with self.assertRaises(FieldTypeError):
            EventDispatcher().dispatch_frame(json.dumps(invalid))

    async def test_nested_market_update_route_is_live_typed(self):
        wrapped = {
            "type": "market_update",
            "data": {
                "type": "custody_stats_data",
                "data": copy.deepcopy(self.event["data"]),
            },
        }
        result = EventDispatcher().dispatch_frame(json.dumps(wrapped))
        self.assertEqual(result.wrapper_type, "market_update")
        self.assertIsInstance(result.normalized, LiveCustodyStatsResponse)

    async def test_legacy_flat_fixture_keeps_existing_model_and_parser(self):
        result = EventDispatcher().dispatch_frame(LEGACY_FRAME)
        self.assertIsInstance(result.normalized, CustodyStats)
        self.assertFalse(result.normalization_pending)

    async def test_live_format_marker_never_falls_back_to_legacy_parser(self):
        event = json.loads(LEGACY_FRAME)
        event["data"]["retail_percentage"]["format"] = {"suffix": "%"}
        with self.assertRaises(MissingFieldError) as captured:
            EventDispatcher().dispatch_frame(json.dumps(event))
        self.assertEqual(captured.exception.field_path, "event.data.request_id")

    async def test_one_successful_send_binds_only_local_query_provenance(self):
        connection = FakeConnection((WELCOME, LIVE_FRAME))
        results = await self.collector((connection,)).collect((self.request,))
        result = next(item for item in results if item.event_type == "custody_stats_data")
        self.assertFalse(result.normalization_pending)
        self.assertEqual(result.normalized.request_provenance.symbol, "AKFIS")
        self.assertIs(result.normalized.request_provenance.history, True)
        self.assertIsNone(result.normalized.request_id)
        self.assertTrue(result.normalized.cached)
        self.assertEqual(len(connection.sent), 1)
        self.assertEqual(json.loads(connection.sent[0])["data"], {
            "symbol": "AKFIS", "history": True,
        })

    async def test_multiple_stats_sends_do_not_bind_response(self):
        connection = FakeConnection((WELCOME, LIVE_FRAME))
        requests = (
            build_custody_stats_request("AKFIS", history=True),
            build_custody_stats_request("AKFIS", history=False),
        )
        results = await self.collector((connection,)).collect(requests)
        result = next(item for item in results if item.event_type == "custody_stats_data")
        self.assertEqual(len(connection.sent), 2)
        self.assertTrue(result.normalization_pending)
        self.assertIsNone(result.normalized.request_provenance)

    async def test_no_welcome_means_no_local_provenance(self):
        result = next(
            item for item in await self.collector((FakeConnection((LIVE_FRAME,)),))
            .collect((self.request,)) if item.event_type == "custody_stats_data"
        )
        self.assertTrue(result.normalization_pending)
        self.assertIsNone(result.normalized.request_provenance)

    async def test_rate_limited_request_does_not_bind_cached_response(self):
        limiter = RequestRateLimiter(
            minimum_interval_seconds=0, deduplication_window_seconds=60
        )
        await limiter.acquire(self.request)
        result = next(
            item for item in await self.collector(
                (FakeConnection((WELCOME, LIVE_FRAME)),), limiter=limiter,
            ).collect((self.request,)) if item.event_type == "custody_stats_data"
        )
        self.assertTrue(result.normalized.cached)
        self.assertTrue(result.normalization_pending)
        self.assertIsNone(result.normalized.request_provenance)

    async def test_sent_request_snapshot_is_immune_to_later_caller_mutation(self):
        def mutate_request():
            self.request.data["symbol"] = "OTHER"

        connection = FakeConnection((WELCOME, LIVE_FRAME), on_send=mutate_request)
        results = await self.collector((connection,)).collect((self.request,))
        response = next(
            item.normalized for item in results
            if item.event_type == "custody_stats_data"
        )
        self.assertEqual(response.request_provenance.symbol, "AKFIS")
        self.assertEqual(json.loads(connection.sent[0])["data"]["symbol"], "AKFIS")

    async def test_retry_attempt_does_not_inherit_prior_send_provenance(self):
        first = FakeConnection((WELCOME,), fail_after_frames=True)
        second = FakeConnection((LIVE_FRAME,))  # no welcome, therefore no send
        results = await self.collector((first, second), retries=1).collect((self.request,))
        response = next(
            item.normalized for item in results
            if item.event_type == "custody_stats_data"
        )
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(second.sent, [])
        self.assertIsNone(response.request_provenance)


if __name__ == "__main__":
    unittest.main()
