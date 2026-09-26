"""Synthetic tests of the observed AKD subset, not a complete live-schema fixture."""

import copy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import unittest

from fintr.collector.client import replay_frames
from fintr.collector.dispatcher import EventDispatcher
from fintr.collector.subscriptions import build_akd_request
from fintr.data.parsers.akd import validate_akd_data_envelope
from fintr.data.parsers.common import FieldTypeError, MissingFieldError
from fintr.data.models import AKDSnapshot, LiveAKDResponse


FIXTURE = Path(__file__).parent / "fixtures" / "market" / "akd_data_partial.json"


class LiveAKDContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_request_matches_user_observed_wire_format(self):
        request = build_akd_request("GLRMK", date(2026, 9, 22), date(2026, 9, 22))
        self.assertEqual(request.to_json(), (
            '{"type":"get_akd","data":{"company":"GLRMK",'
            '"start":"2026-09-22","end":"2026-09-22"}}'
        ))

    def test_recognized_envelope_is_typed_but_not_a_legacy_snapshot(self):
        before = copy.deepcopy(self.event)
        result = EventDispatcher().dispatch_frame(json.dumps(self.event))
        self.assertTrue(result.known)
        self.assertTrue(result.normalization_pending)
        self.assertIsInstance(result.normalized, LiveAKDResponse)
        self.assertNotIsInstance(result.normalized, AKDSnapshot)
        self.assertEqual(result.normalized.results[0].total.volume, Decimal("2700.5"))
        self.assertIsNone(result.normalized.request_provenance)
        self.assertEqual(result.event_type, "akd_data")
        self.assertEqual(self.event, before)

    def test_empty_results_still_cannot_supply_symbol_or_period(self):
        self.event["data"]["data"]["results"] = []
        result = EventDispatcher().dispatch_frame(json.dumps(self.event))
        self.assertTrue(result.normalization_pending)
        self.assertEqual(result.normalized.results, ())
        self.assertIsNone(result.normalized.request_provenance)

    def test_unknown_fields_are_not_retained_and_request_id_is_hidden_in_repr(self):
        marker = "SYNTHETIC_DO_NOT_RETAIN"
        self.event["data"]["request_id"] = marker
        self.event["data"]["data"]["unknown"] = {"opaque": marker}
        with self.assertNoLogs("fintr.collector.dispatcher", level="DEBUG"):
            result = EventDispatcher().dispatch_frame(json.dumps(self.event))
        self.assertNotIn(marker, repr(result))
        self.assertEqual(result.normalized.request_id, marker)

    def test_missing_observed_fields_are_explicit(self):
        paths = (
            ("data", "request_id"), ("data", "timestamp"),
            ("data", "processing_time_ms"), ("data", "data", "results"),
            ("data", "data", "results", 0, "brokerage"),
            ("data", "data", "results", 0, "net", "cost"),
            ("data", "data", "results", 0, "total", "volume"),
            ("data", "data", "source"),
        )
        for path in paths:
            with self.subTest(path=path):
                event = copy.deepcopy(self.event)
                parent = event
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with self.assertRaises(MissingFieldError):
                    validate_akd_data_envelope(event)

    def test_as_of_is_optional_but_non_null_values_remain_validated(self):
        for mode, value in (("missing", None), ("null", None)):
            with self.subTest(mode=mode):
                event = copy.deepcopy(self.event)
                if mode == "missing":
                    del event["data"]["data"]["as_of"]
                else:
                    event["data"]["data"]["as_of"] = value
                result = EventDispatcher().dispatch_frame(json.dumps(event))
                self.assertIsNone(result.normalized.as_of)
                self.assertTrue(result.normalization_pending)

        event = copy.deepcopy(self.event)
        event["data"]["data"]["as_of"] = "12:34:56"
        self.assertEqual(
            EventDispatcher().dispatch_frame(json.dumps(event)).normalized.as_of,
            "12:34:56",
        )
        for value, error in (([], FieldTypeError), ("", MissingFieldError)):
            with self.subTest(value=value):
                event = copy.deepcopy(self.event)
                event["data"]["data"]["as_of"] = value
                with self.assertRaises(error):
                    validate_akd_data_envelope(event)

    def test_net_types_are_checked_without_coercion_or_payload_errors(self):
        for key, bad in (("size", True), ("size", "120"), ("percentage", "PRIVATE"),
                         ("cost", float("inf"))):
            with self.subTest(key=key, bad=bad):
                event = copy.deepcopy(self.event)
                event["data"]["data"]["results"][0]["net"][key] = bad
                with self.assertRaises(FieldTypeError) as captured:
                    validate_akd_data_envelope(event)
                self.assertNotIn("PRIVATE", str(captured.exception))

    def test_timestamp_and_nested_payload_types_are_checked(self):
        for key, value in (("timestamp", "2026-09-22"), ("processing_time_ms", True),
                           ("data", []), ("request_id", 1)):
            with self.subTest(key=key):
                event = copy.deepcopy(self.event)
                event["data"][key] = value
                with self.assertRaises(FieldTypeError):
                    validate_akd_data_envelope(event)

    async def test_replay_keeps_partial_event_and_continues(self):
        frames = (
            '{"type":"welcome"}', json.dumps(self.event),
            (FIXTURE.parent / "depth_update.json").read_text(encoding="utf-8"),
        )
        results = await replay_frames(frames)
        self.assertFalse(results[0].known)
        self.assertTrue(results[1].normalization_pending)
        self.assertIsInstance(results[1].normalized, LiveAKDResponse)
        self.assertIsNotNone(results[2].normalized)
        self.assertFalse(results[2].normalization_pending)

    def test_nested_market_wrapper_preserves_akd_envelope(self):
        result = EventDispatcher().dispatch_frame(json.dumps({
            "type": "market_update", "data": self.event,
        }))
        self.assertEqual(result.wrapper_type, "market_update")
        self.assertTrue(result.normalization_pending)
        self.assertIsInstance(result.normalized, LiveAKDResponse)

    def test_decimal_token_precision_is_preserved(self):
        frame = json.dumps(self.event).replace('"percentage": 0.25', '"percentage": 0.12345678901234567890123456789')
        result = EventDispatcher().dispatch_frame(frame)
        self.assertEqual(result.normalized.results[0].net.percentage,
                         Decimal("0.12345678901234567890123456789"))


if __name__ == "__main__":
    unittest.main()
