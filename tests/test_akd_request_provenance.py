"""Offline AKD send/response attribution tests; no WebSocket or auth needed."""

import json
from datetime import date
from pathlib import Path
import unittest

from fintr.collector.client import (
    CollectorConnectionError, RequestRateLimiter, WebSocketCollector, _safe_field_path,
)
from fintr.collector.models import CollectorConfig, RetryPolicy
from fintr.collector.models import CollectorRequest
from fintr.collector.subscriptions import build_akd_request
from fintr.data.models import LiveAKDResponse
from fintr.data.parsers.common import FieldTypeError


AKD_FRAME = (Path(__file__).parent / "fixtures" / "market" / "akd_data_partial.json").read_text(encoding="utf-8")
WELCOME = '{"type":"welcome"}'


class FakeConnection:
    def __init__(self, frames, *, fail_on_recv=False, fail_on_send=False, on_send=None):
        self.frames = iter(frames)
        self.sent = []
        self.fail_on_recv = fail_on_recv
        self.fail_on_send = fail_on_send
        self.on_send = on_send

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        if self.fail_on_send:
            raise OSError("SECRET-CLOSE-REASON")
        self.sent.append(message)
        if self.on_send is not None:
            self.on_send()

    async def recv(self):
        try:
            return next(self.frames)
        except StopIteration:
            if self.fail_on_recv:
                raise OSError("synthetic connection drop")
            raise TimeoutError


class AKDAttributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = build_akd_request("GLRMK", date(2026, 9, 22), date(2026, 9, 22))

    def collector(self, connections, *, limiter=None, retries=0, observer=None,
                  diagnostic_observer=None):
        queue = iter(connections)

        class Provider:
            def get_init_data(self):
                return "synthetic-secret-not-logged"

        return WebSocketCollector(
            Provider(),
            connector=lambda *args, **kwargs: next(queue),
            config=CollectorConfig(receive_timeout_seconds=0.01, max_events=5),
            retry_policy=RetryPolicy(max_retries=retries, initial_delay_seconds=0),
            rate_limiter=limiter or RequestRateLimiter(
                minimum_interval_seconds=0, deduplication_window_seconds=0
            ),
            send_observer=observer,
            diagnostic_observer=diagnostic_observer,
        )

    async def test_single_actually_sent_request_gets_local_provenance(self):
        connection = FakeConnection((WELCOME, AKD_FRAME))
        observations = []
        results = await self.collector((connection,), observer=observations.append).collect((self.request,))
        self.assertEqual([(item.attempt, item.request_type, item.status) for item in observations],
                         [(1, "get_akd", "sent")])
        self.assertEqual(connection.sent, [self.request.to_json()])
        response = results[-1].normalized
        self.assertIsInstance(response, LiveAKDResponse)
        self.assertFalse(results[-1].normalization_pending)
        self.assertEqual(response.request_provenance.symbol, "GLRMK")
        self.assertEqual(response.request_provenance.start, date(2026, 9, 22))
        self.assertEqual(response.request_provenance.origin, "local_sent_request")

    async def test_missing_as_of_response_can_complete_with_sent_request_context(self):
        event = json.loads(AKD_FRAME)
        del event["data"]["data"]["as_of"]
        connection = FakeConnection((WELCOME, json.dumps(event)))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertFalse(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.as_of)
        self.assertEqual(results[-1].normalized.request_provenance.symbol, "GLRMK")

    async def test_unsolicited_response_stays_unattributed(self):
        connection = FakeConnection((AKD_FRAME,))
        observations = []
        results = await self.collector((connection,), observer=observations.append).collect((self.request,))
        self.assertEqual(connection.sent, [])
        self.assertEqual(observations[0].status, "not_sent_no_welcome")
        self.assertTrue(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.request_provenance)

    async def test_mutating_request_after_send_cannot_change_wire_provenance(self):
        connection = FakeConnection(
            (WELCOME, AKD_FRAME),
            on_send=lambda: self.request.data.update({"company": "ASELS"}),
        )
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(results[-1].normalized.request_provenance.symbol, "GLRMK")
        self.assertIn('"company":"GLRMK"', connection.sent[0])

    async def test_only_control_frames_do_not_claim_akd_success(self):
        connection = FakeConnection((WELCOME, '{"type":"hb"}'))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertTrue(connection.sent)
        self.assertFalse(any(result.event_type == "akd_data" for result in results))

    async def test_hb_without_welcome_reports_not_sent(self):
        connection = FakeConnection(('{"type":"hb"}',))
        observations = []
        results = await self.collector((connection,), observer=observations.append).collect((self.request,))
        self.assertEqual(connection.sent, [])
        self.assertEqual(observations[0].status, "not_sent_no_welcome")
        self.assertFalse(any(result.event_type == "akd_data" for result in results))

    async def test_observation_never_exposes_unknown_request_type(self):
        connection = FakeConnection((WELCOME,))
        observations = []
        request = CollectorRequest("secret-looking-request-type")
        await self.collector((connection,), observer=observations.append).collect((request,))
        self.assertEqual(observations[0].request_type, "[UNKNOWN_REQUEST_TYPE]")
        self.assertNotIn("secret-looking", repr(observations))

    async def test_two_sent_akd_requests_are_ambiguous(self):
        second = build_akd_request("ASELS", date(2026, 9, 21), date(2026, 9, 22))
        connection = FakeConnection((WELCOME, AKD_FRAME))
        results = await self.collector((connection,)).collect((self.request, second))
        self.assertEqual(len(connection.sent), 2)
        self.assertTrue(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.request_provenance)

    async def test_deduplicated_request_is_not_counted_as_sent(self):
        limiter = RequestRateLimiter(minimum_interval_seconds=0, deduplication_window_seconds=60)
        self.assertTrue(await limiter.acquire(self.request))
        connection = FakeConnection((WELCOME, AKD_FRAME))
        observations = []
        results = await self.collector((connection,), limiter=limiter, observer=observations.append).collect((self.request,))
        self.assertEqual(connection.sent, [])
        self.assertEqual(observations[0].status, "rate_limited")
        self.assertTrue(results[-1].normalization_pending)

    async def test_send_failure_is_not_reported_as_completed(self):
        connection = FakeConnection((WELCOME,), fail_on_send=True)
        observations = []
        with self.assertRaises(CollectorConnectionError) as captured:
            await self.collector((connection,), observer=observations.append).collect((self.request,))
        self.assertEqual([(item.attempt, item.status) for item in observations],
                         [(1, "send_failed")])
        self.assertNotIn("SECRET-CLOSE-REASON", str(captured.exception))

    async def test_retry_does_not_inherit_previous_send_context(self):
        limiter = RequestRateLimiter(minimum_interval_seconds=0, deduplication_window_seconds=60)
        first = FakeConnection((WELCOME,), fail_on_recv=True)
        second = FakeConnection((WELCOME, AKD_FRAME))
        observations = []
        failures = []
        results = await self.collector((first, second), limiter=limiter, retries=1,
                                       observer=observations.append,
                                       diagnostic_observer=failures.append).collect((self.request,))
        self.assertEqual(first.sent, [self.request.to_json()])
        self.assertEqual(second.sent, [])
        self.assertTrue(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.request_provenance)
        self.assertEqual([(item.attempt, item.status) for item in observations],
                         [(1, "sent"), (2, "rate_limited")])
        self.assertEqual([(item.attempt, item.stage, item.category) for item in failures],
                         [(1, "receive", "transport_error")])

    async def test_sent_malformed_akd_stops_on_first_parse_failure(self):
        malformed = json.loads(AKD_FRAME)
        malformed["data"]["data"]["results"][0]["total"]["volume"] = "PRIVATE-PAYLOAD"
        first = FakeConnection((WELCOME, json.dumps(malformed)))
        second = FakeConnection((WELCOME, AKD_FRAME))
        sends, failures = [], []
        collector = self.collector((first, second), retries=1,
                                   observer=sends.append, diagnostic_observer=failures.append)
        with self.assertRaises(CollectorConnectionError) as captured:
            await collector.collect((self.request,))
        self.assertEqual(first.sent, [self.request.to_json()])
        self.assertEqual(second.sent, [])
        self.assertEqual([(item.attempt, item.status) for item in sends], [(1, "sent")])
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0].attempt, failures[0].stage,
                          failures[0].category, failures[0].event_type,
                          failures[0].field_path),
                         (1, "parse", "field_type", "akd_data",
                          "event.data.data.results[0].total.volume"))
        self.assertNotIn("PRIVATE-PAYLOAD", str(captured.exception) + repr(failures))

    async def test_json_decode_error_is_not_retried_or_logged_raw(self):
        first = FakeConnection((WELCOME, '{"type":"akd_data","data":SECRET'))
        second = FakeConnection((WELCOME, AKD_FRAME))
        failures = []
        collector = self.collector((first, second), retries=1,
                                   diagnostic_observer=failures.append)
        with self.assertRaises(CollectorConnectionError) as captured:
            await collector.collect((self.request,))
        self.assertEqual(second.sent, [])
        self.assertEqual((failures[0].attempt, failures[0].stage,
                          failures[0].category, failures[0].event_type),
                         (1, "decode", "invalid_event_frame", None))
        self.assertNotIn("SECRET", str(captured.exception) + repr(failures))

    def test_untrusted_schema_path_is_not_exposed(self):
        self.assertIsNone(_safe_field_path(FieldTypeError(
            "event.data.results[0].SECRET: expected number"
        )))


if __name__ == "__main__":
    unittest.main()
