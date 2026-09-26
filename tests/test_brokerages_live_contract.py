"""Offline tests for the JS-derived, candidate brokerage response contract."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fintr.collector.client import _safe_field_path
from fintr.collector.dispatcher import EventDispatcher
from fintr.data.models import BrokerageDirectory, LiveBrokeragesResponse
from fintr.data.parsers.brokerages_live import (
    MAX_EMBEDDED_JSON_CHARS,
    parse_live_brokerages_data,
)
from fintr.data.parsers.common import FieldTypeError, MissingFieldError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market"
LIVE_EVENT = json.loads(
    (FIXTURE_DIR / "brokerages_data_live.json").read_text(encoding="utf-8")
)
LEGACY_EVENT = json.loads(
    (FIXTURE_DIR / "brokerages_data.json").read_text(encoding="utf-8")
)


def live_event_for(text: str) -> dict:
    event = copy.deepcopy(LIVE_EVENT)
    event["data"]["data"] = text
    return event


class LiveBrokeragesContractTests(unittest.TestCase):
    def test_synthetic_live_fixture_parses_candidate_rows_without_repr_leak(self):
        frame = (FIXTURE_DIR / "brokerages_data_live.json").read_text(encoding="utf-8")
        result = EventDispatcher().dispatch_frame(frame)
        self.assertIsInstance(result.normalized, LiveBrokeragesResponse)
        self.assertFalse(result.normalization_pending)
        row = result.normalized.rows[0]
        self.assertEqual(row.code, "SYNTH-CODE-001")
        self.assertEqual(row.title, "Synthetic Candidate Title")
        self.assertEqual(row.short_title, "Synthetic")
        rendered = repr(result.normalized)
        for value in (row.code, row.title, row.short_title):
            self.assertNotIn(value, rendered)

    def test_embedded_json_is_decoded_once_including_diagnostic_path(self):
        raw = json.dumps([{"code": "SYNTH", "title": "Synthetic", "short_title": "Syn"}])
        with patch(
            "fintr.data.parsers.brokerages_live.json.loads", wraps=json.loads,
        ) as load_json:
            parsed = parse_live_brokerages_data(live_event_for(raw))
        self.assertEqual(len(parsed.rows), 1)
        self.assertEqual(load_json.call_count, 1)

    def test_legacy_fixture_still_dispatches_to_legacy_model(self):
        result = EventDispatcher().dispatch_frame(json.dumps(LEGACY_EVENT))
        self.assertIsInstance(result.normalized, BrokerageDirectory)
        self.assertEqual(result.normalized.as_of.isoformat(), "2026-09-22")

    def test_invalid_embedded_json_is_redacted(self):
        secret_text = '{"results":["PRIVATE_ROW"], PRIVATE_INVALID'
        with self.assertRaises(FieldTypeError) as captured:
            parse_live_brokerages_data(live_event_for(secret_text))
        error = captured.exception
        self.assertEqual(error.field_path, "event.data.data_json")
        self.assertEqual(error.expected_kind, "array")
        self.assertEqual(error.actual_kind, "string")
        self.assertEqual(error.reason, "invalid_json")
        self.assertIn("event.data.data_json=invalid_json", error.shape_summary)
        self.assertNotIn("PRIVATE", str(error))
        self.assertNotIn("PRIVATE", " ".join(error.shape_summary))

    def test_json_top_level_must_be_an_array_without_reporting_object_contents(self):
        with self.assertRaises(FieldTypeError) as captured:
            parse_live_brokerages_data(live_event_for(
                '{"results":[],"date":"PRIVATE_DATE","secret":"PRIVATE_KEY"}'
            ))
        error = captured.exception
        self.assertEqual(error.field_path, "event.data.data_json")
        self.assertEqual(error.expected_kind, "array")
        self.assertEqual(error.actual_kind, "object")
        self.assertIn(
            "event.data.data_json=object{data:missing,results:array,date:string}",
            error.shape_summary,
        )
        for private in ("PRIVATE_DATE", "PRIVATE_KEY", "secret"):
            self.assertNotIn(private, str(error) + " ".join(error.shape_summary))

    def test_array_rows_must_be_objects(self):
        with self.assertRaises(FieldTypeError) as captured:
            parse_live_brokerages_data(live_event_for('["PRIVATE_ROW"]'))
        error = captured.exception
        self.assertEqual(error.field_path, "event.data.data_json[0]")
        self.assertEqual(error.expected_kind, "object")
        self.assertEqual(error.actual_kind, "string")
        self.assertEqual(
            _safe_field_path(error, "brokerages_data"),
            "event.data.data_json[index]",
        )
        self.assertNotIn("PRIVATE_ROW", str(error))

    def test_each_candidate_text_field_is_required_and_type_checked(self):
        for missing_field in ("code", "title", "short_title"):
            row = {
                "code": "PRIVATE_CODE",
                "title": "PRIVATE_TITLE",
                "short_title": "PRIVATE_SHORT",
            }
            del row[missing_field]
            raw = json.dumps([row])
            with self.subTest(missing=missing_field):
                with self.assertRaises(MissingFieldError) as captured:
                    parse_live_brokerages_data(live_event_for(raw))
                error = captured.exception
                self.assertEqual(
                    error.field_path,
                    f"event.data.data_json[0].{missing_field}",
                )
                self.assertEqual(error.expected_kind, "present")
                self.assertEqual(error.actual_kind, "missing_or_null")
                self.assertEqual(
                    _safe_field_path(error, "brokerages_data"),
                    f"event.data.data_json[index].{missing_field}",
                )
                self.assertNotIn("PRIVATE", str(error))

        for field, value, actual_kind in (
            ("code", 12, "number"),
            ("title", True, "boolean"),
            ("short_title", None, "null"),
        ):
            row = {"code": "SYNTH", "title": "Synthetic", "short_title": "Syn"}
            row[field] = value
            with self.subTest(field=field, actual=actual_kind):
                with self.assertRaises(FieldTypeError) as captured:
                    parse_live_brokerages_data(live_event_for(json.dumps([row])))
                error = captured.exception
                self.assertEqual(error.expected_kind, "string")
                self.assertEqual(error.actual_kind, actual_kind)
                self.assertEqual(
                    _safe_field_path(error, "brokerages_data"),
                    f"event.data.data_json[index].{field}",
                )

    def test_embedded_text_size_is_bounded(self):
        with self.assertRaises(FieldTypeError) as captured:
            parse_live_brokerages_data(live_event_for(
                "X" * (MAX_EMBEDDED_JSON_CHARS + 1)
            ))
        error = captured.exception
        self.assertEqual(error.reason, "oversize")
        self.assertIn("event.data.data_json=oversize", error.shape_summary)
        self.assertNotIn("X" * 32, str(error) + " ".join(error.shape_summary))

    def test_row_diagnostic_summary_never_contains_row_values(self):
        with self.assertRaises(MissingFieldError) as captured:
            parse_live_brokerages_data(live_event_for(
                '[{"code":"PRIVATE_CODE","title":"PRIVATE_TITLE"}]'
            ))
        error = captured.exception
        self.assertIn("event.data.data_json=array", error.shape_summary)
        for private in ("PRIVATE_CODE", "PRIVATE_TITLE"):
            self.assertNotIn(private, " ".join(error.shape_summary) + str(error))


if __name__ == "__main__":
    unittest.main()
