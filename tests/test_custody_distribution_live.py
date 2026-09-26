"""Synthetic nested distribution contract; no provider or live socket."""

import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import unittest

from fintr.collector.assembler import MarketSnapshotAssembler
from fintr.collector.client import (
    CollectorConnectionError,
    RequestRateLimiter,
    WebSocketCollector,
    _safe_field_path,
    replay_frames,
)
from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.models import CollectorConfig, RetryPolicy
from fintr.collector.subscriptions import build_custody_distribution_request
from fintr.data.models import (
    CustodyDistributionSnapshot,
    LiveCustodyDistributionResponse,
)
from fintr.data.parsers.common import FieldTypeError, MissingFieldError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market"
FRAME = (FIXTURE_DIR / "custody_distribution_data_live.json").read_text(
    encoding="utf-8"
)
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


class LiveCustodyDistributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = json.loads(FRAME)
        self.request = build_custody_distribution_request("AKFIS")

    def collector(
        self, connections, *, limiter=None, retries=0, max_events=2,
        diagnostic_observer=None,
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
            diagnostic_observer=diagnostic_observer,
        )

    async def diagnostic_for(self, event):
        diagnostics = []
        frame = json.dumps(event)
        with self.assertRaises(CollectorConnectionError):
            await self.collector(
                (FakeConnection((WELCOME, frame)),),
                diagnostic_observer=diagnostics.append,
            ).collect((self.request,))
        self.assertEqual(len(diagnostics), 1)
        return diagnostics[0]

    def test_live_parser_preserves_nested_decimal_null_and_variable_slots(self):
        result = EventDispatcher().dispatch_frame(FRAME)
        self.assertIsInstance(result.normalized, LiveCustodyDistributionResponse)
        self.assertTrue(result.normalization_pending)
        response = result.normalized
        self.assertEqual(response.source, "pg")
        self.assertEqual(len(response.periods), 6)
        self.assertEqual(response.rows[0].values[1], Decimal("20.125"))
        self.assertEqual(
            response.rows[0].values[2], Decimal("30.000000000000000001")
        )
        self.assertEqual(
            response.rows[0].percentage, Decimal("0.123456789012345678901")
        )
        self.assertEqual(response.rows[1].values[1], Decimal("0"))
        self.assertIsNone(response.rows[1].values[5])
        self.assertIsNone(response.rows[1].percentage)
        self.assertFalse(hasattr(response, "symbol"))
        self.assertIsNone(response.request_provenance)
        self.assertNotIn("synthetic-distribution-request", repr(response))

        event = copy.deepcopy(self.event)
        event["data"]["data"]["periods"] = event["data"]["data"]["periods"][:3]
        for row in event["data"]["data"]["data"]:
            row["values"] = row["values"][:3]
        shorter = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertEqual(len(shorter.periods), 3)
        self.assertTrue(all(len(row.values) == 3 for row in shorter.rows))

    def test_values_length_must_match_periods(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["data"][0]["values"].pop()
        with self.assertRaises(FieldTypeError) as captured:
            EventDispatcher().dispatch_frame(json.dumps(event))
        self.assertEqual(captured.exception.reason, "period_values_length_mismatch")
        self.assertEqual(captured.exception.field_path,
                         "event.data.data.data[0].values")

    def test_nested_market_update_distribution_is_dispatched(self):
        wrapped = {
            "type": "market_update",
            "data": {
                "type": "custody_distribution_data",
                "data": copy.deepcopy(self.event["data"]),
            },
        }
        result = EventDispatcher().dispatch_frame(json.dumps(wrapped))
        self.assertTrue(result.known)
        self.assertEqual(result.wrapper_type, "market_update")
        self.assertIsInstance(result.normalized, LiveCustodyDistributionResponse)

    def _nested_payload_event(self, *, source_at_nested_level=False):
        event = copy.deepcopy(self.event)
        wrapper = event["data"]["data"]
        periods = wrapper.pop("periods")
        rows = wrapper.pop("data")
        nested = {"periods": periods, "data": rows}
        if source_at_nested_level:
            nested["source"] = wrapper.pop("source")
        wrapper["data"] = nested
        return event

    def test_documented_third_level_payload_parses_with_variable_length(self):
        for source_at_nested_level in (False, True):
            with self.subTest(source_at_nested_level=source_at_nested_level):
                event = self._nested_payload_event(
                    source_at_nested_level=source_at_nested_level
                )
                frame = json.dumps(event).replace(
                    "30.0", "30.000000000000000001", 1
                )
                result = EventDispatcher().dispatch_frame(frame)
                self.assertIsInstance(
                    result.normalized, LiveCustodyDistributionResponse
                )
                self.assertEqual(result.normalized.source, "pg")
                self.assertEqual(len(result.normalized.periods), 6)
                self.assertEqual(
                    result.normalized.rows[0].values[2],
                    Decimal("30.000000000000000001"),
                )
                self.assertIsNone(result.normalized.rows[1].values[5])

        malformed = self._nested_payload_event()
        del malformed["data"]["data"]["data"]["periods"]
        with self.assertRaises(MissingFieldError) as captured:
            EventDispatcher().dispatch_frame(json.dumps(malformed))
        self.assertEqual(
            captured.exception.field_path, "event.data.data.data.periods"
        )

    def test_conflicting_flat_and_third_level_payloads_fail_explicitly(self):
        event = copy.deepcopy(self.event)
        wrapper = event["data"]["data"]
        wrapper["data"] = {
            "periods": list(wrapper["periods"]),
            "data": copy.deepcopy(wrapper["data"]),
        }
        with self.assertRaises(FieldTypeError) as captured:
            EventDispatcher().dispatch_frame(json.dumps(event))
        self.assertEqual(
            captured.exception.reason, "ambiguous_distribution_payload"
        )

        duplicate_source = self._nested_payload_event(source_at_nested_level=True)
        duplicate_source["data"]["data"]["source"] = "pg"
        with self.assertRaises(FieldTypeError) as source_error:
            EventDispatcher().dispatch_frame(json.dumps(duplicate_source))
        self.assertEqual(source_error.exception.reason, "ambiguous_distribution_source")

    def test_invalid_value_types_and_non_finite_numbers_are_rejected(self):
        for invalid in ("2.5", True, [], {}, float("inf")):
            with self.subTest(invalid=invalid):
                event = copy.deepcopy(self.event)
                event["data"]["data"]["data"][0]["values"][0] = invalid
                with self.assertRaises(FieldTypeError):
                    EventDispatcher().dispatch_frame(json.dumps(event))
        event = copy.deepcopy(self.event)
        event["data"]["data"]["data"][0]["percentage"] = True
        with self.assertRaises(FieldTypeError):
            EventDispatcher().dispatch_frame(json.dumps(event))

    def test_missing_and_malformed_nested_live_fields_do_not_fall_back(self):
        paths = (
            ("data", "request_id"),
            ("data", "timestamp"),
            ("data", "processing_time_ms"),
            ("data", "data"),
            ("data", "data", "periods"),
            ("data", "data", "data"),
            ("data", "data", "source"),
            ("data", "data", "data", 0, "code"),
            ("data", "data", "data", 0, "values"),
            ("data", "data", "data", 0, "percentage"),
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

        malformed = copy.deepcopy(self.event)
        malformed["symbol"] = "SHOULD_NOT_FALL_BACK"
        malformed["data"].pop("data")
        malformed["data"]["periods"] = ["2026-09-22"]
        malformed["data"]["data"] = []
        with self.assertRaises(FieldTypeError) as captured:
            EventDispatcher().dispatch_frame(json.dumps(malformed))
        self.assertEqual(captured.exception.field_path, "event.data.data")

    def test_safe_distribution_parser_paths_are_allowlisted_and_index_masked(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["data"][0]["values"][0] = "SENSITIVE_VALUE"
        with self.assertRaises(FieldTypeError) as captured:
            EventDispatcher().dispatch_frame(json.dumps(event))
        self.assertEqual(
            _safe_field_path(captured.exception, "custody_distribution_data"),
            "event.data.data.data[index].values[index]",
        )
        self.assertIsNone(_safe_field_path(
            FieldTypeError("hidden", field_path="event.data.data.data.SECRET"),
            "custody_distribution_data",
        ))

    async def test_parse_diagnostic_distinguishes_missing_from_explicit_null(self):
        missing_event = copy.deepcopy(self.event)
        del missing_event["data"]["data"]["periods"]
        missing = await self.diagnostic_for(missing_event)

        null_event = copy.deepcopy(self.event)
        null_event["data"]["data"]["periods"] = None
        explicit_null = await self.diagnostic_for(null_event)

        self.assertEqual(missing.field_path, "event.data.data.periods")
        self.assertEqual(explicit_null.field_path, "event.data.data.periods")
        self.assertTrue(any("periods:missing" in item for item in missing.shape_summary))
        self.assertTrue(any("periods:null" in item for item in explicit_null.shape_summary))

    async def test_parse_diagnostic_summarizes_only_allowlisted_shape_not_values(self):
        event = copy.deepcopy(self.event)
        envelope = event["data"]
        payload = envelope["data"]
        envelope["request_id"] = "SECRET_REQUEST_ID"
        envelope["periods"] = ["2031-01-01"]
        envelope["results"] = [{"value": "SECRET_RESULT"}]
        payload["source"] = "SECRET_SOURCE_VALUE"
        rows = payload.pop("data")
        nested = {
            "periods": None,
            "data": rows,
            "results": [{"code": "SECRET_CODE"}],
            "api_token": "SECRET_TOKEN",
        }
        payload.pop("periods")
        payload["data"] = nested
        event["init_data"] = "SECRET_INIT_DATA"

        diagnostic = await self.diagnostic_for(event)
        summary = " | ".join(diagnostic.shape_summary)
        self.assertEqual(diagnostic.field_path, "event.data.data.data.periods")
        self.assertIn("event.data=object", summary)
        self.assertIn("event.data.data=object", summary)
        self.assertIn("event.data.data.data=object", summary)
        self.assertIn("periods:array", summary)
        self.assertIn("periods:missing", summary)
        self.assertIn("periods:null", summary)
        self.assertIn("results:array", summary)
        self.assertIn("source:string", summary)
        for secret in (
            "SECRET_REQUEST_ID", "SECRET_RESULT", "SECRET_CODE", "SECRET_TOKEN",
            "SECRET_SOURCE_VALUE", "SECRET_INIT_DATA", "api_token", "2031-01-01",
        ):
            self.assertNotIn(secret, summary)

    async def test_shape_summary_is_limited_to_distribution_parse_failures(self):
        other = json.loads(
            (FIXTURE_DIR / "custodian_data_live.json").read_text(encoding="utf-8")
        )
        other["data"]["data"]["results"][0]["value"] = "SECRET_BAD_NUMBER"
        diagnostics = []
        with self.assertRaises(CollectorConnectionError):
            await self.collector(
                (FakeConnection((WELCOME, json.dumps(other))),),
                diagnostic_observer=diagnostics.append,
            ).collect((self.request,))
        self.assertEqual(diagnostics[0].event_type, "custodian_data")
        self.assertEqual(diagnostics[0].shape_summary, ())

    async def test_collector_reports_only_safe_allowlisted_field_diagnostics(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["data"][0]["values"][0] = "SENSITIVE_VALUE"
        diagnostics = []
        first = FakeConnection((WELCOME, json.dumps(event)))
        second = FakeConnection((WELCOME, FRAME))
        with self.assertRaises(CollectorConnectionError):
            await self.collector(
                (first, second), retries=1,
                diagnostic_observer=diagnostics.append,
            ).collect((self.request,))
        self.assertEqual(second.sent, [])
        self.assertEqual(diagnostics[0].category, "field_type")
        self.assertEqual(diagnostics[0].event_type, "custody_distribution_data")
        self.assertEqual(
            diagnostics[0].field_path,
            "event.data.data.data[index].values[index]",
        )
        self.assertEqual((diagnostics[0].expected_kind, diagnostics[0].actual_kind),
                         ("number", "string"))
        self.assertNotIn("SENSITIVE_VALUE", repr(diagnostics))

    def test_legacy_flat_fixture_and_assembler_behavior_remain_unchanged(self):
        legacy_frame = (FIXTURE_DIR / "custody_distribution_data.json").read_text(
            encoding="utf-8"
        )
        result = EventDispatcher().dispatch_frame(legacy_frame)
        self.assertIsInstance(result.normalized, CustodyDistributionSnapshot)
        self.assertFalse(result.normalization_pending)
        self.assertEqual(len(result.normalized.rows[0].values), 5)
        assembler = MarketSnapshotAssembler()
        self.assertTrue(assembler.ingest(
            result.normalized, received_at=datetime(2026, 9, 23, tzinfo=timezone.utc)
        ))
        self.assertIs(assembler.get("GLRMK").custody_distribution, result.normalized)

    async def test_single_sent_request_binds_only_local_provenance(self):
        connection = FakeConnection((WELCOME, FRAME))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(connection.sent, [
            '{"type":"get_custody_distribution","data":{"code":"AKFIS"}}'
        ])
        result = results[-1]
        self.assertFalse(result.normalization_pending)
        self.assertEqual(result.normalized.request_provenance.symbol, "AKFIS")
        self.assertEqual(result.normalized.request_provenance.origin,
                         "local_sent_request")

    async def test_nested_payload_replay_stays_pending_but_sent_response_binds(self):
        event = self._nested_payload_event()
        frame = json.dumps(event)
        replayed = await replay_frames((frame,))
        self.assertTrue(replayed[0].normalization_pending)
        self.assertIsNone(replayed[0].normalized.request_provenance)

        connection = FakeConnection((WELCOME, frame))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertFalse(results[-1].normalization_pending)
        self.assertEqual(results[-1].normalized.request_provenance.symbol, "AKFIS")

    async def test_multiple_sent_requests_do_not_bind_ambiguous_response(self):
        second = build_custody_distribution_request("ASELS")
        connection = FakeConnection((WELCOME, FRAME))
        results = await self.collector((connection,)).collect((self.request, second))
        self.assertEqual(len(connection.sent), 2)
        self.assertTrue(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.request_provenance)

    async def test_no_welcome_means_no_send_and_no_provenance(self):
        connection = FakeConnection((FRAME,))
        results = await self.collector((connection,), max_events=1).collect((self.request,))
        self.assertEqual(connection.sent, [])
        self.assertTrue(results[0].normalization_pending)
        self.assertIsNone(results[0].normalized.request_provenance)

    async def test_rate_limit_skip_does_not_bind_response(self):
        limiter = RequestRateLimiter(
            minimum_interval_seconds=0, deduplication_window_seconds=60
        )
        self.assertTrue(await limiter.acquire(self.request))
        connection = FakeConnection((WELCOME, FRAME))
        results = await self.collector((connection,), limiter=limiter).collect(
            (self.request,)
        )
        self.assertEqual(connection.sent, [])
        self.assertTrue(results[-1].normalization_pending)
        self.assertIsNone(results[-1].normalized.request_provenance)

    async def test_sent_request_snapshot_survives_caller_mutation(self):
        connection = FakeConnection(
            (WELCOME, FRAME),
            on_send=lambda: self.request.data.update({"code": "ASELS"}),
        )
        results = await self.collector((connection,)).collect((self.request,))
        self.assertIn('"code":"AKFIS"', connection.sent[0])
        self.assertEqual(results[-1].normalized.request_provenance.symbol, "AKFIS")

    async def test_retry_does_not_retroactively_bind_pre_welcome_response(self):
        first = FakeConnection((WELCOME,), fail_after_frames=True)
        second = FakeConnection((FRAME, WELCOME))
        results = await self.collector((first, second), retries=1).collect(
            (self.request,)
        )
        response = next(
            result for result in results
            if result.event_type == "custody_distribution_data"
        )
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(len(second.sent), 1)
        self.assertTrue(response.normalization_pending)
        self.assertIsNone(response.normalized.request_provenance)

    async def test_replay_is_offline_and_leaves_live_context_pending(self):
        results = await replay_frames((FRAME,))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].normalization_pending)
        self.assertIsInstance(results[0].normalized, LiveCustodyDistributionResponse)


if __name__ == "__main__":
    unittest.main()
