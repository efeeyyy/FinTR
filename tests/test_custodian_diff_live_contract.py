"""Synthetic period-difference contract and local send provenance tests."""

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
from fintr.collector.subscriptions import build_custodian_diff_request
from fintr.data.models import CustodianDiffSnapshot, LiveCustodianDiffResponse
from fintr.data.parsers.common import FieldTypeError, MissingFieldError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market"
FRAME = (FIXTURE_DIR / "custodian_diff_data_live.json").read_text(encoding="utf-8")
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


class LiveCustodianDiffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = json.loads(FRAME)
        self.request = build_custodian_diff_request(
            "AKFIS", date(2026, 9, 14), date(2026, 9, 23)
        )

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

    def test_outbound_request_matches_confirmed_fields(self):
        self.assertEqual(self.request.to_json(),
                         '{"type":"get_custodian_diff","data":{"company":"AKFIS",'
                         '"start":"2026-09-14","end":"2026-09-23"}}')

    def test_typed_parser_preserves_precision_and_response_dates(self):
        result = EventDispatcher().dispatch_frame(FRAME)
        self.assertIsInstance(result.normalized, LiveCustodianDiffResponse)
        self.assertTrue(result.normalization_pending)
        response = result.normalized
        self.assertEqual((response.start_date, response.end_date),
                         (date(2026, 9, 14), date(2026, 9, 22)))
        self.assertEqual(response.results[0].difference, Decimal("-24.625"))
        self.assertEqual(response.results[0].first_percentage,
                         Decimal("12.34567890123456789"))
        self.assertEqual(response.results[0].percentage_change,
                         Decimal("-2.469135690246913569"))
        self.assertEqual(response.results[1].difference, Decimal("0"))
        self.assertNotIn("synthetic-diff-request", repr(result))

    def test_nested_market_update_preserves_live_contract(self):
        result = EventDispatcher().dispatch_frame(json.dumps({
            "type": "market_update", "data": self.event,
        }))
        self.assertEqual(result.wrapper_type, "market_update")
        self.assertIsInstance(result.normalized, LiveCustodianDiffResponse)
        self.assertTrue(result.normalization_pending)

    async def test_legacy_flat_fixture_remains_separate(self):
        legacy = (FIXTURE_DIR / "custodian_diff_data.json").read_text(encoding="utf-8")
        result = (await replay_frames((legacy,)))[0]
        self.assertIsInstance(result.normalized, CustodianDiffSnapshot)
        self.assertFalse(result.normalization_pending)
        self.assertEqual(result.normalized.rows[0].first_value, 30000000)

    def test_malformed_live_envelope_never_falls_back(self):
        event = copy.deepcopy(self.event)
        event.update({"symbol": "AKFIS", "first_date": "2026-09-14",
                      "last_date": "2026-09-22"})
        event["data"].pop("data")
        event["data"]["results"] = []
        with self.assertRaises(MissingFieldError):
            EventDispatcher().dispatch_frame(json.dumps(event))

    def test_missing_null_and_wrong_types_are_rejected(self):
        paths = (
            ("data", "request_id"), ("data", "timestamp"),
            ("data", "processing_time_ms"), ("data", "data", "start_date"),
            ("data", "data", "end_date"), ("data", "data", "source"),
            ("data", "data", "results"),
            *(("data", "data", "results", 0, field) for field in (
                "custodian", "difference", "first_value", "last_value",
                "first_percentage", "last_percentage", "percentage_change"
            )),
        )
        for path in paths:
            for mode in ("missing", "null"):
                with self.subTest(path=path, mode=mode):
                    event = copy.deepcopy(self.event)
                    parent = event
                    for key in path[:-1]:
                        parent = parent[key]
                    if mode == "missing":
                        del parent[path[-1]]
                    else:
                        parent[path[-1]] = None
                    with self.assertRaises(MissingFieldError):
                        EventDispatcher().dispatch_frame(json.dumps(event))
        for key, value in (("difference", "-24.625"), ("first_value", True),
                           ("percentage_change", [])):
            with self.subTest(key=key):
                event = copy.deepcopy(self.event)
                event["data"]["data"]["results"][0][key] = value
                with self.assertRaises(FieldTypeError):
                    EventDispatcher().dispatch_frame(json.dumps(event))
        event = copy.deepcopy(self.event)
        event["data"]["data"]["end_date"] = "bad-date"
        with self.assertRaises(FieldTypeError):
            EventDispatcher().dispatch_frame(json.dumps(event))

    async def test_single_sent_request_keeps_requested_and_returned_ranges(self):
        connection = FakeConnection((WELCOME, FRAME))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(connection.sent, [self.request.to_json()])
        response = results[-1].normalized
        self.assertFalse(results[-1].normalization_pending)
        self.assertEqual(response.request_provenance.symbol, "AKFIS")
        self.assertEqual(response.request_provenance.requested_start, date(2026, 9, 14))
        self.assertEqual(response.request_provenance.requested_end, date(2026, 9, 23))
        self.assertEqual(response.end_date, date(2026, 9, 22))

    async def test_multiple_sent_requests_leave_response_unbound(self):
        second = build_custodian_diff_request("ASELS", date(2026, 9, 14), date(2026, 9, 23))
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

    async def test_request_mutation_after_send_does_not_change_provenance(self):
        connection = FakeConnection(
            (WELCOME, FRAME), on_send=lambda: self.request.data.update({"company": "ASELS"})
        )
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(results[-1].normalized.request_provenance.symbol, "AKFIS")
        self.assertIn('"company":"AKFIS"', connection.sent[0])

    async def test_retry_and_dedup_do_not_reuse_old_context(self):
        limiter = RequestRateLimiter(minimum_interval_seconds=0, deduplication_window_seconds=60)
        first = FakeConnection((WELCOME,), fail_after_frames=True)
        second = FakeConnection((WELCOME, FRAME))
        results = await self.collector((first, second), limiter=limiter, retries=1).collect(
            (self.request,)
        )
        self.assertEqual(first.sent, [self.request.to_json()])
        self.assertEqual(second.sent, [])
        self.assertTrue(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.request_provenance)

    async def test_missing_live_field_reports_safe_path_without_retry(self):
        event = copy.deepcopy(self.event)
        del event["data"]["data"]["results"][0]["percentage_change"]
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
            (1, "parse", "missing_field", "custodian_diff_data",
             "event.data.data.results[0].percentage_change"),
        )


if __name__ == "__main__":
    unittest.main()
