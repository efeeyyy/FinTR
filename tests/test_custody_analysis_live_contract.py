"""Synthetic analysis contract; no provider, authentication, or live socket."""

import copy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import unittest

from fintr.collector.client import (
    CollectorConnectionError, RequestRateLimiter, WebSocketCollector,
    _safe_field_path, replay_frames,
)
from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.models import CollectorConfig, RetryPolicy
from fintr.collector.subscriptions import build_custody_analysis_request
from fintr.data.models import CustodyHistory, LiveCustodyAnalysisResponse
from fintr.data.parsers.common import FieldTypeError, MissingFieldError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market"
FRAME = (FIXTURE_DIR / "custody_analysis_data_live.json").read_text(encoding="utf-8")
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


class LiveCustodyAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = json.loads(FRAME)
        self.request = build_custody_analysis_request(
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

    def test_live_model_keeps_separate_sparse_series_and_null_metadata(self):
        result = EventDispatcher().dispatch_frame(FRAME)
        self.assertIsInstance(result.normalized, LiveCustodyAnalysisResponse)
        self.assertTrue(result.normalization_pending)
        response = result.normalized
        self.assertEqual(response.symbol, "AKFIS")
        self.assertEqual(response.dates, (
            date(2026, 9, 22), date(2026, 9, 21), date(2026, 9, 19)
        ))
        self.assertEqual(tuple(day.date for day in response.akd),
                         (date(2026, 9, 22), date(2026, 9, 19)))
        self.assertEqual(tuple(day.date for day in response.custody),
                         (date(2026, 9, 22), date(2026, 9, 21)))
        self.assertEqual(response.akd[1].rows, ())
        self.assertEqual(response.custody[1].rows, ())
        self.assertEqual(response.akd[0].rows[0].net_size, Decimal("-12.375"))
        self.assertEqual(response.akd[0].rows[0].net_percentage,
                         Decimal("22.840000000000001"))
        self.assertEqual(response.custody[0].rows[0].lot_count, Decimal("25.125"))
        self.assertEqual(response.custody[0].rows[0].percentage,
                         Decimal("3.820964628350891"))
        self.assertEqual(response.metadata[0].null_fields,
                         ("retail_pct", "institutional_pct", "investor_count"))
        self.assertEqual(response.metadata[1].null_fields, ("retail_pct",))
        self.assertEqual(response.data_range.actual_from, date(2026, 9, 19))
        self.assertFalse(hasattr(response.data_range, "actual_to"))
        self.assertFalse(hasattr(response.custody[0].rows[0], "daily_net"))
        self.assertFalse(hasattr(response.custody[0].rows[0], "cost"))
        self.assertNotIn("synthetic-analysis-request", repr(result))

    def test_unknown_metadata_keys_are_named_but_not_interpreted(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["metadata"]["2026-09-22"]["other_metric"] = {"value": 7}
        response = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertEqual(response.metadata[0].uninterpreted_keys, ("other_metric",))

    def test_retail_metadata_number_null_and_missing_remain_distinct(self):
        event = copy.deepcopy(self.event)
        entry = event["data"]["data"]["metadata"]["2026-09-22"]
        del entry["retail_pct"]
        missing = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertIsNone(missing.metadata[0].retail_pct)
        self.assertNotIn("retail_pct", missing.metadata[0].null_fields)

        entry["retail_pct"] = None
        null = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertIsNone(null.metadata[0].retail_pct)
        self.assertIn("retail_pct", null.metadata[0].null_fields)

        entry["retail_pct"] = 0
        zero = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertEqual(zero.metadata[0].retail_pct, Decimal("0"))
        self.assertNotIn("retail_pct", zero.metadata[0].null_fields)

    def test_retail_metadata_decimal_precision_and_invalid_types(self):
        precise_frame = FRAME.replace(
            '"retail_pct": null',
            '"retail_pct": 12.345678901234567890123456789',
            1,
        )
        precise = EventDispatcher().dispatch_frame(precise_frame).normalized
        self.assertEqual(precise.metadata[0].retail_pct,
                         Decimal("12.345678901234567890123456789"))
        for value in ("12.5", True, [], {}, float("inf")):
            with self.subTest(value=value):
                event = copy.deepcopy(self.event)
                event["data"]["data"]["metadata"]["2026-09-22"]["retail_pct"] = value
                with self.assertRaises(FieldTypeError):
                    EventDispatcher().dispatch_frame(json.dumps(event))

    def test_institutional_metadata_number_null_and_missing_remain_distinct(self):
        event = copy.deepcopy(self.event)
        entry = event["data"]["data"]["metadata"]["2026-09-22"]
        del entry["institutional_pct"]
        missing = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertIsNone(missing.metadata[0].institutional_pct)
        self.assertNotIn("institutional_pct", missing.metadata[0].null_fields)

        entry["institutional_pct"] = None
        null = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertIsNone(null.metadata[0].institutional_pct)
        self.assertIn("institutional_pct", null.metadata[0].null_fields)

        entry["institutional_pct"] = 0
        zero = EventDispatcher().dispatch_frame(json.dumps(event)).normalized
        self.assertEqual(zero.metadata[0].institutional_pct, Decimal("0"))
        self.assertNotIn("institutional_pct", zero.metadata[0].null_fields)

    def test_institutional_metadata_decimal_precision_and_invalid_types(self):
        precise_frame = FRAME.replace(
            '"institutional_pct": null',
            '"institutional_pct": 98.765432109876543210987654321',
            1,
        )
        precise = EventDispatcher().dispatch_frame(precise_frame).normalized
        self.assertEqual(precise.metadata[0].institutional_pct,
                         Decimal("98.765432109876543210987654321"))
        for value in ("12.5", True, [], {}, float("inf")):
            with self.subTest(value=value):
                event = copy.deepcopy(self.event)
                event["data"]["data"]["metadata"]["2026-09-22"][
                    "institutional_pct"
                ] = value
                with self.assertRaises(FieldTypeError):
                    EventDispatcher().dispatch_frame(json.dumps(event))

    def test_investor_count_non_null_remains_unsupported(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["metadata"]["2026-09-22"]["investor_count"] = 7
        with self.assertRaises(FieldTypeError) as captured:
            EventDispatcher().dispatch_frame(json.dumps(event))
        self.assertIn("non-null metadata type unverified", str(captured.exception))

    def test_untrusted_structured_path_is_rejected(self):
        error = FieldTypeError(
            "SENSITIVE-PAYLOAD",
            field_path="event.data.data.akd.2026-09-22[0].net_cost.SECRET",
            expected_kind="number", actual_kind="string",
        )
        self.assertIsNone(_safe_field_path(error, "custody_analysis_data"))

    async def test_legacy_history_fixture_remains_distinct(self):
        legacy = (FIXTURE_DIR / "custody_analysis_data.json").read_text(encoding="utf-8")
        result = (await replay_frames((legacy,)))[0]
        self.assertIsInstance(result.normalized, CustodyHistory)
        self.assertFalse(result.normalization_pending)

    def test_malformed_live_envelope_never_falls_back_to_legacy(self):
        event = copy.deepcopy(self.event)
        event["data"].pop("data")
        event["data"].update({"symbol": "AKFIS", "dates": [], "custody": {}})
        with self.assertRaises(MissingFieldError):
            EventDispatcher().dispatch_frame(json.dumps(event))

    def test_missing_null_and_wrong_types_are_rejected(self):
        paths = (
            ("data", "request_id"), ("data", "timestamp"),
            ("data", "processing_time_ms"), ("data", "data", "symbol"),
            ("data", "data", "dates"), ("data", "data", "akd"),
            ("data", "data", "custody"), ("data", "data", "data_range"),
            ("data", "data", "metadata"),
            ("data", "data", "data_range", "requested_from"),
            ("data", "data", "data_range", "requested_to"),
            ("data", "data", "data_range", "actual_from"),
            ("data", "data", "akd", "2026-09-22", 0, "net_cost"),
            ("data", "data", "custody", "2026-09-22", 0, "lot_count"),
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
        for path, value in (
            (("data", "data", "dates"), "2026-09-22"),
            (("data", "data", "custody", "2026-09-22", 0, "percentage"), "3.8"),
            (("data", "data", "akd", "2026-09-22", 0, "total_cost"), True),
        ):
            with self.subTest(path=path):
                event = copy.deepcopy(self.event)
                parent = event
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = value
                with self.assertRaises(FieldTypeError):
                    EventDispatcher().dispatch_frame(json.dumps(event))

    async def test_single_sent_request_binds_matching_symbol(self):
        connection = FakeConnection((WELCOME, FRAME))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(connection.sent, [self.request.to_json()])
        self.assertFalse(results[-1].normalization_pending)
        response = results[-1].normalized
        self.assertEqual(response.request_provenance.symbol, "AKFIS")
        self.assertEqual(response.request_provenance.requested_from, date(2026, 9, 14))
        self.assertEqual(response.request_provenance.requested_to, date(2026, 9, 23))
        self.assertEqual(response.data_range.actual_from, date(2026, 9, 19))

    async def test_symbol_mismatch_is_safe_failure_without_retry(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["symbol"] = "OTHER"
        first = FakeConnection((WELCOME, json.dumps(event)))
        second = FakeConnection((WELCOME, FRAME))
        diagnostics = []
        with self.assertRaises(CollectorConnectionError) as captured:
            await self.collector(
                (first, second), retries=1, diagnostic_observer=diagnostics.append
            ).collect((self.request,))
        self.assertEqual(second.sent, [])
        self.assertEqual((diagnostics[0].stage, diagnostics[0].category),
                         ("parse", "symbol_mismatch"))
        self.assertNotIn("OTHER", str(captured.exception))

    async def test_multiple_sent_requests_leave_response_unbound(self):
        second = build_custody_analysis_request(
            "ASELS", date(2026, 9, 14), date(2026, 9, 23)
        )
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
            (WELCOME, FRAME), on_send=lambda: self.request.data.update({"symbol": "ASELS"})
        )
        results = await self.collector((connection,)).collect((self.request,))
        self.assertEqual(results[-1].normalized.request_provenance.symbol, "AKFIS")
        self.assertIn('"symbol":"AKFIS"', connection.sent[0])

    async def test_retry_and_dedup_do_not_reuse_prior_connection(self):
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

    async def test_wrong_type_has_masked_date_and_safe_expected_actual_kinds(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["akd"]["2026-09-22"][0]["net_cost"] = "SECRET-VALUE"
        first = FakeConnection((WELCOME, json.dumps(event)))
        second = FakeConnection((WELCOME, FRAME))
        diagnostics = []
        with self.assertRaises(CollectorConnectionError) as captured:
            await self.collector(
                (first, second), retries=1, diagnostic_observer=diagnostics.append,
            ).collect((self.request,))
        self.assertEqual(second.sent, [])
        self.assertEqual(diagnostics[0].event_type, "custody_analysis_data")
        self.assertEqual(diagnostics[0].category, "field_type")
        self.assertEqual(diagnostics[0].field_path,
                         "event.data.data.akd.[date][index].net_cost")
        self.assertEqual((diagnostics[0].expected_kind, diagnostics[0].actual_kind),
                         ("number", "string"))
        self.assertNotIn("SECRET-VALUE", str(captured.exception) + repr(diagnostics))
        self.assertNotIn("2026-09-22", repr(diagnostics))

    async def test_unverified_investor_count_has_distinct_safe_diagnostic(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["metadata"]["2026-09-22"]["investor_count"] = 12.5
        diagnostics = []
        with self.assertRaises(CollectorConnectionError):
            await self.collector(
                (FakeConnection((WELCOME, json.dumps(event))),),
                diagnostic_observer=diagnostics.append,
            ).collect((self.request,))
        self.assertEqual(diagnostics[0].category, "unsupported_metadata")
        self.assertEqual(diagnostics[0].field_path,
                         "event.data.data.metadata.[date].investor_count")
        self.assertEqual((diagnostics[0].expected_kind, diagnostics[0].actual_kind),
                         ("null", "number"))
        self.assertNotIn("12.5", repr(diagnostics))

    async def test_sent_response_with_both_percentage_numbers_completes_successfully(self):
        frame = FRAME.replace('"retail_pct": null',
                              '"retail_pct": 1.234567890123456789', 1)
        frame = frame.replace('"institutional_pct": null',
                              '"institutional_pct": 98.76543210987654321', 1)
        connection = FakeConnection((WELCOME, frame))
        results = await self.collector((connection,)).collect((self.request,))
        self.assertFalse(results[-1].normalization_pending)
        self.assertEqual(results[-1].normalized.request_provenance.symbol, "AKFIS")
        self.assertEqual(results[-1].normalized.metadata[0].retail_pct,
                         Decimal("1.234567890123456789"))
        self.assertEqual(results[-1].normalized.metadata[0].institutional_pct,
                         Decimal("98.76543210987654321"))

    async def test_malicious_date_key_is_never_echoed(self):
        event = copy.deepcopy(self.event)
        event["data"]["data"]["akd"]["SECRET.date"] = (
            event["data"]["data"]["akd"].pop("2026-09-22")
        )
        diagnostics = []
        with self.assertRaises(CollectorConnectionError) as captured:
            await self.collector(
                (FakeConnection((WELCOME, json.dumps(event))),),
                diagnostic_observer=diagnostics.append,
            ).collect((self.request,))
        self.assertEqual(diagnostics[0].field_path, "event.data.data.akd.[date]")
        self.assertEqual((diagnostics[0].expected_kind, diagnostics[0].actual_kind),
                         ("date", "string"))
        self.assertNotIn("SECRET", str(captured.exception) + repr(diagnostics))


if __name__ == "__main__":
    unittest.main()
