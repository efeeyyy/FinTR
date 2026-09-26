"""Synthetic, offline tests of the live AKD-to-brokerage association."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest

from fintr.data import (
    BrokerageResolutionStatus,
    LiveBrokerageIndex,
    resolve_live_akd_brokerages,
)
from fintr.data.parsers.akd import parse_live_akd_data
from fintr.data.parsers.brokerages_live import parse_live_brokerages_data


AKD_FIXTURE = Path(__file__).parent / "fixtures" / "market" / "akd_data_partial.json"


def directory(*rows: tuple[str, str, str]) -> LiveBrokerageIndex:
    payload = [
        {"code": code, "title": title, "short_title": short_title}
        for code, title, short_title in rows
    ]
    response = parse_live_brokerages_data({
        "type": "brokerages_data", "data": {"data": json.dumps(payload)},
    })
    return LiveBrokerageIndex.from_response(response)


class AKDBrokerageAssociationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event = json.loads(AKD_FIXTURE.read_text(encoding="utf-8"))

    def test_resolved_label_preserves_akd_numbers_and_provenance(self) -> None:
        before = deepcopy(self.event)
        akd = parse_live_akd_data(self.event)
        index = directory(("TEST", "Synthetic Broker", "Synthetic"))
        resolved = resolve_live_akd_brokerages(
            akd, index,
        )

        self.assertIs(resolved.source, akd)
        self.assertIs(resolved.directory, index)
        self.assertIs(resolved.rows[0].row, akd.results[0])
        self.assertIs(
            resolved.rows[0].resolution.status,
            BrokerageResolutionStatus.RESOLVED,
        )
        self.assertEqual(resolved.rows[0].resolution.label.title, "Synthetic Broker")
        self.assertEqual(resolved.rows[0].row.net.size, 120)
        self.assertEqual(resolved.rows[0].row.total.volume, Decimal("2700.5"))
        self.assertIsNone(resolved.source.request_provenance)
        self.assertEqual(self.event, before)
        self.assertNotIn("Synthetic Broker", repr(resolved))
        self.assertNotIn("TEST", repr(resolved))

    def test_missing_and_conflicting_labels_never_pick_a_name(self) -> None:
        akd = parse_live_akd_data(self.event)
        for index, status in (
            (directory(), BrokerageResolutionStatus.UNRESOLVED),
            (
                directory(
                    ("TEST", "First Synthetic", "First"),
                    ("TEST", "Second Synthetic", "Second"),
                ),
                BrokerageResolutionStatus.AMBIGUOUS,
            ),
        ):
            with self.subTest(status=status):
                resolved = resolve_live_akd_brokerages(akd, index)
                self.assertIs(resolved.rows[0].resolution.status, status)
                self.assertIsNone(resolved.rows[0].resolution.label)
                self.assertIs(resolved.rows[0].row, akd.results[0])

    def test_multiple_rows_keep_order_and_independent_statuses(self) -> None:
        second = deepcopy(self.event["data"]["data"]["results"][0])
        second["brokerage"] = "UNKNOWN"
        self.event["data"]["data"]["results"].append(second)
        akd = parse_live_akd_data(self.event)
        resolved = resolve_live_akd_brokerages(
            akd, directory(("TEST", "Synthetic Broker", "Synthetic")),
        )

        self.assertEqual(
            [item.row.brokerage for item in resolved.rows], ["TEST", "UNKNOWN"],
        )
        self.assertEqual(
            [item.resolution.status for item in resolved.rows],
            [BrokerageResolutionStatus.RESOLVED, BrokerageResolutionStatus.UNRESOLVED],
        )

    def test_wrong_input_types_fail_without_payload_values(self) -> None:
        akd = parse_live_akd_data(self.event)
        for bad_response, bad_index in (("PRIVATE", directory()), (akd, "PRIVATE")):
            with self.subTest(bad_response=type(bad_response).__name__):
                with self.assertRaises(TypeError) as captured:
                    resolve_live_akd_brokerages(bad_response, bad_index)
                self.assertNotIn("PRIVATE", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
