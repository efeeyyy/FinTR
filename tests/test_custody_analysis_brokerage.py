"""Synthetic, offline tests for the two custody-analysis label views."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import unittest

from fintr.data import (
    BrokerageResolutionStatus,
    LiveBrokerageIndex,
    resolve_live_custody_analysis_brokerages,
)
from fintr.data.models import SentCustodyAnalysisRequestProvenance
from fintr.data.parsers.brokerages_live import parse_live_brokerages_data
from fintr.data.parsers.custody_analysis_live import parse_live_custody_analysis


FIXTURE = Path(__file__).parent / "fixtures" / "market" / "custody_analysis_data_live.json"


def directory(*rows: tuple[str, str, str]) -> LiveBrokerageIndex:
    payload = [
        {"code": code, "title": title, "short_title": short_title}
        for code, title, short_title in rows
    ]
    response = parse_live_brokerages_data({
        "type": "brokerages_data", "data": {"data": json.dumps(payload)},
    })
    return LiveBrokerageIndex.from_response(response)


class CustodyAnalysisBrokerageAssociationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event = json.loads(FIXTURE.read_text(encoding="utf-8"), parse_float=Decimal)

    def test_both_series_resolve_without_merging_dates_numbers_or_metadata(self) -> None:
        before = deepcopy(self.event)
        parsed = parse_live_custody_analysis(self.event)
        provenance = SentCustodyAnalysisRequestProvenance(
            "AKFIS", date(2026, 9, 14), date(2026, 9, 23),
        )
        parsed = replace(parsed, request_provenance=provenance)
        index = directory(("SYN1", "Synthetic Broker", "Synthetic"))

        resolved = resolve_live_custody_analysis_brokerages(parsed, index)

        self.assertIs(resolved.source, parsed)
        self.assertIs(resolved.directory, index)
        self.assertIs(resolved.source.request_provenance, provenance)
        self.assertEqual([day.date for day in resolved.akd], [
            date(2026, 9, 22), date(2026, 9, 19),
        ])
        self.assertEqual([day.date for day in resolved.custody], [
            date(2026, 9, 22), date(2026, 9, 21),
        ])
        self.assertEqual(resolved.akd[1].rows, ())
        self.assertEqual(resolved.custody[1].rows, ())
        akd_row = resolved.akd[0].rows[0]
        custody_row = resolved.custody[0].rows[0]
        self.assertIs(akd_row.row, parsed.akd[0].rows[0])
        self.assertIs(custody_row.row, parsed.custody[0].rows[0])
        self.assertIs(akd_row.resolution.status, BrokerageResolutionStatus.RESOLVED)
        self.assertIs(custody_row.resolution.status, BrokerageResolutionStatus.RESOLVED)
        self.assertEqual(akd_row.resolution.label.title, "Synthetic Broker")
        self.assertEqual(custody_row.resolution.label.short_title, "Synthetic")
        self.assertEqual(akd_row.row.net_size, Decimal("-12.375"))
        self.assertEqual(custody_row.row.lot_count, Decimal("25.125"))
        self.assertEqual(resolved.source.data_range.actual_from, date(2026, 9, 19))
        self.assertEqual(resolved.source.metadata, parsed.metadata)
        self.assertEqual(self.event, before)
        self.assertNotIn("Synthetic Broker", repr(resolved))
        self.assertNotIn("SYN1", repr(resolved))

    def test_unknown_and_conflicting_codes_do_not_acquire_labels(self) -> None:
        parsed = parse_live_custody_analysis(self.event)
        for index, status in (
            (directory(), BrokerageResolutionStatus.UNRESOLVED),
            (
                directory(
                    ("SYN1", "First Synthetic", "First"),
                    ("SYN1", "Second Synthetic", "Second"),
                ),
                BrokerageResolutionStatus.AMBIGUOUS,
            ),
        ):
            with self.subTest(status=status):
                resolved = resolve_live_custody_analysis_brokerages(parsed, index)
                for item in (resolved.akd[0].rows[0], resolved.custody[0].rows[0]):
                    self.assertIs(item.resolution.status, status)
                    self.assertIsNone(item.resolution.label)

    def test_rows_resolve_independently_without_changing_series_order(self) -> None:
        second = deepcopy(self.event["data"]["data"]["custody"]["2026-09-22"][0])
        second["broker_code"] = "UNKNOWN"
        self.event["data"]["data"]["custody"]["2026-09-22"].append(second)
        parsed = parse_live_custody_analysis(self.event)
        resolved = resolve_live_custody_analysis_brokerages(
            parsed, directory(("SYN1", "Synthetic Broker", "Synthetic")),
        )

        self.assertEqual(
            [item.row.broker_code for item in resolved.custody[0].rows],
            ["SYN1", "UNKNOWN"],
        )
        self.assertEqual(
            [item.resolution.status for item in resolved.custody[0].rows],
            [BrokerageResolutionStatus.RESOLVED, BrokerageResolutionStatus.UNRESOLVED],
        )
        self.assertEqual(len(resolved.akd[0].rows), 1)

    def test_wrong_inputs_fail_without_payload_values(self) -> None:
        parsed = parse_live_custody_analysis(self.event)
        for response, index in (("PRIVATE", directory()), (parsed, "PRIVATE")):
            with self.subTest(response=type(response).__name__):
                with self.assertRaises(TypeError) as captured:
                    resolve_live_custody_analysis_brokerages(response, index)
                self.assertNotIn("PRIVATE", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
