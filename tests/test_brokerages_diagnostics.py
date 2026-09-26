"""Privacy tests for allowlisted brokerage parse-shape diagnostics."""

import json
import unittest

from fintr.collector.client import (
    CollectorConnectionError,
    RequestRateLimiter,
    WebSocketCollector,
    _brokerages_shape_summary,
)
from fintr.collector.models import CollectorConfig, RetryPolicy
from fintr.collector.subscriptions import build_brokerages_request


class FakeConnection:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        try:
            return next(self.frames)
        except StopIteration:
            raise TimeoutError


def _collector(connection, diagnostics):
    class Provider:
        def get_init_data(self):
            return "SYNTHETIC_SECRET"

    return WebSocketCollector(
        Provider(),
        connector=lambda *args, **kwargs: connection,
        config=CollectorConfig(max_events=2, receive_timeout_seconds=0.01),
        retry_policy=RetryPolicy(max_retries=0),
        rate_limiter=RequestRateLimiter(
            minimum_interval_seconds=0, deduplication_window_seconds=0,
        ),
        diagnostic_observer=diagnostics.append,
    )


class BrokerageDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def test_shape_summary_reports_only_allowlisted_container_kinds(self):
        summary = _brokerages_shape_summary({
            "data": {
                "data": '[{"code":"PRIVATE_CODE"}]',
                "results": None,
                "date": "PRIVATE_DATE",
                "init_data": "PRIVATE_INIT_DATA",
            },
        })
        self.assertEqual(summary, (
            "event.data=object{data:string,results:null,date:string}",
            "event.data.data=string",
        ))
        rendered = " | ".join(summary)
        for private in ("PRIVATE_CODE", "PRIVATE_DATE", "PRIVATE_INIT_DATA"):
            self.assertNotIn(private, rendered)

    async def test_parse_failure_reports_safe_path_and_embedded_array_shape(self):
        event = {
            "type": "brokerages_data",
            "data": {
                "data": json.dumps([{
                    "code": "PRIVATE_BROKER_CODE",
                    "short_title": "PRIVATE_SHORT_NAME",
                    "private": "PRIVATE_ROW_VALUE",
                }]),
                "private_key": "PRIVATE_OUTER_VALUE",
            },
        }
        connection = FakeConnection((
            '{"type":"welcome"}', json.dumps(event),
        ))
        diagnostics = []
        collector = _collector(connection, diagnostics)

        with self.assertRaises(CollectorConnectionError) as captured:
            await collector.collect((build_brokerages_request(),))

        self.assertEqual(json.loads(connection.sent[0]), {"type": "get_brokerages"})
        self.assertEqual(len(diagnostics), 1)
        diagnostic = diagnostics[0]
        self.assertEqual(diagnostic.event_type, "brokerages_data")
        self.assertEqual(diagnostic.category, "missing_field")
        self.assertEqual(diagnostic.field_path, "event.data.data_json[index].title")
        self.assertEqual(diagnostic.expected_kind, "present")
        self.assertEqual(diagnostic.actual_kind, "missing_or_null")
        self.assertEqual(diagnostic.shape_summary, (
            "event.data=object{data:string,results:missing,date:missing}",
            "event.data.data=string",
            "event.data.data_json=array",
        ))
        output = repr(diagnostic) + str(captured.exception)
        for private in (
            "PRIVATE_BROKER_CODE", "PRIVATE_SHORT_NAME", "PRIVATE_ROW_VALUE",
            "PRIVATE_OUTER_VALUE", "SYNTHETIC_SECRET",
        ):
            self.assertNotIn(private, output)

    async def test_non_brokerages_parse_error_has_no_brokerage_shape_summary(self):
        connection = FakeConnection((
            '{"type":"welcome"}', '{"type":"depth_update","data":null}',
        ))
        diagnostics = []
        collector = _collector(connection, diagnostics)

        with self.assertRaises(CollectorConnectionError):
            await collector.collect((build_brokerages_request(),))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].event_type, "depth_update")
        self.assertEqual(diagnostics[0].shape_summary, ())


if __name__ == "__main__":
    unittest.main()
