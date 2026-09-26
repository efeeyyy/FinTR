"""Synthetic daily custody contract and local send provenance tests."""

import copy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import unittest

from fintr.collector.client import (
    CollectorConnectionError, RequestRateLimiter, WebSocketCollector, replay_frames,
)
from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.models import CollectorConfig, RetryPolicy
from fintr.collector.subscriptions import build_custodian_request
from fintr.data.models import CustodianSnapshot, LiveCustodianResponse
from fintr.data.parsers.common import FieldTypeError, MissingFieldError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market"
FRAME = (FIXTURE_DIR / "custodian_data_live.json").read_text(encoding="utf-8")
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
        if self.on_send is not None:
            self.on_send()

    async def recv(self):
        try:
            return next(self.frames)
        except StopIteration:
            if self.fail_after_frames:
                raise OSError("synthetic transport drop")
            raise TimeoutError


class LiveCustodianTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = json.loads(FRAME)
        self.request = build_custodian_request("AKFIS", date(2026, 9, 23))

    def collector(self, connections, *, limiter=None, retries=0, diagnostic_observer=None):
        queue = iter(connections)

        class Provider:
            def get_init_data(self):
                return "synthetic-secret"

        return WebSocketCollector(
            Provider(), connector=lambda *args, **kwargs: next(queue),
            config=CollectorConfig(max_events=5, receive_timeout_seconds=0.01),
            retry_policy=RetryPolicy(max_retries=retries, initial_delay_seconds=0),
            rate_limiter=limiter or RequestRateLimiter(
                minimum_interval_seconds=0, deduplication_window_seconds=0
            ),
            diagnostic_observer=diagnostic_observer,
        )

    def test_typed_parser_preserves_decimal_tokens_and_response_date(self):
        result = EventDispatcher().dispatch_frame(FRAME)
        self.assertIsInstance(result.normalized, LiveCustodianResponse)
        self.assertTrue(result.normalization_pending)  # No local send in replay.
        self.assertEqual(result.normalized.date, date(2026, 9, 22))
        self.assertEqual(result.normalized.results[0].value, Decimal("123.4567890123456789"))
        self.assertEqual(result.normalized.results[0].percentage,
                         Decimal("0.12345678901234567890123456789"))
        self.assertEqual(result.normalized.results[1].value, Decimal("42"))
        self.assertNotIn("synthetic-custodian-request", repr(result))

    async def test_legacy_flat_fixture_remains_distinct(self):
        legacy = (FIXTURE_DIR / "custodian_data.json").read_text(encoding="utf-8")
        result = (await replay_frames((legacy,)))[0]
        self.assertIsInstance(result.normalized, CustodianSnapshot)
        self.assertFalse(result.normalization_pending)
        self.assertEqual(result.normalized.rows[0].value, 31500432)

    def test_malformed_live_envelope_never_falls_back_to_legacy(self):
        event = copy.deepcopy(self.event)
        event["symbol"] = "AKFIS"
        event["date"] = "2026-09-22"
        event["data"].pop("data")
        event["data"]["results"] = []
        with self.assertRaises(MissingFieldError):
            EventDispatcher().dispatch_frame(json.dumps(event))

    def test_required_fields_and_numeric_types_are_strict(self):
        paths = (
            ("data", "request_id"), ("data", "timestamp"),
            ("data", "processing_time_ms"), ("data", "data", "date"),
            ("data", "data", "source"), ("data", "data", "results"),
            ("data", "data", "results", 0, "custodian"),
            ("data", "data", "results", 0, "value"),
            ("data", "data", "results", 0, "percentage"),
        )
        for path in paths:
            with self.subTest(path=path):
                event = copy.deepcopy(self.event)
                parent = event
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with self.assertRaises(MissingFieldError):
                    EventDispatcher().dispatch_frame(json.dumps(event))
        for key, value in (("value", "123.4"), ("percentage", True)):
            with self.subTest(key=key):
                event = copy.deepcopy(self.event)
                event["data"]["data"]["results"][0][key] = value
                with self.assertRaises(FieldTypeError):
                    EventDispatcher().dispatch_frame(json.dumps(event))

    async def test_single_sent_request_preserves_requested_and_returned_dates(self):
        connection = FakeConnection((WELCOME, FRAME))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(connection.sent, [self.request.to_json()])
        response = results[-1].normalized
        self.assertFalse(results[-1].normalization_pending)
        self.assertEqual(response.request_provenance.symbol, "AKFIS")
        self.assertEqual(response.request_provenance.requested_date, date(2026, 9, 23))
        self.assertEqual(response.date, date(2026, 9, 22))

    async def test_request_mutation_after_send_cannot_change_local_provenance(self):
        connection = FakeConnection(
            (WELCOME, FRAME),
            on_send=lambda: self.request.data.update({"company": "ASELS"}),
        )
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(results[-1].normalized.request_provenance.symbol, "AKFIS")
        self.assertIn('"company":"AKFIS"', connection.sent[0])

    async def test_missing_live_date_reports_safe_field_without_retry(self):
        event = copy.deepcopy(self.event)
        del event["data"]["data"]["date"]
        first = FakeConnection((WELCOME, json.dumps(event)))
        second = FakeConnection((WELCOME, FRAME))
        diagnostics = []
        with self.assertRaises(CollectorConnectionError):
            await self.collector(
                (first, second), retries=1, diagnostic_observer=diagnostics.append
            ).collect((self.request,))
        self.assertEqual(second.sent, [])
        self.assertEqual(
            (diagnostics[0].attempt, diagnostics[0].stage, diagnostics[0].category,
             diagnostics[0].event_type, diagnostics[0].field_path),
            (1, "parse", "missing_field", "custodian_data", "event.data.data.date"),
        )

    async def test_multiple_sent_requests_leave_response_unbound(self):
        second = build_custodian_request("ASELS", date(2026, 9, 23))
        connection = FakeConnection((WELCOME, FRAME))
        results = await self.collector((connection,)).collect((self.request, second))
        self.assertEqual(len(connection.sent), 2)
        self.assertTrue(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.request_provenance)

    async def test_unsolicited_response_is_unbound(self):
        connection = FakeConnection((FRAME,))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(connection.sent, [])
        self.assertTrue(results[-1].normalization_pending)

    async def test_dedup_skip_and_retry_do_not_inherit_old_send(self):
        limiter = RequestRateLimiter(
            minimum_interval_seconds=0, deduplication_window_seconds=60
        )
        first = FakeConnection((WELCOME,), fail_after_frames=True)
        second = FakeConnection((WELCOME, FRAME))
        results = await self.collector((first, second), limiter=limiter, retries=1).collect(
            (self.request,)
        )
        self.assertEqual(first.sent, [self.request.to_json()])
        self.assertEqual(second.sent, [])
        self.assertTrue(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.request_provenance)


if __name__ == "__main__":
    unittest.main()
